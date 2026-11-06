# encoding: utf-8
# -----------------------------------------------------------------------------
# Rhino 本體指令（IronPython 2.7，全部在 UI 執行緒執行）
# 長度一律是「文件模型單位」，除非參數帶 units（mm/cm/m/km/in/ft）。
# 會改文件的指令都包成一個 undo record，一次 Ctrl+Z 剛好復原一步。
# -----------------------------------------------------------------------------
import sys
import os
import math
import json
import time
import System
import Rhino
import Rhino.Geometry as rg
import Rhino.DocObjects as rdo
import scriptcontext as sc

from rb_common import (command, BridgeError, doc, redraw, unit_scale, tol, pt, vec, num, need,
                       parse_guids, get_objects, parse_color, obj_info, obj_type, layer_path,
                       make_attrs, ensure_layer, add_geometry, UndoRecord, bbox_of, temp_path,
                       STATE, to_jsonable)


# --- 小工具 -------------------------------------------------------------------------

def _all_objects(include_hidden=True, include_locked=True):
    s = rdo.ObjectEnumeratorSettings()
    s.NormalObjects = True
    s.HiddenObjects = include_hidden
    s.LockedObjects = include_locked
    s.DeletedObjects = False
    s.IncludeLights = False
    s.IncludeGrips = False
    return list(doc().Objects.GetObjectList(s))


def _selected_objects():
    return list(doc().Objects.GetSelectedObjects(False, False))


def _created(ids, extra=None):
    d = doc()
    res = {"ids": [str(g) for g in ids]}
    objs = []
    for g in ids:
        o = d.Objects.FindId(g)
        if o is not None:
            objs.append(obj_info(o))
    res["objects"] = objs
    if extra:
        res.update(extra)
    return res


def _type_matches(o, wanted):
    if not wanted:
        return True
    names = set([str(o.ObjectType).lower(), o.Geometry.GetType().Name.lower()])
    if isinstance(o.Geometry, rg.Brep):
        names.add("solid" if o.Geometry.IsSolid else "polysurface")
        if o.Geometry.IsSurface:
            names.add("surface")
    for w in wanted:
        if str(w).lower() in names:
            return True
    return False


def _as_brep(o):
    g = o.Geometry
    if isinstance(g, rg.Brep):
        return g.DuplicateBrep()
    if isinstance(g, rg.Extrusion):
        return g.ToBrep()
    if isinstance(g, rg.Surface):
        return rg.Brep.CreateFromSurface(g)
    raise BridgeError("物件 %s（%s）不是可以做布林運算的 Brep / 曲面 / Extrusion。" % (o.Id, o.ObjectType))


def _as_curve(o):
    g = o.Geometry
    if isinstance(g, rg.Curve):
        return g
    raise BridgeError("物件 %s（%s）不是曲線。" % (o.Id, o.ObjectType))


# --- 資訊 ----------------------------------------------------------------------------

@command("get_info")
def get_info(p):
    d = Rhino.RhinoDoc.ActiveDoc
    info = {
        "app": "Rhino",
        "version": str(Rhino.RhinoApp.Version),
        "exe_version": Rhino.RhinoApp.ExeVersion,
        "service_release": Rhino.RhinoApp.ExeServiceRelease,
        "pid": System.Diagnostics.Process.GetCurrentProcess().Id,
        "python": sys.version.split("\n")[0],
        "bridge": STATE.get("status"),
        "has_document": d is not None,
        "in_command": bool(Rhino.Commands.Command.InCommand()),
        "command_prompt": Rhino.RhinoApp.CommandPrompt,
    }
    if d is not None:
        view = d.Views.ActiveView
        info.update({
            "document": {
                "name": d.Name,
                "path": d.Path,
                "modified": bool(d.Modified),
                "model_units": str(d.ModelUnitSystem),
                "absolute_tolerance": d.ModelAbsoluteTolerance,
                "angle_tolerance_deg": d.ModelAngleToleranceDegrees,
                "object_count": d.Objects.Count,
                "selected_count": len(_selected_objects()),
            },
            "active_view": view.ActiveViewport.Name if view is not None else None,
        })
    return info


