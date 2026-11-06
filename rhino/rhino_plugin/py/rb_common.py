# encoding: utf-8
# -----------------------------------------------------------------------------
# 共用工具：指令註冊、JSON 序列化、單位換算、GUID 解析、物件摘要
#
# 注意：這裡是 IronPython 2.7（不是 CPython 3）。
#   * 沒有 f-string、沒有 type hints；print 是陳述式。
#   * 所有 RhinoCommon / Grasshopper 呼叫都在 Rhino UI 執行緒執行（C# 端已派送）。
# -----------------------------------------------------------------------------
import sys
import math
import json
import os
import tempfile
import time
import System
import Rhino
import Rhino.Geometry as rg
import Rhino.DocObjects as rdo

COMMANDS = {}
STATE = {"status": {}}


def command(name):
    """註冊一個橋接器指令：@command('get_info')"""
    def deco(fn):
        COMMANDS[name] = fn
        fn.rb_name = name
        return fn
    return deco


class BridgeError(Exception):
    """可預期的使用者錯誤（參數不對、找不到物件…），不附 traceback。"""
    pass


# --- JSON ---------------------------------------------------------------------

def to_jsonable(o):
    """json.dumps 的 default：把 .NET / RhinoCommon 型別轉成純資料。"""
    if o is None:
        return None
    if isinstance(o, System.Guid):
        return str(o)
    if isinstance(o, (rg.Point3d, rg.Vector3d)):
        return [round(o.X, 6), round(o.Y, 6), round(o.Z, 6)]
    if isinstance(o, rg.Point2d):
        return [round(o.X, 6), round(o.Y, 6)]
    if isinstance(o, rg.BoundingBox):
        if not o.IsValid:
            return None
        return {"min": to_jsonable(o.Min), "max": to_jsonable(o.Max)}
    if isinstance(o, rg.Interval):
        return [o.T0, o.T1]
    if isinstance(o, System.Drawing.Color):
        return [o.R, o.G, o.B]
    if isinstance(o, System.Enum):
        return str(o)
    if isinstance(o, System.DateTime):
        return o.ToString("yyyy-MM-dd HH:mm:ss")
    if isinstance(o, System.Decimal):
        return float(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, System.Collections.IEnumerable) and not isinstance(o, basestring):
        return [x for x in o]
    try:
        return float(o)
    except Exception:
        pass
    return repr(o)


def clean_floats(o):
    """NaN / Infinity 不是合法 JSON，轉成 None。"""
    if isinstance(o, float):
        if o != o or o in (float("inf"), float("-inf")):
            return None
        return o
    if isinstance(o, dict):
        return dict((k, clean_floats(v)) for k, v in o.items())
    if isinstance(o, (list, tuple)):
        return [clean_floats(v) for v in o]
    return o


def dumps(o):
    return json.dumps(clean_floats(o), default=to_jsonable, ensure_ascii=True)


# --- 文件 / 單位 ----------------------------------------------------------------

def doc():
    d = Rhino.RhinoDoc.ActiveDoc
    if d is None:
        raise BridgeError("Rhino 目前沒有作用中的文件。")
    return d


def redraw():
    try:
        Rhino.RhinoDoc.ActiveDoc.Views.Redraw()
    except Exception:
        pass


_UNIT_MAP = {
    "mm": Rhino.UnitSystem.Millimeters,
    "millimeter": Rhino.UnitSystem.Millimeters,
    "cm": Rhino.UnitSystem.Centimeters,
    "m": Rhino.UnitSystem.Meters,
    "meter": Rhino.UnitSystem.Meters,
    "km": Rhino.UnitSystem.Kilometers,
    "in": Rhino.UnitSystem.Inches,
    "inch": Rhino.UnitSystem.Inches,
    "ft": Rhino.UnitSystem.Feet,
    "feet": Rhino.UnitSystem.Feet,
}


def unit_scale(units):
    """回傳「輸入單位 → 文件模型單位」的倍率。units 為 None / 'model' 表示直接用模型單位。"""
    if units is None:
        return 1.0
    u = str(units).strip().lower()
    if u in ("", "model", "doc", "document"):
        return 1.0
    if u not in _UNIT_MAP:
        raise BridgeError("不支援的單位 %r（可用 model/mm/cm/m/km/in/ft）。" % (units,))
    return Rhino.RhinoMath.UnitScale(_UNIT_MAP[u], doc().ModelUnitSystem)


def tol():
    return doc().ModelAbsoluteTolerance


