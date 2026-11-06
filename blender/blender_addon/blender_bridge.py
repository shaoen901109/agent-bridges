# -*- coding: utf-8 -*-
"""
blender_bridge.py — Blender 端 startup 腳本（Blender Agent Bridge 的唯一正本）。

安裝位置：<Blender 使用者 scripts>/startup/blender_bridge.py
          （Blender 啟動時會 import startup 內每個模組並呼叫 register()）

功能：在 127.0.0.1:9877（可用環境變數 BLENDER_BRIDGE_PORT 改）開一個
JSON-over-TCP 控制伺服器，讓外部程式（MCP server / CLI / 任何 agent）
操控這個正在執行的 Blender。

協定：一條連線 = 一個請求。client 送一行 JSON（\\n 結尾），server 回一行 JSON
後關閉連線。請求格式二擇一（兩種都接受）：

    {"cmd": "ping"}                                   # 參數放頂層
    {"cmd": "exec", "code": "..."}
    {"id": 1, "method": "exec", "params": {"code": "..."}}   # 類 JSON-RPC

回應一律：{"ok": true, ...} 或 {"ok": false, "error": "..."}；請求帶 id 會原樣回傳。

所有 bpy 操作都在 Blender 主執行緒執行：socket thread 把工作丟進 queue，
bpy.app.timers 的回呼（_process_queue）在主執行緒取出執行，再用 threading.Event
把結果交回 socket thread。--background 模式下 timers 不會跑，測試腳本要自己
呼叫 _process_queue() 輪詢（見 test_bg.py）。
"""

import contextlib
import io
import json
import math
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import traceback

import bpy
import bmesh
import mathutils

BRIDGE_VERSION = "2.0.0"
HOST = os.environ.get("BLENDER_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BLENDER_BRIDGE_PORT", "9877"))
TIMER_INTERVAL = 0.05      # 主執行緒輪詢 queue 的間隔（秒）
JOB_TIMEOUT = 600.0        # socket thread 等待主執行緒結果的上限（秒）
TAG = "[blender_bridge]"

# --------------------------------------------------------------------------
# 模組狀態
# --------------------------------------------------------------------------

_job_queue = queue.Queue()
_deferred_jobs = []            # (not_before_timestamp, job)
_server_sock = None
_server_thread = None
_stop_event = threading.Event()

# exec 指令共用的持久 globals
_exec_globals = {
    "__name__": "__blender_bridge_exec__",
    "bpy": bpy,
    "bmesh": bmesh,
    "mathutils": mathutils,
    "math": math,
    "Vector": mathutils.Vector,
    "Matrix": mathutils.Matrix,
    "Euler": mathutils.Euler,
    "Quaternion": mathutils.Quaternion,
}


class _Job:
    __slots__ = ("request", "response", "done", "passes")

    def __init__(self, request):
        self.request = request
        self.response = None
        self.done = threading.Event()
        self.passes = 0


class _Defer(Exception):
    """handler 丟出此例外表示：先讓 Blender 重繪，delay 秒後再重新執行本工作。"""

    def __init__(self, delay=0.15):
        super().__init__("defer")
        self.delay = delay


# --------------------------------------------------------------------------
# 共用工具（只能在主執行緒呼叫）
# --------------------------------------------------------------------------

def _json_default(o):
    if isinstance(o, (mathutils.Vector, mathutils.Euler, mathutils.Quaternion, mathutils.Color)):
        return list(o)
    if isinstance(o, mathutils.Matrix):
        return [list(row) for row in o]
    if isinstance(o, bpy.types.ID):
        return o.name
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _safe_json(obj):
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        try:
            return json.loads(json.dumps(obj, default=_json_default))
        except Exception:
            return str(obj)


def _r(v, nd=5):
    return [round(float(x), nd) for x in v]


def _scene():
    scene = getattr(bpy.context, "scene", None)
    if scene is None:
        window = _first_window()
        scene = window.scene if window is not None else None
    if scene is None and bpy.data.scenes:
        scene = bpy.data.scenes[0]
    return scene


def _view_layer(scene):
    vl = getattr(bpy.context, "view_layer", None)
    if vl is None or scene is None or vl.name not in scene.view_layers:
        vl = scene.view_layers[0] if (scene is not None and scene.view_layers) else None
    return vl


def _first_window():
    wm = bpy.context.window_manager
    if wm is None:
        return None
    for window in wm.windows:
        return window
    return None


def _find_view3d():
    """回傳第一個（面積最大的）VIEW_3D 的 (window, area, region)，找不到回 (None, None, None)。"""
    wm = bpy.context.window_manager
    if wm is None:
        return None, None, None
    best = (None, None, None)
    best_size = -1
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = None
            for reg in area.regions:
                if reg.type == "WINDOW":
                    region = reg
                    break
            size = area.width * area.height
            if size > best_size:
                best_size = size
                best = (window, area, region)
    return best