@command("get_scene_info")
def get_scene_info(p):
    d = doc()
    limit = int(p.get("limit", 200))
    objs = _all_objects()
    by_type = {}
    by_layer = {}
    for o in objs:
        t = obj_type(o)
        by_type[t] = by_type.get(t, 0) + 1
        by_layer[o.Attributes.LayerIndex] = by_layer.get(o.Attributes.LayerIndex, 0) + 1

    layers = []
    for i in range(d.Layers.Count):
        lyr = d.Layers[i]
        if lyr.IsDeleted:
            continue
        layers.append({
            "index": i, "name": lyr.Name, "full_path": lyr.FullPath, "color": lyr.Color,
            "visible": bool(lyr.IsVisible), "locked": bool(lyr.IsLocked),
            "current": (i == d.Layers.CurrentLayerIndex), "object_count": by_layer.get(i, 0),
        })
    materials = []
    try:
        for i in range(d.Materials.Count):
            m = d.Materials[i]
            if m.IsDeleted:
                continue
            materials.append({"index": i, "name": m.Name, "diffuse": m.DiffuseColor, "transparency": m.Transparency})
    except Exception:
        pass
    blocks = []
    try:
        for idef in d.InstanceDefinitions:
            if not idef.IsDeleted:
                blocks.append({"name": idef.Name, "id": str(idef.Id), "object_count": idef.ObjectCount})
    except Exception:
        pass
    views = []
    for v in d.Views:
        vp = v.ActiveViewport
        views.append({"name": vp.Name, "projection": "parallel" if vp.IsParallelProjection else "perspective",
                      "display_mode": vp.DisplayMode.EnglishName if vp.DisplayMode is not None else None,
                      "active": (v == d.Views.ActiveView)})
    sel = _selected_objects()
    return {
        "model_units": str(d.ModelUnitSystem),
        "document": {"name": d.Name, "path": d.Path, "modified": bool(d.Modified)},
        "object_count": len(objs),
        "counts_by_type": by_type,
        "bbox": bbox_of(objs),
        "layers": layers[:limit],
        "layers_truncated": len(layers) > limit,
        "materials": materials[:limit],
        "blocks": blocks[:limit],
        "views": views,
        "selection": {"count": len(sel), "ids": [str(o.Id) for o in sel[:limit]]},
    }


@command("list_objects")
def list_objects(p):
    d = doc()
    types = p.get("types")
    if isinstance(types, basestring):
        types = [types]
    layer = p.get("layer")
    name_contains = p.get("name_contains")
    selected_only = bool(p.get("selected_only"))
    limit = int(p.get("limit", 100))
    offset = int(p.get("offset", 0))
    include_hidden = bool(p.get("include_hidden", True))

    layer_idx = None
    if layer:
        layer_idx = d.Layers.FindByFullPath(str(layer), -1)
        if layer_idx < 0:
            raise BridgeError("找不到圖層 %r。可先用 get_scene_info 查看圖層。" % layer)

    rows = []
    for o in _all_objects(include_hidden=include_hidden):
        if selected_only and not o.IsSelected(False):
            continue
        if layer_idx is not None and o.Attributes.LayerIndex != layer_idx:
            continue
        if name_contains and name_contains.lower() not in (o.Attributes.Name or "").lower():
            continue
        if not _type_matches(o, types):
            continue
        rows.append(o)
    total = len(rows)
    page = rows[offset:offset + limit]
    return {"total": total, "offset": offset, "limit": limit,
            "objects": [obj_info(o) for o in page]}


# --- 建模捷徑 -------------------------------------------------------------------------

def _attrs(p):
    return make_attrs(p.get("name"), p.get("layer"), p.get("color"))


@command("create_box")
def create_box(p):
    s = unit_scale(p.get("units"))
    w, dp, h = num(p, "width", required=True) * s, num(p, "depth", required=True) * s, num(p, "height", required=True) * s
    origin = pt(p.get("origin") or [0, 0, 0], s, "origin")
    if p.get("centered"):
        origin = rg.Point3d(origin.X - w / 2.0, origin.Y - dp / 2.0, origin.Z)
    plane = rg.Plane(origin, rg.Vector3d.XAxis, rg.Vector3d.YAxis)
    box = rg.Box(plane, rg.Interval(0, w), rg.Interval(0, dp), rg.Interval(0, h))
    brep = box.ToBrep()
    if brep is None:
        raise BridgeError("無法建立方塊（尺寸為 0？）。")
    with UndoRecord("create_box"):
        gid = add_geometry(brep, _attrs(p))
    return _created([gid])


@command("create_sphere")
def create_sphere(p):
    s = unit_scale(p.get("units"))
    r = num(p, "radius", required=True) * s
    center = pt(p.get("center") or [0, 0, 0], s, "center")
    brep = rg.Brep.CreateFromSphere(rg.Sphere(center, r))
    with UndoRecord("create_sphere"):
        gid = add_geometry(brep, _attrs(p))
    return _created([gid])


@command("create_cylinder")
def create_cylinder(p):
    s = unit_scale(p.get("units"))
    r = num(p, "radius", required=True) * s
    h = num(p, "height", required=True) * s
    base = pt(p.get("base") or [0, 0, 0], s, "base")
    axis = vec(p.get("axis") or [0, 0, 1], 1.0, "axis")
    if not axis.Unitize():
        raise BridgeError("axis 不能是零向量。")
    cyl = rg.Cylinder(rg.Circle(rg.Plane(base, axis), r), h)
    brep = cyl.ToBrep(bool(p.get("cap", True)), bool(p.get("cap", True)))
    with UndoRecord("create_cylinder"):
        gid = add_geometry(brep, _attrs(p))
    return _created([gid])


