# encoding: utf-8
# -----------------------------------------------------------------------------
# Grasshopper 指令（IronPython 2.7，全部在 Rhino UI 執行緒執行）
#
# 命名坑（給使用者與 agent）：
#   * 元件 / 參數 / 輸出入埠都用「Name 或 NickName（不分大小寫）」或「索引」指定。
#     例如 Circle 元件輸入：Plane(P)、Radius(R)；Extrude：Base(B)、Direction(D)。
#   * 物件一律用 instance GUID（add_* 與 list_document 回傳的 id）；不要用 proxy / component GUID。
#   * Number Slider / Panel / Boolean Toggle 本身就是「參數」：輸出就是自己（索引 0）。
# -----------------------------------------------------------------------------
import sys
import os
import math
import json
import time
import clr
import System
import Rhino
import Rhino.Geometry as rg

from rb_common import (command, BridgeError, doc as rhino_doc, redraw, parse_guid, parse_guids,
                       pt, vec, num, need, parse_color, make_attrs, ensure_layer, add_geometry,
                       UndoRecord, temp_path, to_jsonable)

_GH = {"loaded": False}


def _load_gh_modules():
    clr.AddReference("Grasshopper")
    clr.AddReference("GH_IO")
    import Grasshopper
    return Grasshopper


# --- 載入 / 文件存取 ---------------------------------------------------------------------

def ensure_gh(editor=True):
    """載入 Grasshopper 外掛（必要時載入編輯器但不顯示）。回傳 (gh 介面, Grasshopper 模組)。"""
    gh = Rhino.RhinoApp.GetPlugInObject("Grasshopper")
    if gh is None:
        raise BridgeError("無法載入 Grasshopper 外掛（Rhino 內沒有 Grasshopper？）。")
    G = _load_gh_modules()
    if editor and not gh.IsEditorLoaded():
        gh.DisableBanner()          # 載入時不要跳出 GH 啟動橫幅
        try:
            gh.LoadEditor()
        finally:
            gh.EnableBanner()       # 還原使用者自己的設定
    _GH["loaded"] = True
    return gh, G


def active_doc(G, create=False):
    canvas = G.Instances.ActiveCanvas
    d = canvas.Document if canvas is not None else None
    if d is None:
        if not create:
            raise BridgeError("目前沒有作用中的 Grasshopper 文件。請先用 gh_new_document 或 gh_open_document。")
        d = _new_doc(G)
    return d


def _new_doc(G):
    from Grasshopper.Kernel import GH_Document
    d = GH_Document()
    G.Instances.DocumentServer.AddDocument(d)
    canvas = G.Instances.ActiveCanvas
    if canvas is not None:
        canvas.Document = d
    return d


def _ctx(create=False):
    gh, G = ensure_gh(True)
    d = active_doc(G, create)
    return gh, G, d


# --- 物件 / 埠 摘要 ------------------------------------------------------------------------

def _is_component(G, o):
    return isinstance(o, G.Kernel.IGH_Component)


def _is_param(G, o):
    return isinstance(o, G.Kernel.IGH_Param)


def _kind(G, o):
    n = o.GetType().Name
    return {"GH_NumberSlider": "slider", "GH_Panel": "panel", "GH_BooleanToggle": "toggle",
            "GH_Scribble": "scribble", "GH_ColourSwatch": "colour_swatch"}.get(
        n, "component" if _is_component(G, o) else ("param" if _is_param(G, o) else n))


def _in_params(G, o):
    if _is_component(G, o):
        return list(o.Params.Input)
    k = _kind(G, o)
    if k in ("slider", "toggle", "colour_swatch", "scribble"):
        return []
    if _is_param(G, o):
        return [o]
    return []


def _out_params(G, o):
    if _is_component(G, o):
        return list(o.Params.Output)
    if _is_param(G, o):
        return [o]
    return []


def _owner(p):
    try:
        return p.Attributes.GetTopLevel.DocObject
    except Exception:
        return None


def _port(G, p, idx, is_input):
    info = {"index": idx, "name": p.Name, "nickname": p.NickName, "type": p.TypeName,
            "access": str(p.Access)}
    try:
        info["optional"] = bool(p.Optional)
    except Exception:
        pass
    try:
        dm = str(p.DataMapping)
        if dm != "None":
            info["data_mapping"] = dm
        if p.Reverse:
            info["reverse"] = True
        if p.Simplify:
            info["simplify"] = True
    except Exception:
        pass
    if is_input:
        srcs = []
        for s in p.Sources:
            own = _owner(s)
            if own is None:
                continue
            try:
                outs = _out_params(G, own)
                oi = [id(x) for x in outs].index(id(s)) if False else None
                for i, x in enumerate(outs):
                    if x.InstanceGuid == s.InstanceGuid:
                        oi = i
                        break
            except Exception:
                oi = None
            srcs.append({"id": str(own.InstanceGuid), "name": own.NickName, "output": oi})
        info["sources"] = srcs
        try:
            if hasattr(p, "PersistentData") and p.PersistentData.DataCount > 0:
                info["persistent_data"] = [_short(x) for x in list(p.PersistentData.AllData(True))[:6]]
        except Exception:
            pass
    return info