def _override_kwargs(window, area=None, region=None):
    kw = {}
    if window is not None:
        kw["window"] = window
        if window.screen is not None:
            kw["screen"] = window.screen
    if area is not None:
        kw["area"] = area
    if region is not None:
        kw["region"] = region
    return kw


def _undo_push(message):
    """在「改動之後」推一個 undo step（與 Blender operator 相同），使用者 Ctrl+Z 剛好回到改動前。
    失敗（例如 background 模式）不影響指令，只回報 undo_pushed=false。"""
    try:
        window = _first_window()
        kw = _override_kwargs(window)
        if kw:
            with bpy.context.temp_override(**kw):
                bpy.ops.ed.undo_push(message=str(message))
        else:
            bpy.ops.ed.undo_push(message=str(message))
        return True
    except Exception:
        return False


def _names(req, key="names"):
    names = req.get(key)
    if names is None:
        name = req.get("name")
        names = [name] if name else []
    if isinstance(names, str):
        names = [names]
    return [str(n) for n in names]


def _default_shot_path(prefix):
    directory = os.path.join(tempfile.gettempdir(), "blender_bridge")
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S") + "_%03d" % (int(time.time() * 1000) % 1000)
    return os.path.join(directory, "%s_%s.png" % (prefix, stamp))


def _shrink_png(path, max_edge):
    """用 bpy.data.images 把 PNG 等比縮到最長邊 <= max_edge（原地覆寫）。回傳 (w, h)。"""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        longest = max(w, h)
        if max_edge and longest > max_edge:
            scale = max_edge / float(longest)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            img.scale(nw, nh)
            img.filepath_raw = path
            img.file_format = "PNG"
            img.save()
            return nw, nh
        return w, h
    finally:
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass


def _object_brief(ob, view_layer):
    try:
        visible = ob.visible_get(view_layer=view_layer) if view_layer else None
    except Exception:
        visible = None
    info = {
        "name": ob.name,
        "type": ob.type,
        "location": _r(ob.location),
        "rotation_deg": _r([math.degrees(a) for a in ob.rotation_euler], 3),
        "scale": _r(ob.scale),
        "dimensions": _r(ob.dimensions),
        "parent": ob.parent.name if ob.parent else None,
        "collections": [c.name for c in ob.users_collection],
        "visible": visible,
        "data": ob.data.name if ob.data else None,
    }
    if ob.type == "MESH" and ob.data is not None:
        info["vertices"] = len(ob.data.vertices)
        info["faces"] = len(ob.data.polygons)
    if ob.modifiers:
        info["modifiers"] = [m.name for m in ob.modifiers]
    return info


def _filter_objects(scene, req):
    types = req.get("types")
    if isinstance(types, str):
        types = [types]
    types = {t.upper() for t in types} if types else None
    contains = req.get("name_contains")
    contains = str(contains).lower() if contains else None
    objs = []
    for ob in scene.objects:
        if types and ob.type not in types:
            continue
        if contains and contains not in ob.name.lower():
            continue
        objs.append(ob)
    total = len(objs)
    offset = max(0, int(req.get("offset") or 0))
    limit = req.get("limit")
    limit = int(limit) if limit is not None else 200
    if limit >= 0:
        objs = objs[offset:offset + limit]
    else:
        objs = objs[offset:]
    return objs, total, offset


# --------------------------------------------------------------------------
# 指令 handlers（只能在主執行緒呼叫）
# --------------------------------------------------------------------------

def _cmd_ping(req):
    return {
        "ok": True,
        "blender": bpy.app.version_string,
        "blender_version": list(bpy.app.version),
        "bridge_version": BRIDGE_VERSION,
        "background": bpy.app.background,
        "pid": os.getpid(),
        "port": PORT,
        "file": bpy.data.filepath,
        "is_dirty": bpy.data.is_dirty,
        "scene": _scene().name if _scene() else None,
    }


def _cmd_list_commands(req):
    return {"ok": True, "commands": sorted(_HANDLERS.keys()), "bridge_version": BRIDGE_VERSION}


def _cmd_exec(req):
    code = req.get("code")
    if not isinstance(code, str):
        return {"ok": False, "error": "exec: 需要 'code'（字串）"}

    _exec_globals.pop("result", None)
    want_undo = req.get("undo_push", True)
    message = req.get("message") or "bridge exec"

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    t0 = time.time()
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            exec(compile(code, "<blender_bridge exec>", "exec"), _exec_globals)
    except Exception:
        # 執行到一半失敗也 push，讓使用者能一次 Ctrl+Z 回復部分改動
        undo = _undo_push(message + " (failed)") if want_undo else False
        return {
            "ok": False,
            "error": traceback.format_exc(),
            "stdout": out_buf.getvalue(),
            "stderr": err_buf.getvalue(),
            "undo_pushed": undo,
        }

    # 與 Blender operator 相同：改動「之後」push，Ctrl+Z 剛好回到改動前
    undo = _undo_push(message) if want_undo else False
    resp = {
        "ok": True,
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "elapsed": round(time.time() - t0, 3),
        "undo_pushed": undo,
    }
    if "result" in _exec_globals:
        resp["result"] = _safe_json(_exec_globals["result"])
    return resp