@command("create_circle")
def create_circle(p):
    s = unit_scale(p.get("units"))
    r = num(p, "radius", required=True) * s
    center = pt(p.get("center") or [0, 0, 0], s, "center")
    normal = vec(p.get("normal") or [0, 0, 1], 1.0, "normal")
    if not normal.Unitize():
        raise BridgeError("normal 不能是零向量。")
    crv = rg.Circle(rg.Plane(center, normal), r).ToNurbsCurve()
    with UndoRecord("create_circle"):
        gid = add_geometry(crv, _attrs(p))
    return _created([gid])


@command("create_polyline")
def create_polyline(p):
    s = unit_scale(p.get("units"))
    pts = [pt(v, s, "points[%d]" % i) for i, v in enumerate(need(p, "points"))]
    if len(pts) < 2:
        raise BridgeError("polyline 至少需要 2 個點。")
    if p.get("closed") and pts[0].DistanceTo(pts[-1]) > tol():
        pts.append(pts[0])
    crv = rg.PolylineCurve(pts)
    with UndoRecord("create_polyline"):
        gid = add_geometry(crv, _attrs(p))
    return _created([gid], {"closed": bool(crv.IsClosed)})


@command("create_curve")
def create_curve(p):
    s = unit_scale(p.get("units"))
    pts = [pt(v, s, "points[%d]" % i) for i, v in enumerate(need(p, "points"))]
    degree = int(p.get("degree", 3))
    mode = str(p.get("mode", "interpolate")).lower()
    if len(pts) < degree + 1 and mode != "interpolate":
        raise BridgeError("控制點模式需要至少 degree+1 = %d 個點。" % (degree + 1))
    if len(pts) < 2:
        raise BridgeError("curve 至少需要 2 個點。")
    if mode == "interpolate":
        if p.get("closed"):
            if pts[0].DistanceTo(pts[-1]) <= tol():
                pts = pts[:-1]
            crv = rg.Curve.CreateInterpolatedCurve(pts, degree, rg.CurveKnotStyle.UniformPeriodic)
        else:
            crv = rg.Curve.CreateInterpolatedCurve(pts, degree)
    elif mode in ("control_points", "cv"):
        crv = rg.Curve.CreateControlPointCurve(pts, degree)
    else:
        raise BridgeError("mode 必須是 interpolate 或 control_points。")
    if crv is None:
        raise BridgeError("無法建立曲線（點重複或數量不足？）。")
    with UndoRecord("create_curve"):
        gid = add_geometry(crv, _attrs(p))
    return _created([gid], {"closed": bool(crv.IsClosed), "degree": crv.Degree})


@command("extrude")
def extrude(p):
    """把曲線沿方向擠出成 Brep。direction 預設 +Z；height 可為負。"""
    s = unit_scale(p.get("units"))
    objs = get_objects(p.get("ids") or ([p["id"]] if p.get("id") else None))
    height = num(p, "height", required=True) * s
    direction = vec(p.get("direction") or [0, 0, 1], 1.0, "direction")
    if not direction.Unitize():
        raise BridgeError("direction 不能是零向量。")
    cap = bool(p.get("cap", True))
    out = []
    with UndoRecord("extrude"):
        for o in objs:
            crv = _as_curve(o)
            srf = rg.Surface.CreateExtrusion(crv, direction * height)
            if srf is None:
                raise BridgeError("擠出失敗（曲線無效？）：%s" % o.Id)
            brep = srf.ToBrep() if not isinstance(srf, rg.Brep) else srf
            if cap and crv.IsClosed:
                capped = brep.CapPlanarHoles(tol())
                if capped is not None:
                    brep = capped
            out.append(add_geometry(brep, _attrs_from(p, o)))
            if p.get("delete_input"):
                doc().Objects.Delete(o, True)
    return _created(out)


def _attrs_from(p, src=None):
    """沒指定 name/layer/color 時，沿用來源物件的圖層。"""
    a = make_attrs(p.get("name"), p.get("layer"), p.get("color"))
    if src is not None and not p.get("layer"):
        a.LayerIndex = src.Attributes.LayerIndex
    return a