def pt(v, scale=1.0, name="座標"):
    """[x,y,z]（或 [x,y]）→ Point3d，並乘上單位倍率。"""
    if v is None:
        raise BridgeError("缺少 %s。" % name)
    try:
        vals = [float(x) for x in v]
    except Exception:
        raise BridgeError("%s 必須是數字陣列 [x,y,z]，收到 %r。" % (name, v))
    if len(vals) == 2:
        vals.append(0.0)
    if len(vals) != 3:
        raise BridgeError("%s 必須是 [x,y,z]，收到 %r。" % (name, v))
    return rg.Point3d(vals[0] * scale, vals[1] * scale, vals[2] * scale)


def vec(v, scale=1.0, name="向量"):
    p = pt(v, scale, name)
    return rg.Vector3d(p.X, p.Y, p.Z)


def num(params, key, default=None, required=False):
    v = params.get(key, None)
    if v is None:
        if required:
            raise BridgeError("缺少必要參數 %s。" % key)
        return default
    try:
        return float(v)
    except Exception:
        raise BridgeError("參數 %s 必須是數字，收到 %r。" % (key, v))


def need(params, key):
    v = params.get(key, None)
    if v is None:
        raise BridgeError("缺少必要參數 %s。" % key)
    return v


def parse_guid(s):
    try:
        return System.Guid.Parse(str(s).strip().strip("{}"))
    except Exception:
        raise BridgeError("不是合法的 GUID：%r" % (s,))


def parse_guids(values, name="ids"):
    if values is None:
        raise BridgeError("缺少必要參數 %s（GUID 陣列）。" % name)
    if isinstance(values, basestring):
        values = [v for v in values.replace(";", ",").split(",") if v.strip()]
    return [parse_guid(v) for v in values]


def get_objects(ids):
    """GUID 字串陣列 → RhinoObject 清單；找不到的 id 一次全部報出來。"""
    d = doc()
    found, missing = [], []
    for g in parse_guids(ids):
        o = d.Objects.FindId(g)
        if o is None:
            missing.append(str(g))
        else:
            found.append(o)
    if missing:
        raise BridgeError("找不到這些物件（可能已刪除，或不在目前文件）：%s" % ", ".join(missing))
    return found