def _cmd_scene_info(req):
    scene = _scene()
    if scene is None:
        return {"ok": False, "error": "scene_info: 沒有任何場景"}
    vl = _view_layer(scene)

    counts = {}
    selected = []
    for ob in scene.objects:
        counts[ob.type] = counts.get(ob.type, 0) + 1
        try:
            if vl is not None and ob.select_get(view_layer=vl):
                selected.append(ob.name)
        except Exception:
            pass

    def _coll(c, depth=0):
        return {
            "name": c.name,
            "objects": len(c.objects),
            "children": [_coll(ch, depth + 1) for ch in c.children] if depth < 6 else [],
        }

    objs, total, offset = _filter_objects(scene, req)
    us = scene.unit_settings
    active = vl.objects.active.name if (vl and vl.objects.active) else None
    return {
        "ok": True,
        "scene": scene.name,
        "scenes": [s.name for s in bpy.data.scenes],
        "file": bpy.data.filepath,
        "is_dirty": bpy.data.is_dirty,
        "frame_current": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "fps": scene.render.fps,
        "units": {
            "system": us.system,
            "scale_length": us.scale_length,
            "length_unit": us.length_unit,
            "rotation_unit": us.system_rotation,
        },
        "render": {
            "engine": scene.render.engine,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "percentage": scene.render.resolution_percentage,
        },
        "camera": scene.camera.name if scene.camera else None,
        "object_count": len(scene.objects),
        "counts_by_type": counts,
        "collections": [_coll(c) for c in scene.collection.children],
        "root_collection_objects": [o.name for o in scene.collection.objects][:50],
        "selected": selected,
        "active": active,
        "objects_total": total,
        "objects_offset": offset,
        "objects": [_object_brief(ob, vl) for ob in objs],
    }


def _cmd_list_objects(req):
    scene = _scene()
    if scene is None:
        return {"ok": False, "error": "list_objects: 沒有任何場景"}
    vl = _view_layer(scene)
    if "limit" not in req:
        req["limit"] = 100
    objs, total, offset = _filter_objects(scene, req)
    return {
        "ok": True,
        "total": total,
        "offset": offset,
        "count": len(objs),
        "objects": [_object_brief(ob, vl) for ob in objs],
    }


def _cmd_select(req):
    scene = _scene()
    vl = _view_layer(scene)
    if vl is None:
        return {"ok": False, "error": "select: 沒有 view layer"}
    names = _names(req)
    if req.get("clear") or not names:
        for ob in scene.objects:
            try:
                ob.select_set(False, view_layer=vl)
            except Exception:
                pass
        if not names:
            vl.objects.active = None
    selected, missing = [], []
    for name in names:
        ob = bpy.data.objects.get(name)
        if ob is None or ob.name not in vl.objects:
            missing.append(name)
            continue
        try:
            ob.select_set(True, view_layer=vl)
            selected.append(name)
        except Exception as exc:
            missing.append("%s (%s)" % (name, exc))
    active_name = req.get("active") or (selected[-1] if selected else None)
    if active_name and active_name in vl.objects:
        vl.objects.active = bpy.data.objects[active_name]
    return {
        "ok": True,
        "selected": selected,
        "missing": missing,
        "active": vl.objects.active.name if vl.objects.active else None,
        "selection_now": [o.name for o in scene.objects if o.select_get(view_layer=vl)],
    }


_DATA_COLLECTIONS = {
    "MESH": "meshes", "CURVE": "curves", "SURFACE": "curves", "FONT": "curves",
    "META": "metaballs", "ARMATURE": "armatures", "LATTICE": "lattices",
    "CAMERA": "cameras", "LIGHT": "lights", "SPEAKER": "speakers",
    "LIGHT_PROBE": "lightprobes", "GPENCIL": "grease_pencils", "VOLUME": "volumes",
}


def _cmd_delete(req):
    names = _names(req)
    if not names:
        return {"ok": False, "error": "delete: 需要 'names'（名稱清單）"}
    deleted, missing = [], []
    for name in names:
        ob = bpy.data.objects.get(name)
        if ob is None:
            missing.append(name)
            continue
        data = ob.data
        ob_type = ob.type
        bpy.data.objects.remove(ob, do_unlink=True)
        deleted.append(name)
        # 順手清掉沒人用的 data-block（mesh/curve/...），避免留下孤兒資料
        if req.get("remove_data", True) and data is not None and data.users == 0:
            attr = _DATA_COLLECTIONS.get(ob_type)
            try:
                coll = getattr(bpy.data, attr) if attr else None
                if coll is not None and data.name in coll:
                    coll.remove(data)
            except Exception:
                pass
    undo = _undo_push("bridge delete %s" % ", ".join(names[:5]))
    return {"ok": True, "deleted": deleted, "missing": missing, "undo_pushed": undo}