def _short(goo):
    try:
        return str(goo)
    except Exception:
        return repr(goo)


def _messages(G, o):
    out = {}
    L = G.Kernel.GH_RuntimeMessageLevel
    for name, lvl in (("errors", L.Error), ("warnings", L.Warning), ("remarks", L.Remark)):
        try:
            msgs = [m for m in o.RuntimeMessages(lvl)]
        except Exception:
            msgs = []
        if msgs:
            out[name] = msgs
    return out


def _obj_info(G, o, ports=True):
    info = {
        "id": str(o.InstanceGuid),
        "name": o.Name,
        "nickname": o.NickName,
        "kind": _kind(G, o),
        "category": o.Category,
        "subcategory": o.SubCategory,
        "pivot": [round(o.Attributes.Pivot.X, 1), round(o.Attributes.Pivot.Y, 1)],
    }
    try:
        info["component_guid"] = str(o.ComponentGuid)
    except Exception:
        pass
    k = info["kind"]
    if k == "slider":
        s = o.Slider
        info["slider"] = {"min": float(s.Minimum), "max": float(s.Maximum), "value": float(s.Value),
                          "integer": str(s.Type) == "Integer", "decimals": int(s.DecimalPlaces)}
    elif k == "panel":
        info["text"] = o.UserText
    elif k == "toggle":
        info["value"] = bool(o.Value)
    if ports:
        info["inputs"] = [_port(G, p, i, True) for i, p in enumerate(_in_params(G, o))]
        info["outputs"] = [_port(G, p, i, False) for i, p in enumerate(_out_params(G, o))]
    msgs = _messages(G, o)
    if msgs:
        info["messages"] = msgs
    if getattr(o, "Locked", False):
        info["disabled"] = True
    return info