@command("boolean")
def boolean(p):
    """operation: union | difference | intersection。a / b 為 GUID 陣列。"""
    op = str(need(p, "operation")).lower()
    a_objs = get_objects(need(p, "a"))
    b_objs = get_objects(p["b"]) if p.get("b") else []
    a = [_as_brep(o) for o in a_objs]
    b = [_as_brep(o) for o in b_objs]
    t = tol()
    if op == "union":
        res = rg.Brep.CreateBooleanUnion(a + b, t)
    elif op == "difference":
        if not b:
            raise BridgeError("difference 需要 b（要被減掉的物件）。")
        res = rg.Brep.CreateBooleanDifference(a, b, t)
    elif op == "intersection":
        if not b:
            raise BridgeError("intersection 需要 b。")
        res = rg.Brep.CreateBooleanIntersection(a, b, t)
    else:
        raise BridgeError("operation 必須是 union / difference / intersection。")
    if res is None or len(res) == 0:
        raise BridgeError("布林運算沒有產生結果。請確認輸入是『封閉實體』且確實相交（union 的物件需互相重疊或接觸）。")
    out = []
    with UndoRecord("boolean_" + op):
        for brep in res:
            out.append(add_geometry(brep, _attrs_from(p, a_objs[0])))
        if p.get("delete_inputs", True):
            for o in a_objs + b_objs:
                doc().Objects.Delete(o, True)
    return _created(out)


@command("loft")
def loft(p):
    objs = get_objects(need(p, "ids"))
    if len(objs) < 2:
        raise BridgeError("loft 至少需要 2 條曲線。")
    curves = [_as_curve(o) for o in objs]
    lt_name = str(p.get("loft_type", "normal")).capitalize()
    lt = getattr(rg.LoftType, lt_name, None)
    if lt is None:
        raise BridgeError("loft_type 必須是 normal/loose/tight/straight/uniform。")
    res = rg.Brep.CreateFromLoft(curves, rg.Point3d.Unset, rg.Point3d.Unset, lt, bool(p.get("closed")))
    if res is None or len(res) == 0:
        raise BridgeError("放樣失敗。請確認曲線方向一致、不自交。")
    out = []
    with UndoRecord("loft"):
        for brep in res:
            if p.get("cap"):
                capped = brep.CapPlanarHoles(tol())
                if capped is not None:
                    brep = capped
            out.append(add_geometry(brep, _attrs_from(p, objs[0])))
        if p.get("delete_input"):
            for o in objs:
                doc().Objects.Delete(o, True)
    return _created(out)


# --- 變換 / 刪除 / 選取 -----------------------------------------------------------------

@command("transform_objects")
def transform_objects(p):
    s = unit_scale(p.get("units"))
    objs = get_objects(need(p, "ids"))
    xf = rg.Transform.Identity
    did = []
    sc_ = p.get("scale")
    if sc_ is not None:
        origin = pt(p.get("scale_origin") or [0, 0, 0], s, "scale_origin")
        if isinstance(sc_, (list, tuple)):
            sx, sy, sz = [float(x) for x in sc_]
            xs = rg.Transform.Scale(rg.Plane(origin, rg.Vector3d.XAxis, rg.Vector3d.YAxis), sx, sy, sz)
        else:
            xs = rg.Transform.Scale(origin, float(sc_))
        xf = xs * xf
        did.append("scale")
    rot = p.get("rotate")
    if rot:
        axis = vec(rot.get("axis") or [0, 0, 1], 1.0, "rotate.axis")
        origin = pt(rot.get("origin") or [0, 0, 0], s, "rotate.origin")
        ang = math.radians(float(rot.get("angle_deg", 0)))
        xf = rg.Transform.Rotation(ang, axis, origin) * xf
        did.append("rotate")
    mir = p.get("mirror")
    if mir:
        origin = pt(mir.get("origin") or [0, 0, 0], s, "mirror.origin")
        normal = vec(need(mir, "normal"), 1.0, "mirror.normal")
        xf = rg.Transform.Mirror(origin, normal) * xf
        did.append("mirror")
    tr = p.get("translate")
    if tr:
        xf = rg.Transform.Translation(vec(tr, s, "translate")) * xf
        did.append("translate")
    if not did:
        raise BridgeError("沒有指定任何變換（translate / rotate / scale / mirror）。")
    copy = bool(p.get("copy"))
    new_ids = []
    with UndoRecord("transform_objects"):
        for o in objs:
            g = doc().Objects.Transform(o.Id, xf, not copy)
            if g == System.Guid.Empty:
                raise BridgeError("變換失敗：%s" % o.Id)
            new_ids.append(g)
    return _created(new_ids, {"applied": did, "copy": copy})


@command("delete_objects")
def delete_objects(p):
    objs = get_objects(need(p, "ids"))
    n = 0
    with UndoRecord("delete_objects"):
        for o in objs:
            if doc().Objects.Delete(o.Id, True):
                n += 1
    return {"deleted": n, "requested": len(objs)}


@command("select_objects")
def select_objects(p):
    d = doc()
    if p.get("clear") and not p.get("ids"):
        d.Objects.UnselectAll()
        redraw()
        return {"selected": 0}
    objs = get_objects(need(p, "ids"))
    if not p.get("add"):
        d.Objects.UnselectAll()
    n = 0
    for o in objs:
        if d.Objects.Select(o.Id, True):
            n += 1
    redraw()
    return {"selected": n, "total_selected": len(_selected_objects())}