def _cmd_transform(req):
    names = _names(req)
    if not names:
        return {"ok": False, "error": "transform: 需要 'names'（名稱清單）"}
    location = req.get("location")
    rotation = req.get("rotation")          # 度
    scale = req.get("scale")
    delta = req.get("delta")                # 相對位移
    delta_rotation = req.get("delta_rotation")  # 相對旋轉（度）
    delta_scale = req.get("delta_scale")    # 相對縮放（乘法）
    if isinstance(scale, (int, float)):
        scale = [scale] * 3
    if isinstance(delta_scale, (int, float)):
        delta_scale = [delta_scale] * 3

    changed, missing = [], []
    for name in names:
        ob = bpy.data.objects.get(name)
        if ob is None:
            missing.append(name)
            continue
        if location is not None:
            ob.location = mathutils.Vector([float(v) for v in location])
        if rotation is not None:
            ob.rotation_mode = "XYZ" if ob.rotation_mode not in {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"} else ob.rotation_mode
            ob.rotation_euler = mathutils.Euler([math.radians(float(v)) for v in rotation], ob.rotation_mode)
        if scale is not None:
            ob.scale = mathutils.Vector([float(v) for v in scale])
        if delta is not None:
            ob.location = ob.location + mathutils.Vector([float(v) for v in delta])
        if delta_rotation is not None:
            e = ob.rotation_euler
            ob.rotation_euler = mathutils.Euler(
                [e[i] + math.radians(float(delta_rotation[i])) for i in range(3)], e.order)
        if delta_scale is not None:
            ob.scale = mathutils.Vector([ob.scale[i] * float(delta_scale[i]) for i in range(3)])
        changed.append({
            "name": ob.name,
            "location": _r(ob.location),
            "rotation_deg": _r([math.degrees(a) for a in ob.rotation_euler], 3),
            "scale": _r(ob.scale),
        })
    try:
        vl = _view_layer(_scene())
        if vl is not None:
            vl.update()
    except Exception:
        pass
    undo = _undo_push("bridge transform %s" % ", ".join(names[:5]))
    return {"ok": True, "changed": changed, "missing": missing, "undo_pushed": undo}


_VIEW_AXES = {"front": "FRONT", "back": "BACK", "left": "LEFT", "right": "RIGHT",
              "top": "TOP", "bottom": "BOTTOM"}
_SHADING = {"wireframe": "WIREFRAME", "solid": "SOLID", "material": "MATERIAL",
            "rendered": "RENDERED"}


def _view_state(space, r3d):
    return {
        "view_perspective": r3d.view_perspective,
        "is_perspective": r3d.is_perspective,
        "shading": space.shading.type,
        "view_distance": round(float(r3d.view_distance), 4),
        "view_location": _r(r3d.view_location),
        "view_rotation_quat": _r(r3d.view_rotation),
    }


def _cmd_set_view(req):
    if bpy.app.background:
        return {"ok": False, "error": "set_view: Blender 在 --background 模式沒有 3D 視埠"}
    window, area, region = _find_view3d()
    if area is None or region is None:
        return {"ok": False, "error": "set_view: 找不到任何 VIEW_3D 區域"}
    space = area.spaces.active
    r3d = space.region_3d
    applied = []

    prefs_view = bpy.context.preferences.view
    saved_smooth = prefs_view.smooth_view
    try:
        prefs_view.smooth_view = 0   # 讓視角瞬間切換，截圖不會截到動畫中途
        with bpy.context.temp_override(**_override_kwargs(window, area, region)):
            view = req.get("view")
            if view:
                v = str(view).lower()
                if v == "iso":
                    r3d.view_rotation = mathutils.Euler(
                        (math.radians(54.7356), 0.0, math.radians(45.0)), "XYZ").to_quaternion()
                    applied.append("view=iso")
                elif v in _VIEW_AXES:
                    bpy.ops.view3d.view_axis(type=_VIEW_AXES[v])
                    applied.append("view=" + v)
                else:
                    return {"ok": False, "error": "set_view: view 必須是 front/back/left/right/top/bottom/iso，收到 %r" % view}
            projection = req.get("projection")
            if projection:
                p = str(projection).lower()
                if p in ("ortho", "orthographic"):
                    r3d.view_perspective = "ORTHO"
                elif p in ("persp", "perspective"):
                    r3d.view_perspective = "PERSP"
                else:
                    return {"ok": False, "error": "set_view: projection 必須是 ortho/persp"}
                applied.append("projection=" + p)
            shading = req.get("shading")
            if shading:
                s = str(shading).lower()
                if s not in _SHADING:
                    return {"ok": False, "error": "set_view: shading 必須是 wireframe/solid/material/rendered"}
                space.shading.type = _SHADING[s]
                applied.append("shading=" + s)
            if req.get("zoom_extents"):
                bpy.ops.view3d.view_all(center=False)
                applied.append("zoom_extents")
            if req.get("view_selected"):
                bpy.ops.view3d.view_selected(use_all_regions=False)
                applied.append("view_selected")
    finally:
        prefs_view.smooth_view = saved_smooth
    area.tag_redraw()
    return {"ok": True, "applied": applied, "view": _view_state(space, r3d)}


def _cmd_screenshot(req, _job=None):
    mode = str(req.get("mode", "viewport")).lower()
    if bpy.app.background:
        return {"ok": False, "error": "screenshot: Blender 在 --background 模式沒有視窗/視埠，無法截圖"}
    path = req.get("path") or _default_shot_path(mode)
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    max_edge = req.get("max_edge")

    window, area, region = _find_view3d()
    if window is None:
        window = _first_window()
    if window is None:
        return {"ok": False, "error": "screenshot: 沒有任何 Blender 視窗"}

    # 第一次進來：先要求重繪，延後一小段時間再真正截圖，避免截到舊畫面
    if _job is not None and _job.passes == 0:
        for a in window.screen.areas:
            a.tag_redraw()
        raise _Defer(float(req.get("redraw_delay", 0.15)))

    attempts = []
    method = None
    if mode == "window":
        try:
            with bpy.context.temp_override(**_override_kwargs(window)):
                bpy.ops.screen.screenshot(filepath=path)
            if os.path.exists(path):
                method = "screen.screenshot"
            else:
                attempts.append("screen.screenshot: 檔案未寫出")
        except Exception:
            attempts.append("screen.screenshot: " + traceback.format_exc())
    else:
        if area is None:
            return {"ok": False, "error": "screenshot: 找不到任何 VIEW_3D 區域"}
        if hasattr(bpy.ops.screen, "screenshot_area"):
            try:
                with bpy.context.temp_override(**_override_kwargs(window, area, region)):
                    bpy.ops.screen.screenshot_area(filepath=path)
                if os.path.exists(path):
                    method = "screen.screenshot_area"
                else:
                    attempts.append("screen.screenshot_area: 檔案未寫出")
            except Exception:
                attempts.append("screen.screenshot_area: " + traceback.format_exc())
        else:
            attempts.append("screen.screenshot_area: 此版本沒有這個 operator")
        if method is None:
            scene = _scene()
            r = scene.render
            saved = (r.filepath, r.image_settings.file_format, r.image_settings.color_mode)
            try:
                r.filepath = path
                r.image_settings.file_format = "PNG"
                r.image_settings.color_mode = "RGBA"
                with bpy.context.temp_override(**_override_kwargs(window, area, region)):
                    bpy.ops.render.opengl(view_context=True, write_still=True)
                if os.path.exists(path):
                    method = "render.opengl"
                else:
                    attempts.append("render.opengl: 檔案未寫出")
            except Exception:
                attempts.append("render.opengl: " + traceback.format_exc())
            finally:
                r.filepath = saved[0]
                try:
                    r.image_settings.file_format = saved[1]
                    r.image_settings.color_mode = saved[2]
                except Exception:
                    pass
    if method is None:
        return {"ok": False, "error": "\n".join(attempts)}

    size = None
    try:
        size = list(_shrink_png(path, int(max_edge) if max_edge else 0))
    except Exception as exc:
        attempts.append("shrink: %s" % exc)
    return {
        "ok": True,
        "path": path,
        "mode": mode,
        "method": method,
        "size": size,
        "note": "; ".join(attempts) if attempts else None,
    }


def _undo_state():
    scene = _scene()
    return {
        "is_dirty": bpy.data.is_dirty,
        "object_count": len(scene.objects) if scene else None,
        "objects": sorted(o.name for o in scene.objects)[:50] if scene else [],
    }


def _cmd_undo_redo(req, direction):
    times = max(1, int(req.get("times") or 1))
    before = _undo_state()
    window = _first_window()
    kw = _override_kwargs(window)
    results = []
    error = None
    for _ in range(times):
        try:
            if kw:
                with bpy.context.temp_override(**kw):
                    res = bpy.ops.ed.undo() if direction == "undo" else bpy.ops.ed.redo()
            else:
                res = bpy.ops.ed.undo() if direction == "undo" else bpy.ops.ed.redo()
            results.append(sorted(res))
            if "FINISHED" not in res:
                break
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, exc)
            break
    after = _undo_state()
    resp = {
        "ok": error is None and bool(results) and all("FINISHED" in r for r in results),
        "direction": direction,
        "requested": times,
        "performed": sum(1 for r in results if "FINISHED" in r),
        "operator_results": results,
        "before": before,
        "after": after,
    }
    if bpy.app.background:
        resp["note"] = "Blender 在 --background 模式：undo 堆疊可用但 is_dirty 不會更新；GUI 行為以實機為準"
    if error:
        resp["error"] = error
    elif not resp["ok"]:
        resp["error"] = "%s 未執行（operator 回傳 %s）——可能已在堆疊底/頂端，或 background 模式不支援" % (direction, results)
    return resp