def parse_color(c):
    """'#RRGGBB' / 'red' / [r,g,b] → System.Drawing.Color"""
    if c is None:
        return None
    if isinstance(c, basestring):
        s = c.strip()
        if s.startswith("#"):
            s = s[1:]
            if len(s) == 3:
                s = "".join([ch * 2 for ch in s])
            if len(s) != 6:
                raise BridgeError("顏色 %r 不是 #RRGGBB。" % (c,))
            return System.Drawing.Color.FromArgb(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        col = System.Drawing.Color.FromName(s)
        if col.A == 0 and s.lower() != "transparent":
            raise BridgeError("無法辨識的顏色名稱 %r，請用 #RRGGBB 或 [r,g,b]。" % (c,))
        return col
    try:
        r, g, b = [int(x) for x in c][:3]
        return System.Drawing.Color.FromArgb(r, g, b)
    except Exception:
        raise BridgeError("顏色必須是 '#RRGGBB' 或 [r,g,b]（0-255），收到 %r。" % (c,))


# --- 物件摘要 ---------------------------------------------------------------------

def layer_path(o):
    try:
        return doc().Layers[o.Attributes.LayerIndex].FullPath
    except Exception:
        return None


def obj_type(o):
    t = str(o.ObjectType)
    g = o.Geometry
    if isinstance(g, rg.Brep) and g.IsSolid:
        return "Brep(solid)"
    return t


def obj_info(o, detail=True):
    a = o.Attributes
    info = {
        "id": str(o.Id),
        "type": obj_type(o),
        "geometry": o.Geometry.GetType().Name if o.Geometry is not None else None,
        "name": a.Name or "",
        "layer": layer_path(o),
    }
    if detail:
        bb = o.Geometry.GetBoundingBox(True) if o.Geometry is not None else rg.BoundingBox.Unset
        info["bbox"] = bb
        info["visible"] = bool(o.Visible)
        info["locked"] = bool(o.IsLocked)
        info["selected"] = bool(o.IsSelected(False) != 0)
        try:
            info["color"] = a.ObjectColor if a.ColorSource == rdo.ObjectColorSource.ColorFromObject else None
        except Exception:
            pass
    return info


def make_attrs(name=None, layer=None, color=None):
    """建立新物件用的 ObjectAttributes；layer 不存在會自動建立。"""
    d = doc()
    attrs = d.CreateDefaultAttributes()
    if name:
        attrs.Name = name
    if layer:
        attrs.LayerIndex = ensure_layer(layer)
    if color is not None:
        attrs.ObjectColor = parse_color(color)
        attrs.ColorSource = rdo.ObjectColorSource.ColorFromObject
    return attrs


def ensure_layer(path, color=None):
    """依完整路徑（'Parent::Child'）取得圖層索引，不存在就建立（含父層）。"""
    d = doc()
    path = str(path).strip()
    if not path:
        raise BridgeError("圖層名稱不能為空。")
    idx = d.Layers.FindByFullPath(path, -1)
    if idx >= 0:
        return idx
    parent_id = System.Guid.Empty
    built = []
    idx = -1
    for part in path.split("::"):
        built.append(part)
        full = "::".join(built)
        idx = d.Layers.FindByFullPath(full, -1)
        if idx < 0:
            lyr = rdo.Layer()
            lyr.Name = part
            if parent_id != System.Guid.Empty:
                lyr.ParentLayerId = parent_id
            if color is not None and full == path:
                lyr.Color = parse_color(color)
            idx = d.Layers.Add(lyr)
            if idx < 0:
                raise BridgeError("無法建立圖層 %r（名稱重複或不合法）。" % full)
        parent_id = d.Layers[idx].Id
    return idx


def add_geometry(geo, attrs):
    """把 GeometryBase 加進文件並回傳 GUID。"""
    d = doc()
    if isinstance(geo, rg.Brep):
        gid = d.Objects.AddBrep(geo, attrs)
    elif isinstance(geo, rg.Curve):
        gid = d.Objects.AddCurve(geo, attrs)
    elif isinstance(geo, rg.Surface):
        gid = d.Objects.AddSurface(geo, attrs)
    elif isinstance(geo, rg.Mesh):
        gid = d.Objects.AddMesh(geo, attrs)
    elif isinstance(geo, rg.Extrusion):
        gid = d.Objects.AddExtrusion(geo, attrs)
    elif isinstance(geo, rg.Point):
        gid = d.Objects.AddPoint(geo.Location, attrs)
    else:
        gid = d.Objects.Add(geo, attrs)
    if gid == System.Guid.Empty:
        raise BridgeError("Rhino 拒絕加入這個幾何（可能是無效幾何）。")
    return gid


class UndoRecord(object):
    """with UndoRecord('名稱'): ... 讓一次 Ctrl+Z 能整批復原。

    巢狀使用時只有最外層會開 record。若 BeginUndoRecord 在最外層回傳 0，代表
    有一筆「沒人收尾」的舊 record 一直開著（之後所有操作都會被吞進去、永遠 undo 不到），
    這裡會先把它結束再重開——橋接器不處理使用者互動指令，所以不可能誤傷正在進行的操作。
    """

    def __init__(self, name):
        self.name = name
        self.sn = 0

    def __enter__(self):
        self.doc = doc()
        depth = STATE.get("undo_depth", 0)
        if depth == 0:
            sn = self.doc.BeginUndoRecord("Agent: " + self.name)
            if not sn:
                try:
                    self.doc.EndUndoRecord(self.doc.CurrentUndoRecordSerialNumber)
                except Exception:
                    pass
                sn = self.doc.BeginUndoRecord("Agent: " + self.name)
            self.sn = sn
        STATE["undo_depth"] = depth + 1
        return self

    def __exit__(self, et, ev, tb):
        STATE["undo_depth"] = max(0, STATE.get("undo_depth", 1) - 1)
        try:
            if self.sn:
                self.doc.EndUndoRecord(self.sn)
        finally:
            redraw()
        return False


def bbox_of(objs):
    bb = rg.BoundingBox.Unset
    for o in objs:
        g = o.Geometry
        if g is None:
            continue
        b = g.GetBoundingBox(True)
        if b.IsValid:
            bb = b if not bb.IsValid else rg.BoundingBox.Union(bb, b)
    return bb


def temp_dir():
    p = os.path.join(tempfile.gettempdir(), "rhino_agent_bridge")
    if not os.path.isdir(p):
        os.makedirs(p)
    return p


def temp_path(prefix, ext=".png"):
    return os.path.join(temp_dir(), "%s_%s_%03d%s" % (
        prefix, time.strftime("%Y%m%d_%H%M%S"), int(time.time() * 1000) % 1000, ext))