# --- 圖層 / 顏色 / 材質 --------------------------------------------------------------------

@command("set_layer")
def set_layer(p):
    """指派物件到圖層（可順便建立 / 改色 / 設為目前圖層）。"""
    d = doc()
    name = need(p, "layer")
    objs = get_objects(p["ids"]) if p.get("ids") else []
    with UndoRecord("set_layer"):
        if p.get("create", True):
            idx = ensure_layer(name, p.get("color"))
        else:
            idx = d.Layers.FindByFullPath(str(name), -1)
            if idx < 0:
                raise BridgeError("圖層 %r 不存在（create=false）。" % name)
        lyr = d.Layers[idx]
        changed = False
        if p.get("color") is not None:
            lyr.Color = parse_color(p["color"])
            changed = True
        if p.get("visible") is not None:
            lyr.IsVisible = bool(p["visible"])
            changed = True
        if p.get("locked") is not None:
            lyr.IsLocked = bool(p["locked"])
            changed = True
        if changed:
            d.Layers.Modify(lyr, idx, True)
        if p.get("current"):
            d.Layers.SetCurrentLayerIndex(idx, True)
        for o in objs:
            a = o.Attributes.Duplicate()
            a.LayerIndex = idx
            d.Objects.ModifyAttributes(o, a, True)
    return {"layer": d.Layers[idx].FullPath, "layer_index": idx, "assigned": len(objs)}


@command("set_material")
def set_material(p):
    """設定物件顏色與（可選）材質。color: '#RRGGBB' 或 [r,g,b]；transparency 0-1。"""
    d = doc()
    objs = get_objects(need(p, "ids"))
    col = parse_color(p.get("color"))
    transparency = num(p, "transparency")
    mname = p.get("material")
    mat_idx = -1
    with UndoRecord("set_material"):
        if col is not None or transparency is not None or mname:
            if col is None and not mname:
                col = System.Drawing.Color.FromArgb(200, 200, 200)
            if not mname:
                mname = "Agent_%02X%02X%02X" % (col.R, col.G, col.B)
            mat_idx = d.Materials.Find(mname, True)
            if mat_idx < 0:
                m = rdo.Material()
                m.Name = mname
                if col is not None:
                    m.DiffuseColor = col
                if transparency is not None:
                    m.Transparency = max(0.0, min(1.0, transparency))
                mat_idx = d.Materials.Add(m)
            elif col is not None or transparency is not None:
                m = d.Materials[mat_idx]
                if col is not None:
                    m.DiffuseColor = col
                if transparency is not None:
                    m.Transparency = max(0.0, min(1.0, transparency))
                d.Materials.Modify(m, mat_idx, True)
        for o in objs:
            a = o.Attributes.Duplicate()
            if col is not None:
                a.ObjectColor = col
                a.ColorSource = rdo.ObjectColorSource.ColorFromObject
            if mat_idx >= 0:
                a.MaterialIndex = mat_idx
                a.MaterialSource = rdo.ObjectMaterialSource.MaterialFromObject
            d.Objects.ModifyAttributes(o, a, True)
    return {"updated": len(objs), "material": mname, "material_index": mat_idx,
            "color": col}


# --- 指令 / 視圖 / 截圖 -----------------------------------------------------------------------

def _history_text():
    try:
        return Rhino.RhinoApp.CommandHistoryWindowText or ""
    except Exception:
        return ""


@command("run_command")
def run_command(p):
    """執行 Rhino 指令字串（指令腳本形式），例如 '_-Box 0,0,0 10,10,10 5'。"""
    d = doc()
    cmds = need(p, "command")
    if isinstance(cmds, basestring):
        cmds = [cmds]
    echo = bool(p.get("echo", False))
    before = set(o.Id for o in _all_objects())
    hist_before = _history_text()
    results = []
    with UndoRecord("run_command"):
        for c in cmds:
            ok = Rhino.RhinoApp.RunScript(str(c), echo)
            results.append(bool(ok))
            if not ok and p.get("stop_on_error", True):
                break
    after_objs = _all_objects()
    new_ids = [str(o.Id) for o in after_objs if o.Id not in before]
    hist = _history_text()
    output = hist[len(hist_before):] if hist.startswith(hist_before) else hist[-2000:]
    return {
        "succeeded": results,
        "all_succeeded": all(results),
        "new_object_ids": new_ids[:200],
        "new_object_count": len(new_ids),
        "output": output.strip()[-4000:],
        "document_changed": bool(Rhino.RhinoDoc.ActiveDoc is not d),
    }