def _cmd_undo(req):
    return _cmd_undo_redo(req, "undo")


def _cmd_redo(req):
    return _cmd_undo_redo(req, "redo")


def _run_wm_op(fn):
    window = _first_window()
    kw = _override_kwargs(window)
    if kw:
        with bpy.context.temp_override(**kw):
            return fn()
    return fn()


def _cmd_open(req):
    path = req.get("path")
    if not path:
        return {"ok": False, "error": "open: 需要 'path'"}
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return {"ok": False, "error": "open: 找不到檔案：%s" % path}
    if bpy.data.is_dirty and not req.get("confirm"):
        return {"ok": False, "refused": True,
                "error": "open: 目前檔案有未儲存的變更（is_dirty=true）。請先詢問使用者是否要儲存；"
                         "確定放棄變更請帶 confirm=true。",
                "current_file": bpy.data.filepath}
    load_ui = bool(req.get("load_ui", False))
    res = _run_wm_op(lambda: bpy.ops.wm.open_mainfile(filepath=path, load_ui=load_ui))
    return {"ok": "FINISHED" in res, "result": sorted(res), "file": bpy.data.filepath,
            "is_dirty": bpy.data.is_dirty}


def _cmd_new(req):
    if bpy.data.is_dirty and not req.get("confirm"):
        return {"ok": False, "refused": True,
                "error": "new: 目前檔案有未儲存的變更（is_dirty=true）。請先詢問使用者是否要儲存；"
                         "確定放棄變更請帶 confirm=true。",
                "current_file": bpy.data.filepath}
    use_empty = bool(req.get("empty", False))
    res = _run_wm_op(lambda: bpy.ops.wm.read_homefile(use_empty=use_empty, load_ui=False))
    scene = _scene()
    return {"ok": "FINISHED" in res, "result": sorted(res), "file": bpy.data.filepath,
            "objects": [o.name for o in scene.objects] if scene else []}