def find_object(G, d, ref):
    """instance GUID，或唯一的 NickName / Name。"""
    if ref is None or str(ref).strip() == "":
        raise BridgeError("缺少物件 id。")
    s = str(ref).strip()
    try:
        g = System.Guid.Parse(s.strip("{}"))
        o = d.FindObject(g, True)
        if o is not None:
            return o
        raise BridgeError("文件中找不到 instance GUID %s（可用 gh_list_document 查看）。" % s)
    except BridgeError:
        raise
    except Exception:
        pass
    hits = [o for o in d.Objects if o.NickName.lower() == s.lower()] or \
           [o for o in d.Objects if o.Name.lower() == s.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise BridgeError("找不到物件 %r（請用 instance GUID，或唯一的名稱 / 暱稱）。" % s)
    raise BridgeError("名稱 %r 對應到多個物件，請改用 instance GUID：%s" % (
        s, ", ".join(["%s(%s)" % (o.InstanceGuid, o.Name) for o in hits])))


def find_port(G, obj, which, is_input):
    ports = _in_params(G, obj) if is_input else _out_params(G, obj)
    label = "輸入" if is_input else "輸出"
    if not ports:
        raise BridgeError("物件 %s（%s）沒有可用的%s埠。" % (obj.NickName, obj.Name, label))
    if which is None:
        if len(ports) == 1:
            return ports[0], 0
        raise BridgeError("物件 %s 有 %d 個%s埠（%s），請指定名稱或索引。" % (
            obj.NickName, len(ports), label, ", ".join([p.NickName for p in ports])))
    if isinstance(which, (int, long)) or (isinstance(which, basestring) and which.strip().lstrip("-").isdigit()):
        i = int(which)
        if i < 0 or i >= len(ports):
            raise BridgeError("%s索引 %d 超出範圍 0..%d。" % (label, i, len(ports) - 1))
        return ports[i], i
    w = str(which).strip().lower()
    for key in ("nick", "name"):
        hits = [(p, i) for i, p in enumerate(ports) if (p.NickName if key == "nick" else p.Name).lower() == w]
        if len(hits) == 1:
            return hits[0]
    raise BridgeError("物件 %s 沒有名為 %r 的%s埠。可用：%s" % (
        obj.NickName, which, label,
        ", ".join(["%d:%s(%s)" % (i, p.Name, p.NickName) for i, p in enumerate(ports)])))


# --- 元件搜尋 / 放置 ------------------------------------------------------------------------

def _proxy_row(p):
    d = p.Desc
    return {"guid": str(p.Guid), "name": d.Name, "nickname": d.NickName, "category": d.Category,
            "subcategory": d.SubCategory, "description": (d.Description or "")[:160],
            "obsolete": bool(p.Obsolete), "kind": str(p.Kind)}


def _ports_of_proxy(G, p):
    try:
        obj = p.CreateInstance()
    except Exception:
        return None
    ins = [{"name": x.Name, "nickname": x.NickName, "type": x.TypeName, "access": str(x.Access),
            "optional": bool(x.Optional)} for x in _in_params(G, obj)]
    outs = [{"name": x.Name, "nickname": x.NickName, "type": x.TypeName} for x in _out_params(G, obj)]
    return {"inputs": ins, "outputs": outs}


def _score(p, q):
    d = p.Desc
    name, nick = (d.Name or "").lower(), (d.NickName or "").lower()
    if name == q:
        return 100
    if nick == q:
        return 90
    if name.startswith(q):
        return 70
    if q in name:
        return 60
    if nick.startswith(q):
        return 50
    if q in nick:
        return 40
    if q in (d.Description or "").lower():
        return 10
    return 0


def _search(G, query, category=None, subcategory=None):
    q = (query or "").strip().lower()
    rows = []
    for p in G.Instances.ComponentServer.ObjectProxies:
        d = p.Desc
        if category and (d.Category or "").lower() != str(category).lower():
            continue
        if subcategory and (d.SubCategory or "").lower() != str(subcategory).lower():
            continue
        if not q:
            sc_ = 1
        else:
            sc_ = _score(p, q)
        if sc_ <= 0:
            continue
        hidden = False
        try:
            hidden = str(p.Exposure) == "hidden"
        except Exception:
            pass
        # 同分時：未過時 > 過時；正式元件 > 隱藏；Params 類別放後面
        rank = (sc_, 0 if p.Obsolete else 1, 0 if hidden else 1, 0 if (d.Category or "") == "Params" else 1)
        rows.append((rank, p))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


@command("gh_search_components")
def gh_search_components(p):
    gh, G = ensure_gh(True)
    q = p.get("query")
    limit = int(p.get("limit", 15))
    with_ports = bool(p.get("with_ports", True))
    rows = _search(G, q, p.get("category"), p.get("subcategory"))
    out = []
    for rank, px in rows[:limit]:
        r = _proxy_row(px)
        if with_ports:
            r.update(_ports_of_proxy(G, px) or {})
        out.append(r)
    return {"total_matches": len(rows), "returned": len(out), "results": out}


def _resolve_proxy(G, p):
    guid = p.get("guid")
    if guid:
        g = parse_guid(guid)
        px = G.Instances.ComponentServer.EmitObjectProxy(g)
        if px is None:
            raise BridgeError("找不到 component GUID %s。" % guid)
        return px
    name = need(p, "name")
    rows = _search(G, name, p.get("category"), p.get("subcategory"))
    exact = [(r, px) for r, px in rows if r[0] >= 90]
    pool = exact or rows
    if not pool:
        raise BridgeError("找不到元件 %r。請先用 gh_search_components 搜尋。" % name)
    top = pool[0][0]
    best = [px for r, px in pool if r == top]
    if len(best) > 1:
        raise BridgeError("名稱 %r 有 %d 個同等符合的元件，請加上 category / subcategory，或直接給 guid：\n%s" % (
            name, len(best), "\n".join(["  %s  %s/%s  %s" % (x.Guid, x.Desc.Category, x.Desc.SubCategory, x.Desc.Name)
                                         for x in best[:8]])))
    return best[0]


def _right_edge(d):
    r = None
    for o in d.Objects:
        a = o.Attributes
        right = a.Bounds.Right if a.Bounds.Width > 0 else a.Pivot.X + 120.0
        r = right if r is None else max(r, right)
    return r


def _place(G, d, obj, x, y):
    from System.Drawing import PointF
    obj.CreateAttributes()
    if x is None:
        r = _right_edge(d)
        x = 60.0 if r is None else r + 70.0
    if y is None:
        y = 60.0
    obj.Attributes.Pivot = PointF(float(x), float(y))
    try:
        obj.Attributes.PerformLayout()   # 讓 Bounds 有值，下一個物件才排得到右邊
    except Exception:
        pass


def _finish_add(G, d, obj, p):
    if p.get("nickname"):
        obj.NickName = str(p["nickname"])
    d.AddObject(obj, False)
    return _obj_info(G, obj)


@command("gh_add_component")
def gh_add_component(p):
    gh, G, d = _ctx()
    px = _resolve_proxy(G, p)
    obj = px.CreateInstance()
    _place(G, d, obj, p.get("x"), p.get("y"))
    return _finish_add(G, d, obj, p)


def _fmt_num(v, integer, decimals=2):
    if integer:
        return str(int(round(float(v))))
    return ("%." + str(decimals) + "f") % float(v)


@command("gh_add_slider")
def gh_add_slider(p):
    gh, G, d = _ctx()
    from Grasshopper.Kernel.Special import GH_NumberSlider
    mn, mx = num(p, "min", required=True), num(p, "max", required=True)
    if mx <= mn:
        raise BridgeError("max 必須大於 min。")
    val = num(p, "value", mn)
    val = max(mn, min(mx, val))
    integer = bool(p.get("integer", False))
    decimals = int(p.get("decimals", 2))
    obj = GH_NumberSlider()
    _place(G, d, obj, p.get("x"), p.get("y"))
    # SetInitCode 格式 "min<value<max"：整數寫法 → Integer 型，含小數點 → Float 型
    obj.SetInitCode("%s<%s<%s" % (_fmt_num(mn, integer, decimals), _fmt_num(val, integer, decimals),
                                  _fmt_num(mx, integer, decimals)))
    if p.get("nickname") is None and p.get("name"):
        p["nickname"] = p["name"]
    return _finish_add(G, d, obj, p)


@command("gh_add_panel")
def gh_add_panel(p):
    gh, G, d = _ctx()
    from Grasshopper.Kernel.Special import GH_Panel
    obj = GH_Panel()
    _place(G, d, obj, p.get("x"), p.get("y"))
    text = p.get("text")
    if text is not None:
        obj.SetUserText(unicode(text) if not isinstance(text, basestring) else text)
    if p.get("nickname") is None and p.get("name"):
        p["nickname"] = p["name"]
    info = _finish_add(G, d, obj, p)
    w, h = p.get("width"), p.get("height")
    if w and h:
        from System.Drawing import RectangleF
        piv = obj.Attributes.Pivot
        obj.Attributes.Bounds = RectangleF(piv.X, piv.Y, float(w), float(h))
    return info


@command("gh_add_toggle")
def gh_add_toggle(p):
    gh, G, d = _ctx()
    from Grasshopper.Kernel.Special import GH_BooleanToggle
    obj = GH_BooleanToggle()
    _place(G, d, obj, p.get("x"), p.get("y"))
    obj.Value = bool(p.get("value", False))
    return _finish_add(G, d, obj, p)


# --- 設定輸入 / 連線 ----------------------------------------------------------------------------

def _convert_item(G, param, v):
    """把 JSON 值轉成適合該輸入參數的 .NET 值。"""
    tn = param.GetType().Name
    if isinstance(v, dict):
        if "rhino_id" in v:
            o = rhino_doc().Objects.FindId(parse_guid(v["rhino_id"]))
            if o is None:
                raise BridgeError("找不到 Rhino 物件 %s。" % v["rhino_id"])
            return o.Geometry.Duplicate()
        if "origin" in v:  # plane
            o = pt(v["origin"], 1.0, "plane.origin")
            n = vec(v.get("normal", [0, 0, 1]), 1.0, "plane.normal")
            return rg.Plane(o, n)
        raise BridgeError("不認得的值格式 %r（支援 {\"rhino_id\":...} 或 {\"origin\":..,\"normal\":..}）。" % (v,))
    if tn == "Param_Point":
        return pt(v, 1.0, "point")
    if tn == "Param_Vector":
        return vec(v, 1.0, "vector")
    if tn == "Param_Plane" and isinstance(v, (list, tuple)):
        return rg.Plane(pt(v, 1.0, "plane origin"), rg.Vector3d.ZAxis)
    if tn == "Param_Interval":
        a, b = [float(x) for x in v][:2]
        return rg.Interval(a, b)
    if tn == "Param_Colour":
        return parse_color(v)
    if tn == "Param_Boolean":
        return bool(v)
    if tn == "Param_Integer":
        return int(v)
    if tn == "Param_Number":
        return float(v)
    if tn == "Param_String":
        return v if isinstance(v, basestring) else unicode(v)
    return v


_POINTLIKE = ("Param_Point", "Param_Vector", "Param_Plane", "Param_Interval", "Param_Colour")


@command("gh_set_input")
def gh_set_input(p):
    """設定輸入埠的 persistent data（沒連線時使用的值）。
    對 slider / panel / toggle 本身則是直接設定它的值。"""
    gh, G, d = _ctx()
    obj = find_object(G, d, p.get("id"))
    value = p.get("value")
    k = _kind(G, obj)
    with UndoRecord("gh_set_input"):
        if k == "slider":
            s = obj.Slider
            v = float(value)
            if v < float(s.Minimum) or v > float(s.Maximum):
                raise BridgeError("slider 值 %s 超出範圍 [%s, %s]。" % (v, s.Minimum, s.Maximum))
            obj.SetSliderValue(System.Decimal(v))
        elif k == "panel":
            obj.SetUserText(value if isinstance(value, basestring) else unicode(value))
        elif k == "toggle":
            obj.Value = bool(value)
        else:
            param, idx = find_port(G, obj, p.get("input"), True)
            if not hasattr(param, "SetPersistentData"):
                raise BridgeError("輸入埠 %s 不支援 persistent data（請改用連線）。" % param.NickName)
            if value is None:
                param.PersistentData.Clear()
            else:
                tn = param.GetType().Name
                if isinstance(value, (list, tuple)):
                    flat_single = tn in _POINTLIKE and len(value) in (2, 3) and \
                        all(isinstance(x, (int, long, float)) for x in value)
                    if tn == "Param_Interval" and len(value) == 2 and all(isinstance(x, (int, long, float)) for x in value):
                        flat_single = True
                    items = [value] if flat_single else list(value)
                else:
                    items = [value]
                conv = [_convert_item(G, param, x) for x in items]
                param.PersistentData.Clear()
                param.SetPersistentData(*conv)
            if p.get("flatten") is not None or p.get("graft") is not None:
                M = G.Kernel.GH_DataMapping
                if p.get("flatten"):
                    param.DataMapping = M.Flatten
                elif p.get("graft"):
                    param.DataMapping = M.Graft
                else:
                    param.DataMapping = getattr(M, "None")
            if p.get("reverse") is not None:
                param.Reverse = bool(p["reverse"])
            if p.get("simplify") is not None:
                param.Simplify = bool(p["simplify"])
        obj.ExpireSolution(True)
    return _obj_info(G, obj)


@command("gh_connect")
def gh_connect(p):
    gh, G, d = _ctx()
    src = find_object(G, d, p.get("source"))
    dst = find_object(G, d, p.get("target"))
    sp, si = find_port(G, src, p.get("source_output"), False)
    tp, ti = find_port(G, dst, p.get("target_input"), True)
    with UndoRecord("gh_connect"):
        if p.get("replace"):
            tp.RemoveAllSources()
        already = any(s.InstanceGuid == sp.InstanceGuid for s in tp.Sources)
        if not already:
            tp.AddSource(sp)
        dst.ExpireSolution(True)
    return {"connected": not already, "already_connected": already,
            "source": {"id": str(src.InstanceGuid), "name": src.NickName, "output": si, "output_name": sp.Name},
            "target": {"id": str(dst.InstanceGuid), "name": dst.NickName, "input": ti, "input_name": tp.Name},
            "target_sources": len(list(tp.Sources))}


@command("gh_disconnect")
def gh_disconnect(p):
    gh, G, d = _ctx()
    dst = find_object(G, d, p.get("target"))
    tp, ti = find_port(G, dst, p.get("target_input"), True)
    removed = 0
    with UndoRecord("gh_disconnect"):
        if p.get("source"):
            src = find_object(G, d, p.get("source"))
            sp, si = find_port(G, src, p.get("source_output"), False)
            for s in list(tp.Sources):
                if s.InstanceGuid == sp.InstanceGuid:
                    tp.RemoveSource(s)
                    removed += 1
        else:
            removed = len(list(tp.Sources))
            tp.RemoveAllSources()
        dst.ExpireSolution(True)
    return {"removed": removed, "remaining_sources": len(list(tp.Sources))}


# --- 文件 --------------------------------------------------------------------------------------

def _doc_summary(G, d):
    return {"name": d.DisplayName, "path": d.FilePath, "modified": bool(d.IsModified),
            "object_count": d.ObjectCount, "enabled": bool(d.Enabled),
            "solution_state": str(d.SolutionState)}


@command("gh_status")
def gh_status(p):
    gh, G = ensure_gh(bool(p.get("load_editor", True)))
    canvas = G.Instances.ActiveCanvas
    d = canvas.Document if canvas is not None else None
    return {
        "grasshopper_version": str(G.Versioning.Version.ToString()) if hasattr(G, "Versioning") else None,
        "editor_loaded": bool(gh.IsEditorLoaded()),
        "editor_visible": bool(gh.IsEditorVisible()) if gh.IsEditorLoaded() else False,
        "solver_enabled": bool(gh.IsSolverEnabled()),
        "document_count": G.Instances.DocumentServer.DocumentCount,
        "active_document": _doc_summary(G, d) if d is not None else None,
        "component_proxies": len(list(G.Instances.ComponentServer.ObjectProxies)),
    }


@command("gh_open_editor")
def gh_open_editor(p):
    gh, G = ensure_gh(True)
    show = p.get("show", True)
    if show:
        gh.ShowEditor()
    else:
        gh.HideEditor()
    return {"editor_loaded": True, "editor_visible": bool(gh.IsEditorVisible())}


@command("gh_new_document")
def gh_new_document(p):
    gh, G = ensure_gh(True)
    d = _new_doc(G)
    if p.get("show", False):
        gh.ShowEditor()
    return _doc_summary(G, d)


@command("gh_open_document")
def gh_open_document(p):
    gh, G = ensure_gh(True)
    path = os.path.abspath(need(p, "path"))
    if not os.path.isfile(path):
        raise BridgeError("找不到檔案：%s" % path)
    if not path.lower().endswith((".gh", ".ghx")):
        raise BridgeError("只能開啟 .gh / .ghx。")
    io = G.Kernel.GH_DocumentIO()
    if not io.Open(path):
        raise BridgeError("開啟失敗（檔案毀損，或使用了未安裝的外掛元件）：%s" % path)
    d = io.Document
    d.FilePath = path
    G.Instances.DocumentServer.AddDocument(d)
    canvas = G.Instances.ActiveCanvas
    if canvas is not None:
        canvas.Document = d
    if p.get("show", False):
        gh.ShowEditor()
    return _doc_summary(G, d)


@command("gh_save_document")
def gh_save_document(p):
    gh, G, d = _ctx()
    if not p.get("confirm"):
        raise BridgeError("儲存 Grasshopper 文件需要使用者同意；取得同意後以 confirm=true 重試。")
    path = p.get("path") or d.FilePath
    if not path:
        raise BridgeError("文件還沒有存檔路徑，請提供 path（.gh 或 .ghx）。")
    path = os.path.abspath(path)
    if not path.lower().endswith((".gh", ".ghx")):
        raise BridgeError("副檔名必須是 .gh 或 .ghx。")
    if os.path.exists(path) and path != (d.FilePath or "") and not p.get("overwrite"):
        raise BridgeError("檔案已存在：%s。要覆蓋必須取得使用者同意並設 overwrite=true。" % path)
    if not os.path.isdir(os.path.dirname(path)):
        raise BridgeError("資料夾不存在：%s" % os.path.dirname(path))
    io = G.Kernel.GH_DocumentIO(d)
    ok = io.SaveQuiet(path)
    if not ok:
        raise BridgeError("儲存失敗。")
    d.FilePath = path
    return {"saved_to": path, "size": os.path.getsize(path) if os.path.isfile(path) else 0}


@command("gh_list_document")
def gh_list_document(p):
    gh, G, d = _ctx()
    ports = bool(p.get("ports", True))
    objs = [_obj_info(G, o, ports) for o in d.Objects]
    wires = []
    problems = []
    for o in d.Objects:
        for ti, tp in enumerate(_in_params(G, o)):
            for s in tp.Sources:
                own = _owner(s)
                if own is None:
                    continue
                oi = None
                for i, x in enumerate(_out_params(G, own)):
                    if x.InstanceGuid == s.InstanceGuid:
                        oi = i
                wires.append({"from": str(own.InstanceGuid), "from_output": oi,
                              "to": str(o.InstanceGuid), "to_input": ti})
        m = _messages(G, o)
        if m.get("errors") or m.get("warnings"):
            problems.append({"id": str(o.InstanceGuid), "name": o.NickName,
                             "errors": m.get("errors", []), "warnings": m.get("warnings", [])})
    return {"document": _doc_summary(G, d), "objects": objs, "wires": wires, "problems": problems}


@command("gh_delete_objects")
def gh_delete_objects(p):
    gh, G, d = _ctx()
    ids = p.get("ids")
    if not ids:
        raise BridgeError("缺少 ids。")
    objs = [find_object(G, d, i) for i in ids]
    with UndoRecord("gh_delete_objects"):
        d.RemoveObjects(objs, True)
    return {"deleted": len(objs), "remaining": d.ObjectCount}


# --- 求解 / 讀輸出 / 烘焙 -----------------------------------------------------------------------

@command("gh_solve")
def gh_solve(p):
    gh, G, d = _ctx()
    t0 = time.time()
    d.NewSolution(bool(p.get("expire_all", True)))
    ms = int((time.time() - t0) * 1000)
    problems, ok = [], 0
    for o in d.Objects:
        m = _messages(G, o)
        if m:
            row = {"id": str(o.InstanceGuid), "name": o.NickName}
            row.update(m)
            problems.append(row)
        else:
            ok += 1
    errs = sum(1 for r in problems if r.get("errors"))
    warns = sum(1 for r in problems if r.get("warnings"))
    return {"duration_ms": ms, "solution_state": str(d.SolutionState), "object_count": d.ObjectCount,
            "objects_without_messages": ok, "objects_with_errors": errs, "objects_with_warnings": warns,
            "messages": problems}


def _goo_summary(G, goo):
    if goo is None:
        return None
    tn = goo.GetType().Name
    row = {"type": goo.TypeName}
    try:
        val = goo.ScriptVariable()
    except Exception:
        val = None
    if isinstance(val, (bool, int, long, float, basestring)):
        row["value"] = val
        return row
    if isinstance(val, (rg.Point3d, rg.Vector3d)):
        row["value"] = to_jsonable(val)
        return row
    if isinstance(val, System.Drawing.Color):
        row["value"] = to_jsonable(val)
        return row
    if isinstance(val, rg.Interval):
        row["value"] = [val.T0, val.T1]
        return row
    if isinstance(val, rg.Plane):
        row["origin"] = to_jsonable(val.Origin)
        row["normal"] = to_jsonable(val.ZAxis)
        return row
    if isinstance(val, rg.Brep):
        row["is_solid"] = bool(val.IsSolid)
        row["faces"] = val.Faces.Count
    elif isinstance(val, rg.Curve):
        row["length"] = round(val.GetLength(), 4)
        row["closed"] = bool(val.IsClosed)
    elif isinstance(val, rg.Mesh):
        row["vertices"] = val.Vertices.Count
        row["faces"] = val.Faces.Count
    elif isinstance(val, rg.Surface):
        row["closed_u"] = bool(val.IsClosed(0))
    try:
        bb = goo.Boundingbox
        row["bbox"] = to_jsonable(bb)
    except Exception:
        pass
    if "value" not in row and val is None:
        row["text"] = _short(goo)
    return row


@command("gh_get_output")
def gh_get_output(p):
    """輸出（或輸入）埠目前資料的摘要。不傾印大量資料：每個分支最多 max_items 筆。"""
    gh, G, d = _ctx()
    obj = find_object(G, d, p.get("id"))
    is_input = bool(p.get("input"))
    which = p.get("input") if is_input else p.get("output")
    param, idx = find_port(G, obj, which, is_input)
    max_items = int(p.get("max_items", 10))
    max_branches = int(p.get("max_branches", 10))
    data = param.VolatileData
    branches = []
    for bi in range(min(data.PathCount, max_branches)):
        path = data.Paths[bi]
        br = data.Branches[bi]
        items = [_goo_summary(G, br[i]) for i in range(min(br.Count, max_items))]
        branches.append({"path": str(path), "count": br.Count, "items": items,
                         "truncated": br.Count > max_items})
    total_bb = None
    try:
        geo = [g for g in data.AllData(True)]
        bbs = []
        for g in geo:
            try:
                b = g.Boundingbox
                if b.IsValid:
                    bbs.append(b)
            except Exception:
                pass
        if bbs:
            u = bbs[0]
            for b in bbs[1:]:
                u = rg.BoundingBox.Union(u, b)
            total_bb = u
    except Exception:
        pass
    res = {"object": obj.NickName, "port": param.Name, "port_index": idx,
           "path_count": data.PathCount, "data_count": data.DataCount,
           "branches": branches, "branches_truncated": data.PathCount > max_branches,
           "bbox": total_bb}
    msgs = _messages(G, obj)
    if msgs:
        res["messages"] = msgs
    return res


def _to_geometry(val):
    """GH 的值型別（Box / Circle / Line ...）不是 GeometryBase，轉成可放進 Rhino 文件的幾何。"""
    if isinstance(val, rg.GeometryBase):
        return val.Duplicate()
    if isinstance(val, rg.Box):
        return val.ToBrep()
    if isinstance(val, rg.Line):
        return rg.LineCurve(val)
    for t in (rg.Circle, rg.Arc, rg.Ellipse, rg.Rectangle3d, rg.Polyline):
        if isinstance(val, t):
            return val.ToNurbsCurve()
    return None


@command("gh_bake")
def gh_bake(p):
    gh, G, d = _ctx()
    obj = find_object(G, d, p.get("id"))
    param, idx = find_port(G, obj, p.get("output"), False)
    rdoc = rhino_doc()
    attrs = make_attrs(p.get("name"), p.get("layer"), p.get("color"))
    guids, skipped = [], 0
    with UndoRecord("gh_bake"):
        for goo in list(param.VolatileData.AllData(True)):
            baked = False
            if isinstance(goo, G.Kernel.IGH_BakeAwareData):
                try:
                    ok, gid = goo.BakeGeometry(rdoc, attrs)
                    if ok and gid != System.Guid.Empty:
                        guids.append(gid)
                        baked = True
                except Exception:
                    baked = False
            if not baked:
                try:
                    val = goo.ScriptVariable()
                    geo = _to_geometry(val)
                    if geo is not None:
                        guids.append(add_geometry(geo, attrs))
                        baked = True
                    elif isinstance(val, rg.Point3d):
                        guids.append(rdoc.Objects.AddPoint(val, attrs))
                        baked = True
                except Exception:
                    pass
            if not baked:
                skipped += 1
    objs = []
    for g in guids[:50]:
        o = rdoc.Objects.FindId(g)
        if o is not None:
            from rb_common import obj_info
            objs.append(obj_info(o))
    return {"baked": len(guids), "skipped_non_geometry": skipped, "ids": [str(g) for g in guids],
            "objects": objs, "layer": p.get("layer")}


# --- 畫布截圖 / 任意 Python --------------------------------------------------------------------

@command("gh_canvas_screenshot")
def gh_canvas_screenshot(p):
    """截 Grasshopper 畫布（需要編輯器視窗存在；預設會顯示編輯器）。"""
    gh, G, d = _ctx()
    if p.get("show_editor", True) and not gh.IsEditorVisible():
        gh.ShowEditor()
    canvas = G.Instances.ActiveCanvas
    if canvas is None or not canvas.IsHandleCreated:
        raise BridgeError("畫布尚未建立（編輯器沒有顯示）。請設 show_editor=true。")
    if p.get("zoom_extents", True) and d.ObjectCount > 0:
        from System.Drawing import PointF
        l = t = r = bt = None
        for o in d.Objects:
            bd = o.Attributes.Bounds
            if bd.Width <= 0:
                continue
            l = bd.Left if l is None else min(l, bd.Left)
            t = bd.Top if t is None else min(t, bd.Top)
            r = bd.Right if r is None else max(r, bd.Right)
            bt = bd.Bottom if bt is None else max(bt, bd.Bottom)
        if l is not None:
            bw, bh = max(r - l, 1.0) + 80.0, max(bt - t, 1.0) + 80.0
            z = min(canvas.Width / bw, canvas.Height / bh, 1.5)
            canvas.Viewport.Zoom = max(z, 0.1)
            canvas.Viewport.MidPoint = PointF((l + r) / 2.0, (t + bt) / 2.0)
    canvas.Refresh()
    w, h = canvas.Width, canvas.Height
    bmp = System.Drawing.Bitmap(w, h)
    try:
        canvas.DrawToBitmap(bmp, System.Drawing.Rectangle(0, 0, w, h))
        path = p.get("path") or temp_path("gh_canvas")
        folder = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(folder):
            os.makedirs(folder)
        bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png)
    finally:
        bmp.Dispose()
    return {"path": os.path.abspath(path), "width": w, "height": h}