@command("cancel_command")
def cancel_command(p):
    """Rhino 停在指令提示等輸入（例如 run_command 的指令沒給完整）時用來解除。
    how: escape（預設，取消指令）| enter（以 Enter 接受目前預設值）。
    只能解除『指令列提示』；跳出的對話框要由使用者處理。"""
    how = str(p.get("how", "escape")).lower()
    before = Rhino.RhinoApp.CommandPrompt
    was_in_command = bool(Rhino.Commands.Command.InCommand())
    if how == "enter":
        Rhino.RhinoApp.SendKeystrokes("", True)
    elif how == "escape":
        import clr
        clr.AddReference("System.Windows.Forms")
        import System.Windows.Forms as WF
        Rhino.RhinoApp.SetFocusToMainWindow()
        WF.SendKeys.SendWait("{ESC}")
    else:
        raise BridgeError("how 必須是 escape 或 enter。")
    return {"was_in_command": was_in_command, "prompt_before": before, "how": how}


_PROJ = {
    "top": "Top", "bottom": "Bottom", "front": "Front", "back": "Back",
    "left": "Left", "right": "Right", "perspective": "Perspective",
}


def _find_view(name):
    d = doc()
    if not name:
        v = d.Views.ActiveView
        if v is None:
            raise BridgeError("目前沒有作用中的視埠。")
        return v
    for v in d.Views:
        if v.ActiveViewport.Name.lower() == str(name).lower():
            return v
    raise BridgeError("找不到視埠 %r。可用視埠請看 get_scene_info 的 views。" % name)


@command("set_view")
def set_view(p):
    d = doc()
    v = _find_view(p.get("viewport"))
    if p.get("activate", True):
        d.Views.ActiveView = v
    vp = v.ActiveViewport
    view = p.get("view")
    if view:
        view = str(view).lower()
        if view in _PROJ:
            proj = getattr(Rhino.Display.DefinedViewportProjection, _PROJ[view])
            vp.SetProjection(proj, _PROJ[view], False)
        elif view in ("iso", "isometric"):
            vp.SetProjection(Rhino.Display.DefinedViewportProjection.Perspective, "Perspective", False)
            bb = bbox_of(_all_objects())
            center = bb.Center if bb.IsValid else rg.Point3d.Origin
            size = max(bb.Diagonal.Length, 1.0) if bb.IsValid else 100.0
            direction = rg.Vector3d(1, -1, 0.8)
            direction.Unitize()
            vp.CameraUp = rg.Vector3d.ZAxis
            vp.SetCameraLocations(center, center + direction * size * 1.5)
        else:
            raise BridgeError("view 必須是 top/bottom/front/back/left/right/perspective/iso。")
    if p.get("eye") is not None or p.get("target") is not None:
        s = unit_scale(p.get("units"))
        eye = pt(p["eye"], s, "eye") if p.get("eye") is not None else vp.CameraLocation
        target = pt(p["target"], s, "target") if p.get("target") is not None else vp.CameraTarget
        vp.SetCameraLocations(target, eye)
    if p.get("projection"):
        pr = str(p["projection"]).lower()
        if pr == "parallel":
            vp.ChangeToParallelProjection(True)
        elif pr == "perspective":
            vp.ChangeToPerspectiveProjection(True, p.get("lens", 50.0))
    if p.get("display_mode"):
        mode = Rhino.Display.DisplayModeDescription.FindByName(str(p["display_mode"]))
        if mode is None:
            names = [m.EnglishName for m in Rhino.Display.DisplayModeDescription.GetDisplayModes()]
            raise BridgeError("找不到顯示模式 %r。可用：%s" % (p["display_mode"], ", ".join(names)))
        vp.DisplayMode = mode
    if p.get("maximize") is not None:
        v.Maximized = bool(p["maximize"])
    ze = p.get("zoom_extents")
    if ze is None:
        ze = bool(view)
    if ze:
        if p.get("selected_only"):
            vp.ZoomExtentsSelected()
        else:
            vp.ZoomExtents()
    v.Redraw()
    return {
        "viewport": vp.Name,
        "projection": "parallel" if vp.IsParallelProjection else "perspective",
        "display_mode": vp.DisplayMode.EnglishName if vp.DisplayMode is not None else None,
        "camera_location": vp.CameraLocation,
        "camera_target": vp.CameraTarget,
        "size": [vp.Size.Width, vp.Size.Height],
    }