def _cmd_save(req):
    current = bpy.data.filepath
    path = req.get("path")
    confirm = bool(req.get("confirm"))
    overwrite = bool(req.get("overwrite"))
    if not path:
        if not current:
            return {"ok": False, "error": "save: 檔案從未儲存過，需要指定 'path'"}
        if not confirm:
            return {"ok": False, "refused": True,
                    "error": "save: 存回原路徑 %s 需要 confirm=true（必須由使用者明確要求）" % current}
        path = current
    else:
        path = os.path.abspath(path)
        if current and os.path.normcase(path) == os.path.normcase(os.path.abspath(current)):
            if not confirm:
                return {"ok": False, "refused": True,
                        "error": "save: 存回原路徑 %s 需要 confirm=true（必須由使用者明確要求）" % current}
        elif os.path.exists(path) and not overwrite:
            return {"ok": False, "refused": True,
                    "error": "save: 目標檔案已存在 %s，覆寫需要 overwrite=true（必須由使用者明確要求）" % path}
    if not path.lower().endswith(".blend"):
        path += ".blend"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    copy = bool(req.get("copy", False))
    res = _run_wm_op(lambda: bpy.ops.wm.save_as_mainfile(filepath=path, copy=copy))
    return {"ok": "FINISHED" in res, "result": sorted(res), "path": path,
            "file": bpy.data.filepath, "is_dirty": bpy.data.is_dirty, "copy": copy}


_ENGINES = {"WORKBENCH": "BLENDER_WORKBENCH", "BLENDER_WORKBENCH": "BLENDER_WORKBENCH",
            "EEVEE": "BLENDER_EEVEE", "BLENDER_EEVEE": "BLENDER_EEVEE",
            "CYCLES": "CYCLES"}