@command("gh_eval_python")
def gh_eval_python(p):
    """在 Rhino IronPython 執行程式碼，可直接用 Grasshopper SDK。
    可用名稱：G(Grasshopper)、ghdoc(作用中 GH 文件)、canvas、gh(外掛介面)、find(id)、
    Rhino/rg/rdo/rs/sc/System/math/json、doc(Rhino 文件)；回傳值放在 result。"""
    import rhinoscriptsyntax as rs
    import scriptcontext as sc
    import Rhino.DocObjects as rdo
    from rb_core import run_user_code, _result_summary
    gh, G, d = _ctx(create=bool(p.get("create_document", False)))
    code = need(p, "code")

    def find(ref):
        return find_object(G, d, ref)

    ns = {
        "__name__": "__rhino_agent_gh__",
        "Rhino": Rhino, "rg": rg, "rdo": rdo, "rs": rs, "sc": sc, "System": System,
        "math": math, "json": json, "doc": rhino_doc(), "result": None,
        "G": G, "gh": gh, "ghdoc": d, "canvas": G.Instances.ActiveCanvas, "find": find,
    }
    stdout = run_user_code(code, ns, "gh_eval_python")
    value = ns.get("result")
    return {"stdout": stdout, "result": _result_summary(value),
            "result_type": type(value).__name__ if value is not None else None}