@command("screenshot")
def screenshot(p):
    """用 RhinoCommon ViewCapture 截 3D 視埠，存成 PNG 並回傳路徑。"""
    v = _find_view(p.get("viewport"))
    vp = v.ActiveViewport
    w = int(p.get("width") or vp.Size.Width)
    h = int(p.get("height") or vp.Size.Height)
    path = p.get("path") or temp_path("viewport")
    d = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(d):
        os.makedirs(d)
    bmp = None
    method = "ViewCapture"
    try:
        vc = Rhino.Display.ViewCapture()
        vc.Width = w
        vc.Height = h
        vc.ScaleScreenItems = False
        vc.DrawAxes = bool(p.get("axes", True))
        vc.DrawGrid = bool(p.get("grid", True))
        vc.DrawGridAxes = False
        vc.TransparentBackground = bool(p.get("transparent", False))
        bmp = vc.CaptureToBitmap(v)
    except Exception:
        bmp = None
    if bmp is None:
        method = "RhinoView.CaptureToBitmap"
        bmp = v.CaptureToBitmap(System.Drawing.Size(w, h), bool(p.get("grid", True)),
                                bool(p.get("axes", True)), False)
    if bmp is None:
        raise BridgeError("視埠截圖失敗（ViewCapture 回傳 null）。")
    try:
        bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png)
    finally:
        bmp.Dispose()
    return {"path": os.path.abspath(path), "width": w, "height": h, "viewport": vp.Name, "method": method}


# --- 復原 / 檔案 ---------------------------------------------------------------------------------

@command("undo")
def undo(p):
    n = 0
    d = doc()
    for _ in range(int(p.get("times", 1))):
        if d.Undo():
            n += 1
        else:
            break
    redraw()
    return {"undone": n}


@command("redo")
def redo(p):
    """重做。RhinoDoc.Redo() 在剛 undo 後常回傳 False（實測 7.1），所以失敗時改走 _Redo 指令。"""
    n = 0
    d = doc()
    notes = []
    for _ in range(int(p.get("times", 1))):
        if d.Redo():
            n += 1
            continue
        before = _history_text()
        with UndoRecord("redo"):   # 實測：不包 record 時 _Redo 會回報「沒有動作可重做」
            ok = Rhino.RhinoApp.RunScript("_Redo", False)
        if not ok:
            break
        after = _history_text()
        said = (after[len(before):] if after.startswith(before) else after[-200:]).strip()
        # 沒有東西可重做時指令仍「成功」，只是命令列會印出提示（中 / 英文）
        if (u"沒有" in said) or ("nothing" in said.lower()) or ("no " in said.lower() and "redo" in said.lower()):
            notes.append(said)
            break
        n += 1
    redraw()
    res = {"redone": n}
    if notes:
        res["note"] = notes[-1]
    return res


def _doc_summary(d):
    return {"name": d.Name, "path": d.Path, "modified": bool(d.Modified)}


def _run_file_command(cmd, max_presses=4):
    """執行會問一串『選項提示』的檔案指令（匯出 STL 等）。

    RunScript 會一直卡到指令結束；提示出現時，用背景計時器每秒派一次到 UI 執行緒，
    對指令列送 Enter（接受預設值），最多 max_presses 次。對話框式的選項仍需要使用者處理。
    """
    state = {"n": 0, "done": False}

    def press():
        if state["done"] or state["n"] >= max_presses:
            return
        if Rhino.Commands.Command.InCommand() and "(" in (Rhino.RhinoApp.CommandPrompt or ""):
            state["n"] += 1
            Rhino.RhinoApp.SendKeystrokes("", True)

    def tick(_state):
        if not state["done"]:
            try:
                Rhino.RhinoApp.InvokeOnUiThread(System.Action(press))
            except Exception:
                pass

    timer = System.Threading.Timer(System.Threading.TimerCallback(tick), None, 1500, 1000)
    try:
        ok = Rhino.RhinoApp.RunScript(cmd, False)
    finally:
        state["done"] = True
        timer.Dispose()
    return bool(ok), state["n"]


@command("new_model")
def new_model(p):
    d = doc()
    if d.Modified and not p.get("confirm"):
        raise BridgeError("目前文件有未儲存的變更。新增文件會『丟棄』這些變更，請取得使用者同意後以 confirm=true 重試。")
    template = p.get("template")  # 預設不套範本（空白文件）
    d.Modified = False
    cmd = '-_New None' if not template else '-_New "%s"' % template
    ok = Rhino.RhinoApp.RunScript(cmd, False)
    nd = Rhino.RhinoDoc.ActiveDoc
    return {"ok": bool(ok), "document": _doc_summary(nd) if nd is not None else None}


@command("open_model")
def open_model(p):
    d = doc()
    path = need(p, "path")
    if not os.path.isfile(path):
        raise BridgeError("找不到檔案：%s" % path)
    if not str(path).lower().endswith(".3dm"):
        raise BridgeError("只支援開啟 .3dm（其他格式請用 run_command 的 -_Import）。")
    if d.Modified and not p.get("confirm"):
        raise BridgeError("目前文件有未儲存的變更。開啟其他檔案會『丟棄』這些變更，請取得使用者同意後以 confirm=true 重試。")
    d.Modified = False
    ok = Rhino.RhinoApp.RunScript('-_Open "%s"' % path, False)
    nd = Rhino.RhinoDoc.ActiveDoc
    return {"ok": bool(ok), "document": _doc_summary(nd) if nd is not None else None}