def _cmd_render(req):
    scene = _scene()
    if scene is None:
        return {"ok": False, "error": "render: 沒有任何場景"}
    path = req.get("path") or _default_shot_path("render")
    path = os.path.abspath(path)
    if not path.lower().endswith(".png"):
        path += ".png"
    os.makedirs(os.path.dirname(path), exist_ok=True)

    engine_req = req.get("engine")
    engine = _ENGINES.get(str(engine_req).upper()) if engine_req else None
    if engine_req and engine is None:
        return {"ok": False, "error": "render: engine 必須是 WORKBENCH/EEVEE/CYCLES，收到 %r" % engine_req}

    r = scene.render
    saved = {
        "engine": r.engine, "filepath": r.filepath,
        "file_format": r.image_settings.file_format, "color_mode": r.image_settings.color_mode,
        "res_x": r.resolution_x, "res_y": r.resolution_y, "percentage": r.resolution_percentage,
        "camera": scene.camera,
        "cycles_samples": getattr(scene.cycles, "samples", None) if hasattr(scene, "cycles") else None,
        "eevee_samples": getattr(scene.eevee, "taa_render_samples", None) if hasattr(scene, "eevee") else None,
    }
    t0 = time.time()
    try:
        cam_name = req.get("camera")
        if cam_name:
            cam = bpy.data.objects.get(cam_name)
            if cam is None or cam.type != "CAMERA":
                return {"ok": False, "error": "render: 找不到相機物件 %r" % cam_name}
            scene.camera = cam
        if scene.camera is None:
            return {"ok": False, "error": "render: 場景沒有相機（scene.camera 為 None）。"
                                          "請先用 exec 建立相機，或用 camera 參數指定。"}
        if engine:
            r.engine = engine
        res = req.get("resolution")
        if res:
            r.resolution_x, r.resolution_y = int(res[0]), int(res[1])
        if req.get("percentage"):
            r.resolution_percentage = int(req["percentage"])
        samples = req.get("samples")
        if samples:
            if r.engine == "CYCLES" and hasattr(scene, "cycles"):
                scene.cycles.samples = int(samples)
            elif r.engine == "BLENDER_EEVEE" and hasattr(scene, "eevee"):
                scene.eevee.taa_render_samples = int(samples)
        r.filepath = path
        r.image_settings.file_format = "PNG"
        r.image_settings.color_mode = "RGBA" if req.get("transparent") else "RGB"
        if req.get("transparent"):
            r.film_transparent = True

        window = _first_window()
        kw = _override_kwargs(window)
        kw["scene"] = scene
        with bpy.context.temp_override(**kw):
            result = bpy.ops.render.render(write_still=True)
        if not os.path.exists(path):
            return {"ok": False, "error": "render: operator 回傳 %s 但檔案未寫出：%s" % (sorted(result), path)}
        return {
            "ok": True, "path": path, "engine": r.engine,
            "resolution": [r.resolution_x, r.resolution_y], "percentage": r.resolution_percentage,
            "camera": scene.camera.name, "elapsed": round(time.time() - t0, 2),
        }
    finally:
        try:
            r.engine = saved["engine"]
            r.filepath = saved["filepath"]
            r.image_settings.file_format = saved["file_format"]
            r.image_settings.color_mode = saved["color_mode"]
            r.resolution_x, r.resolution_y = saved["res_x"], saved["res_y"]
            r.resolution_percentage = saved["percentage"]
            scene.camera = saved["camera"]
            if saved["cycles_samples"] is not None:
                scene.cycles.samples = saved["cycles_samples"]
            if saved["eevee_samples"] is not None:
                scene.eevee.taa_render_samples = saved["eevee_samples"]
        except Exception:
            pass


_HANDLERS = {
    "ping": _cmd_ping,
    "list_commands": _cmd_list_commands,
    "exec": _cmd_exec,
    "scene_info": _cmd_scene_info,
    "list_objects": _cmd_list_objects,
    "select": _cmd_select,
    "delete": _cmd_delete,
    "transform": _cmd_transform,
    "set_view": _cmd_set_view,
    "screenshot": _cmd_screenshot,
    "undo": _cmd_undo,
    "redo": _cmd_redo,
    "open": _cmd_open,
    "save": _cmd_save,
    "new": _cmd_new,
    "render": _cmd_render,
}


def _normalize_request(req):
    """把 {"method","params"} 與 {"cmd", ...} 兩種格式統一成一個扁平 dict。"""
    cmd = req.get("cmd") or req.get("method")
    merged = dict(req)
    params = req.get("params")
    if isinstance(params, dict):
        merged.update(params)
    merged["cmd"] = cmd
    return merged


def _dispatch(job):
    req = _normalize_request(job.request)
    cmd = req.get("cmd")
    handler = _HANDLERS.get(cmd)
    if handler is None:
        return {"ok": False,
                "error": "未知指令 %r（可用：%s）" % (cmd, ", ".join(sorted(_HANDLERS)))}
    try:
        if handler is _cmd_screenshot:
            return handler(req, _job=job)
        return handler(req)
    except _Defer:
        raise
    except Exception:
        return {"ok": False, "error": traceback.format_exc()}


# --------------------------------------------------------------------------
# 主執行緒 pump
# --------------------------------------------------------------------------