@command("save_model")
def save_model(p):
    """儲存目前文件。
    * 無 path：存回原路徑（需 confirm=true；文件還沒有路徑則必須提供 path）。
    * 有 path（另存）：目標已存在需 overwrite=true。
    """
    d = doc()
    path = p.get("path")
    if not path:
        if not d.Path:
            raise BridgeError("文件還沒有存檔路徑，請提供 path（另存新檔）。")
        if not p.get("confirm"):
            raise BridgeError("這會覆寫 %s。未經使用者明確同意不要存檔；取得同意後以 confirm=true 重試。" % d.Path)
        ok, _n = _run_file_command("-_Save")
        return {"ok": bool(ok), "saved_to": d.Path, "document": _doc_summary(doc())}
    path = os.path.abspath(path)
    if not path.lower().endswith(".3dm"):
        raise BridgeError("另存新檔的副檔名必須是 .3dm（匯出其他格式請用 export）。")
    if os.path.exists(path) and not p.get("overwrite"):
        raise BridgeError("檔案已存在：%s。要覆蓋必須取得使用者同意並設 overwrite=true。" % path)
    if not p.get("confirm"):
        raise BridgeError("另存新檔會改變目前文件的路徑。取得使用者同意後以 confirm=true 重試。")
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        raise BridgeError("資料夾不存在：%s" % folder)
    ok, _n = _run_file_command('-_SaveAs "%s"' % path)
    return {"ok": bool(ok and os.path.isfile(path)), "saved_to": path, "document": _doc_summary(doc())}


@command("export_model")
def export_model(p):
    """匯出（不改變目前文件路徑）。副檔名決定格式：.3dm .stl .obj .step .igs .dwg .fbx …"""
    d = doc()
    path = os.path.abspath(need(p, "path"))
    if os.path.exists(path) and not p.get("overwrite"):
        raise BridgeError("檔案已存在：%s。要覆蓋必須取得使用者同意並設 overwrite=true。" % path)
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        raise BridgeError("資料夾不存在：%s" % folder)
    if p.get("ids"):
        objs = get_objects(p["ids"])
        d.Objects.UnselectAll()
        for o in objs:
            d.Objects.Select(o.Id, True)
        selected = True
    else:
        selected = bool(p.get("selected_only")) and len(_selected_objects()) > 0
    if not selected and len(_all_objects()) == 0:
        raise BridgeError("文件是空的，沒有東西可匯出。")
    if not selected:
        d.Objects.UnselectAll()
        for o in _all_objects(include_hidden=False, include_locked=True):
            d.Objects.Select(o.Id, True)
    if os.path.exists(path):
        os.remove(path)  # 已取得 overwrite 同意；先刪掉以免跳出『覆蓋？』對話框
    ok, presses = _run_file_command('-_Export "%s"' % path)
    return {"ok": bool(ok and os.path.isfile(path)), "path": path,
            "size": os.path.getsize(path) if os.path.isfile(path) else 0,
            "option_prompts_accepted": presses}


# --- 任意 IronPython ---------------------------------------------------------------------------

class _Capture(object):
    def __init__(self):
        self.parts = []

    def write(self, s):
        try:
            self.parts.append(s if isinstance(s, basestring) else str(s))
        except Exception:
            self.parts.append(repr(s))

    def flush(self):
        pass

    def getvalue(self):
        return u"".join([x if isinstance(x, unicode) else x.decode("utf-8", "replace") for x in self.parts])


def _result_summary(value):
    if value is None:
        return None
    try:
        json.dumps(value, default=to_jsonable)
        return value
    except Exception:
        return repr(value)


def run_user_code(code, ns, undo_name):
    cap = _Capture()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = cap, cap
    try:
        with UndoRecord(undo_name):
            exec code in ns
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return cap.getvalue()


@command("eval_python")
def eval_python(p):
    """在 Rhino 的 IronPython 2.7 執行程式碼。可用名稱：Rhino, rg(Rhino.Geometry), rs(rhinoscriptsyntax),
    sc(scriptcontext), doc, System, math, json；把要回傳的值放在變數 result。"""
    import rhinoscriptsyntax as rs
    code = need(p, "code")
    ns = {
        "__name__": "__rhino_agent__",
        "Rhino": Rhino, "rg": rg, "rdo": rdo, "rs": rs, "sc": sc, "System": System,
        "math": math, "json": json, "doc": doc(), "result": None,
    }
    stdout = run_user_code(code, ns, "eval_python")
    value = ns.get("result")
    return {"stdout": stdout, "result": _result_summary(value),
            "result_type": type(value).__name__ if value is not None else None}