def _process_queue():
    """在主執行緒處理待辦工作。bpy.app.timers 會定期呼叫；測試也可手動呼叫。"""
    now = time.time()
    if _deferred_jobs:
        still = []
        for not_before, job in _deferred_jobs:
            if now >= not_before:
                _job_queue.put(job)
            else:
                still.append((not_before, job))
        _deferred_jobs[:] = still

    while True:
        try:
            job = _job_queue.get_nowait()
        except queue.Empty:
            break
        try:
            job.response = _dispatch(job)
        except _Defer as d:
            job.passes += 1
            _deferred_jobs.append((time.time() + d.delay, job))
            continue
        except Exception:
            job.response = {"ok": False, "error": traceback.format_exc()}
        if "id" in job.request and isinstance(job.response, dict):
            job.response.setdefault("id", job.request["id"])
        job.done.set()
    return TIMER_INTERVAL


def _timer_callback():
    try:
        _process_queue()
    except Exception:
        traceback.print_exc()
    return TIMER_INTERVAL


# --------------------------------------------------------------------------
# Socket server（背景執行緒）
# --------------------------------------------------------------------------

def _read_line(conn, limit=64 * 1024 * 1024):
    chunks = []
    total = 0
    while True:
        data = conn.recv(65536)
        if not data:
            break
        chunks.append(data)
        total += len(data)
        if b"\n" in data or total > limit:
            break
    raw = b"".join(chunks)
    nl = raw.find(b"\n")
    if nl != -1:
        raw = raw[:nl]
    return raw


def _handle_client(conn, addr):
    try:
        conn.settimeout(30.0)
        raw = _read_line(conn)
        if not raw.strip():
            resp = {"ok": False, "error": "empty request"}
        else:
            try:
                req = json.loads(raw.decode("utf-8"))
                if not isinstance(req, dict):
                    raise ValueError("request must be a JSON object")
            except Exception as e:
                req = None
                resp = {"ok": False, "error": "bad JSON request: %s" % e}
            if req is not None:
                job = _Job(req)
                _job_queue.put(job)
                if job.done.wait(JOB_TIMEOUT):
                    resp = job.response
                else:
                    resp = {"ok": False,
                            "error": "timeout: 主執行緒 %d 秒內沒有處理工作"
                                     "（Blender 忙碌中、跳出對話框或 modal operator 執行中？）" % JOB_TIMEOUT}
        try:
            payload = json.dumps(resp, ensure_ascii=False, default=str)
        except Exception as e:
            payload = json.dumps({"ok": False, "error": "response not serialisable: %s" % e})
        conn.settimeout(30.0)
        conn.sendall(payload.encode("utf-8") + b"\n")
    except Exception:
        traceback.print_exc()
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def _serve(sock):
    sock.settimeout(0.5)
    while not _stop_event.is_set():
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        t = threading.Thread(target=_handle_client, args=(conn, addr),
                             name="blender_bridge_client", daemon=True)
        t.start()
    try:
        sock.close()
    except Exception:
        pass


def _start_server():
    global _server_sock, _server_thread
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))
    sock.listen(8)
    _server_sock = sock
    _stop_event.clear()
    _server_thread = threading.Thread(target=_serve, args=(sock,),
                                      name="blender_bridge_server", daemon=True)
    _server_thread.start()
    print("%s v%s listening on %s:%d (pid %d)" % (TAG, BRIDGE_VERSION, HOST, PORT, os.getpid()))


def _stop_server():
    global _server_sock, _server_thread
    _stop_event.set()
    if _server_sock is not None:
        try:
            _server_sock.close()
        except Exception:
            pass
        _server_sock = None
    if _server_thread is not None:
        _server_thread.join(timeout=2.0)
        _server_thread = None


# --------------------------------------------------------------------------
# Blender 進入點
# --------------------------------------------------------------------------

def register():
    if _server_sock is not None or _server_thread is not None:
        print("%s closing previous server instance" % TAG)
        _stop_server()
    try:
        _start_server()
    except OSError as e:
        # 埠被佔用（例如另一個 Blender）：只印警告，絕不讓 Blender 啟動失敗
        print("%s WARNING: could not bind %s:%d (%s). Bridge disabled." % (TAG, HOST, PORT, e))
        return
    try:
        if not bpy.app.timers.is_registered(_timer_callback):
            bpy.app.timers.register(_timer_callback, first_interval=TIMER_INTERVAL,
                                    persistent=True)
    except Exception:
        traceback.print_exc()


def unregister():
    try:
        if bpy.app.timers.is_registered(_timer_callback):
            bpy.app.timers.unregister(_timer_callback)
    except Exception:
        pass
    _stop_server()
    print("%s stopped" % TAG)


if __name__ == "__main__":
    # 允許 `blender --python blender_bridge.py` 或在 Text Editor 直接 Run Script
    register()
