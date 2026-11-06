# encoding: utf-8
"""Rhino 7 + Grasshopper Agent Bridge 的 MCP server（stdio transport）。

只是 client.py 的一層薄殼：所有協定邏輯都在 client.py，這裡負責
把指令包成 MCP tools、把截圖包成 image content。

啟動方式：
    python -m rhino_bridge.mcp_server
環境變數：
    RHINO_BRIDGE_HOST      預設 127.0.0.1
    RHINO_BRIDGE_PORT      預設 9878
    RHINO_BRIDGE_TIMEOUT   預設 30（秒）
    RHINO_BRIDGE_MAX_EDGE  預設 1280（回傳給模型的圖片最長邊像素）
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

try:
    # mcp SDK 2.x：FastMCP 已改名為 MCPServer
    from mcp.server.mcpserver import Image, MCPServer as _ServerClass
except ImportError:  # pragma: no cover - 相容 mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _ServerClass, Image

from .client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    RhinoClient,
    RhinoCommandError,
    RhinoConnectionError,
)
from . import images as image_utils
from . import launcher
from . import window_capture

HOST = os.environ.get("RHINO_BRIDGE_HOST", DEFAULT_HOST)
PORT = int(os.environ.get("RHINO_BRIDGE_PORT", DEFAULT_PORT))
TIMEOUT = float(os.environ.get("RHINO_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT))
MAX_EDGE = int(os.environ.get("RHINO_BRIDGE_MAX_EDGE", image_utils.DEFAULT_MAX_EDGE))

mcp = _ServerClass(
    "rhino-bridge",
    instructions=(
        "Control the Rhino 7 instance on this machine, including Grasshopper: inspect the model, "
        "run IronPython (RhinoCommon), model, set views, take screenshots, and build/solve/bake "
        "Grasshopper definitions. Tools are prefixed rhino_ and gh_. "
        "前提：Rhino 7 已開啟且已安裝 RhinoAgentBridge 外掛（Rhino 啟動時自動開伺服器）。"
        "若工具回傳 RHINO_NOT_AVAILABLE，可呼叫 rhino_launch 啟動 Rhino；"
        "若 Rhino 被授權 / 登入 / 更新對話框卡住，請告知使用者自行處理，不要強制結束 Rhino。"
    ),
)

_client: Optional[RhinoClient] = None


def _get_client() -> RhinoClient:
    global _client
    if _client is None:
        _client = RhinoClient(host=HOST, port=PORT, timeout=TIMEOUT)
    return _client


def _call(method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
    """呼叫 Rhino；連線中斷時自動重連一次。"""
    client = _get_client()
    try:
        return client.call(method, params, timeout)
    except RhinoConnectionError:
        client.close()
        return client.call(method, params, timeout)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _run(method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> str:
    """執行指令並把結果序列化成 JSON 字串；錯誤轉成人看得懂的說明。"""
    try:
        return _dump(_call(method, params, timeout))
    except RhinoConnectionError as exc:
        return f"RHINO_NOT_AVAILABLE\n{exc}"
    except RhinoCommandError as exc:
        return f"RHINO_COMMAND_FAILED\n{exc}"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _slow() -> float:
    return max(TIMEOUT, 60.0)


def _image_result(path: str) -> Any:
    try:
        with open(path, "rb") as handle:
            image = image_utils.load_image(handle.read())
    except OSError as exc:
        return f"RHINO_COMMAND_FAILED\n截圖檔案無法讀取：{path}（{exc}）"
    data, mime = image_utils.encode_for_model(image, MAX_EDGE)
    return Image(data=data, format="png" if mime == "image/png" else "jpeg")


# --- 1. 基本資訊 -----------------------------------------------------------


@mcp.tool()
def rhino_get_info() -> str:
    """Check the bridge connection and get Rhino basics (also serves as ping).

    取得 Rhino 版本、目前檔案、模型單位與公差、選取數量、橋接器狀態、是否有指令執行中。
    Always call this first to confirm the bridge is reachable.
    """
    return _run("get_info")


@mcp.tool()
def rhino_get_scene_info(limit: int = 200) -> str:
    """Summarize the model: object counts by type, layers, bounding box, units, views.

    大模型會自動截斷（limit 控制每類清單上限）。Lengths are in model units (see rhino_get_info).
    """
    return _run("get_scene_info", _clean({"limit": limit}), timeout=_slow())


@mcp.tool()
def rhino_list_objects(
    types: Optional[List[str]] = None,
    layer: Optional[str] = None,
    name_contains: Optional[str] = None,
    selected_only: bool = False,
    include_hidden: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """List objects with GUID, type, name, layer, colour and bounding box (paged).

    types: filter, e.g. ["brep","curve","mesh","point","extrusion","surface","instance"].
    layer: full layer path, e.g. "Parent::Child". GUID 是後續操作物件的唯一識別碼。
    """
    return _run(
        "list_objects",
        _clean({"types": types, "layer": layer, "name_contains": name_contains,
                "selected_only": selected_only or None, "include_hidden": include_hidden or None,
                "limit": limit, "offset": offset}),
        timeout=_slow(),
    )


@mcp.tool()
def rhino_select_objects(ids: Optional[List[str]] = None, clear: bool = False, add: bool = False) -> str:
    """Set or clear the Rhino selection by GUID. add=true 表示加入而非取代。"""
    return _run("select_objects", _clean({"ids": ids, "clear": clear or None, "add": add or None}))


@mcp.tool()
def rhino_delete_objects(ids: List[str]) -> str:
    """Delete objects by GUID (one undoable record).

    破壞性操作：先用 rhino_list_objects 確認 GUID，不要刪除使用者既有的幾何。
    """
    return _run("delete_objects", {"ids": ids})


# --- 2. 截圖 ---------------------------------------------------------------


@mcp.tool(structured_output=False)
def rhino_viewport_screenshot(
    viewport: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    axes: Optional[bool] = None,
    grid: Optional[bool] = None,
    transparent: bool = False,
) -> Any:
    """Capture a Rhino viewport to a PNG image (ViewCapture, the displayed mode).

    這是驗證建模結果的主要方式：改完模型就截圖確認（先 rhino_set_view zoom_extents）。
    viewport: 視埠名稱（預設作用中視埠）。width/height 預設為視埠大小。
    """
    try:
        result = _call(
            "screenshot",
            _clean({"viewport": viewport, "width": width, "height": height, "axes": axes,
                    "grid": grid, "transparent": transparent or None}),
            timeout=_slow(),
        )
    except RhinoConnectionError as exc:
        return f"RHINO_NOT_AVAILABLE\n{exc}"
    except RhinoCommandError as exc:
        return f"RHINO_COMMAND_FAILED\n{exc}"
    return _image_result(result.get("path"))


@mcp.tool(structured_output=False)
def rhino_window_screenshot(
    target: str = "rhino", max_edge: Optional[int] = None, restore_if_minimized: bool = False
) -> Any:
    """Capture a whole application window at the OS level (PrintWindow; view-only, no input simulation).

    target: "rhino" (main window, incl. command line / panels / dialogs) or "grasshopper" (GH editor).
    當需要看工具列、開著的對話框、命令列提示，或視埠截圖看不到的 UI 時使用。
    """
    try:
        if str(target).lower().startswith("g"):
            wins = window_capture.find_windows(process_name="Rhino.exe", title_contains="Grasshopper")
            if not wins:
                return "RHINO_WINDOW_NOT_FOUND\n找不到 Grasshopper 編輯器視窗（先呼叫 gh_open_editor）。"
            captured = window_capture.capture_window(
                hwnd=wins[0]["hwnd"], restore_if_minimized=restore_if_minimized)
        else:
            captured = window_capture.capture_window(restore_if_minimized=restore_if_minimized)
    except window_capture.WindowCaptureError as exc:
        return f"RHINO_WINDOW_NOT_FOUND\n{exc}"
    except ImportError as exc:
        return f"MISSING_DEPENDENCY\n需要 Pillow 才能做全視窗截圖：{exc}"
    data, mime = image_utils.encode_for_model(captured["image"], max_edge or MAX_EDGE)
    return Image(data=data, format="png" if mime == "image/png" else "jpeg")


# --- 3. 視圖 ---------------------------------------------------------------


@mcp.tool()
def rhino_set_view(
    view: Optional[str] = None,
    display_mode: Optional[str] = None,
    eye: Optional[List[float]] = None,
    target: Optional[List[float]] = None,
    projection: Optional[str] = None,
    zoom_extents: bool = False,
    selected_only: bool = False,
    maximize: Optional[bool] = None,
    viewport: Optional[str] = None,
    units: Optional[str] = None,
) -> str:
    """Set a viewport: standard view, display mode, camera, zoom extents.

    view: top/bottom/front/back/left/right/perspective/iso.
    display_mode: e.g. "Wireframe", "Shaded", "Rendered", "Ghosted", "Arctic".
    eye/target: [x,y,z]（units，預設模型單位）。projection: "perspective" | "parallel".
    zoom_extents=true 會把整個模型（或 selected_only 的選取物）帶進畫面——截圖前常用。
    """
    return _run(
        "set_view",
        _clean({"view": view, "display_mode": display_mode, "eye": eye, "target": target,
                "projection": projection, "zoom_extents": zoom_extents or None,
                "selected_only": selected_only or None, "maximize": maximize,
                "viewport": viewport, "units": units}),
    )


# --- 4. 建模捷徑（name / layer / color 皆為選填）--------------------------------
# layer 用完整路徑 'Parent::Child'，不存在會自動建立；color 為 '#RRGGBB' 或 [r,g,b]。


@mcp.tool()
def rhino_create_box(
    width: float, depth: float, height: float,
    origin: Optional[List[float]] = None, centered: bool = False, units: Optional[str] = None,
    name: Optional[str] = None, layer: Optional[str] = None, color: Optional[Any] = None,
) -> str:
    """Create a box (Brep). width=X, depth=Y, height=Z (+Z up from origin).

    units: mm/cm/m/km/in/ft；省略 = 模型單位。centered=true 讓 origin 在底面中心。
    name / layer('Parent::Child', auto-created) / color('#RRGGBB' or [r,g,b]) optional.
    """
    return _run("create_box", _clean({"width": width, "depth": depth, "height": height, "origin": origin,
                                      "centered": centered or None, "units": units, "name": name,
                                      "layer": layer, "color": color}))


@mcp.tool()
def rhino_create_sphere(
    radius: float, center: Optional[List[float]] = None, units: Optional[str] = None,
    name: Optional[str] = None, layer: Optional[str] = None, color: Optional[Any] = None,
) -> str:
    """Create a sphere (Brep). 單位同 rhino_create_box；name / layer / color optional."""
    return _run("create_sphere", _clean({"radius": radius, "center": center, "units": units,
                                         "name": name, "layer": layer, "color": color}))


@mcp.tool()
def rhino_create_cylinder(
    radius: float, height: float, base: Optional[List[float]] = None, axis: Optional[List[float]] = None,
    cap: bool = True, units: Optional[str] = None,
    name: Optional[str] = None, layer: Optional[str] = None, color: Optional[Any] = None,
) -> str:
    """Create a cylinder (Brep). base = bottom centre; axis defaults to +Z; cap=false gives an open tube.

    name / layer / color optional.
    """
    return _run("create_cylinder", _clean({"radius": radius, "height": height, "base": base, "axis": axis,
                                           "cap": cap, "units": units, "name": name, "layer": layer,
                                           "color": color}))


@mcp.tool()
def rhino_create_circle(
    radius: float, center: Optional[List[float]] = None, normal: Optional[List[float]] = None,
    units: Optional[str] = None, name: Optional[str] = None, layer: Optional[str] = None,
    color: Optional[Any] = None,
) -> str:
    """Create a circle curve (default on the XY plane). 可再用 rhino_extrude 擠成圓柱。

    name / layer / color optional.
    """
    return _run("create_circle", _clean({"radius": radius, "center": center, "normal": normal,
                                         "units": units, "name": name, "layer": layer, "color": color}))


@mcp.tool()
def rhino_create_polyline(
    points: List[List[float]], closed: bool = False, units: Optional[str] = None,
    name: Optional[str] = None, layer: Optional[str] = None, color: Optional[Any] = None,
) -> str:
    """Create a polyline from [[x,y,z],...]. closed=true 自動封閉（封閉平面曲線可 extrude 成實體）。

    name / layer / color optional.
    """
    return _run("create_polyline", _clean({"points": points, "closed": closed or None, "units": units,
                                           "name": name, "layer": layer, "color": color}))


@mcp.tool()
def rhino_create_curve(
    points: List[List[float]], degree: int = 3, mode: str = "control", closed: bool = False,
    units: Optional[str] = None, name: Optional[str] = None, layer: Optional[str] = None,
    color: Optional[Any] = None,
) -> str:
    """Create a NURBS curve. mode: "control" (control points) or "interpolate" (passes through points).

    name / layer / color optional.
    """
    return _run("create_curve", _clean({"points": points, "degree": degree, "mode": mode,
                                        "closed": closed or None, "units": units, "name": name,
                                        "layer": layer, "color": color}))


@mcp.tool()
def rhino_extrude(
    ids: List[str], height: float, direction: Optional[List[float]] = None, cap: bool = True,
    delete_input: bool = False, units: Optional[str] = None,
    name: Optional[str] = None, layer: Optional[str] = None, color: Optional[Any] = None,
) -> str:
    """Extrude curves (or surfaces) by `height` along direction (default +Z).

    cap=true 會把封閉平面曲線封成實體。delete_input=true 刪除原曲線。name / layer / color optional.
    """
    return _run("extrude", _clean({"ids": ids, "height": height, "direction": direction, "cap": cap,
                                   "delete_input": delete_input or None, "units": units, "name": name,
                                   "layer": layer, "color": color}))


@mcp.tool()
def rhino_boolean(operation: str, a: List[str], b: List[str], delete_inputs: bool = True) -> str:
    """Boolean on closed Breps. operation: "union" | "difference" (a minus b) | "intersection".

    a / b: GUID 陣列。delete_inputs=false 保留原物件。結果不如預期時用 rhino_undo_redo 還原。
    """
    return _run("boolean", {"operation": operation, "a": a, "b": b, "delete_inputs": delete_inputs})


@mcp.tool()
def rhino_loft(ids: List[str], loft_type: str = "normal", closed: bool = False, cap: bool = False,
               delete_input: bool = False) -> str:
    """Loft through curves in order. loft_type: normal/loose/tight/straight/uniform.

    cap=true 在兩端封口（需封閉曲線）。
    """
    return _run("loft", _clean({"ids": ids, "loft_type": loft_type, "closed": closed or None,
                                "cap": cap or None, "delete_input": delete_input or None}))


@mcp.tool()
def rhino_transform_objects(
    ids: List[str],
    translate: Optional[List[float]] = None,
    rotate: Optional[Dict[str, Any]] = None,
    scale: Optional[Any] = None,
    scale_origin: Optional[List[float]] = None,
    mirror: Optional[Dict[str, Any]] = None,
    units: Optional[str] = None,
    copy: bool = False,
) -> str:
    """Move / rotate / scale / mirror objects by GUID (one undoable record).

    translate: [dx,dy,dz]. rotate: {"origin":[x,y,z],"axis":[x,y,z],"angle_deg":45}.
    scale: number or [sx,sy,sz] about scale_origin (default [0,0,0]). copy=true 保留原物件。
    """
    return _run("transform_objects", _clean({"ids": ids, "translate": translate, "rotate": rotate,
                                             "scale": scale, "scale_origin": scale_origin, "mirror": mirror,
                                             "units": units, "copy": copy or None}))


@mcp.tool()
def rhino_set_layer(
    layer: str, ids: Optional[List[str]] = None, create: bool = True, color: Optional[Any] = None,
    visible: Optional[bool] = None, locked: Optional[bool] = None, current: bool = False,
) -> str:
    """Move objects to a layer (full path 'Parent::Child') and/or edit the layer.

    ids 省略 = 只編輯圖層本身。create=true 不存在就建立。current=true 設為目前圖層。
    """
    return _run("set_layer", _clean({"layer": layer, "ids": ids, "create": create, "color": color,
                                     "visible": visible, "locked": locked, "current": current or None}))


@mcp.tool()
def rhino_set_material(
    ids: List[str], color: Optional[Any] = None, material: Optional[str] = None,
    transparency: Optional[float] = None,
) -> str:
    """Set object colour / simple material.

    color: "#RRGGBB" or [r,g,b]. material: 材質名稱（不存在會建立）。transparency: 0.0-1.0。
    """
    return _run("set_material", _clean({"ids": ids, "color": color, "material": material,
                                        "transparency": transparency}))


# --- 5. 任意 Python / 指令 --------------------------------------------------


@mcp.tool()
def rhino_eval_python(code: str, timeout_seconds: Optional[float] = None) -> str:
    """Run arbitrary IronPython 2.7 on Rhino's UI thread (full RhinoCommon / rhinoscriptsyntax).

    預設名稱：Rhino, rg(Rhino.Geometry), rdo(Rhino.DocObjects), rs(rhinoscriptsyntax), sc, System,
    math, json, doc(目前文件)。把要回傳的值放在變數 result；print 的輸出會一併回傳。
    整段程式是一個 undo record。注意：這是 Python 2.7 語法（不支援 f-string / 型別註記；
    中文字串用 u'' 或檔頭 # encoding: utf-8）；單位是文件單位（見 rhino_get_info），不是公釐。
    """
    return _run("eval_python", {"code": code}, timeout=timeout_seconds or _slow())


@mcp.tool()
def rhino_run_command(command: Any, echo: bool = False, stop_on_error: bool = True,
                      timeout_seconds: Optional[float] = None) -> str:
    """Run Rhino command-line script(s), e.g. "_-Box 0,0,0 10,10,10 5" (use the '-' prefixed, dialog-free forms).

    command: 字串或字串陣列。回傳是否成功、新增物件 GUID、命令列輸出。
    若指令停在提示等待輸入，用 rhino_cancel_command 結束（how: escape | enter）。
    """
    return _run("run_command", _clean({"command": command, "echo": echo or None,
                                       "stop_on_error": stop_on_error}),
                timeout=timeout_seconds or _slow())


@mcp.tool()
def rhino_cancel_command(how: str = "escape") -> str:
    """Cancel / finish a command stuck at a prompt. how: "escape" (cancel) or "enter" (accept).

    注意：無法關閉跳出的對話框（模態視窗），那種情況請使用者自己處理。
    """
    return _run("cancel_command", {"how": how})


# --- 6. 復原 / 檔案 / 啟動 --------------------------------------------------


@mcp.tool()
def rhino_undo_redo(direction: str = "undo", times: int = 1) -> str:
    """Undo or redo in Rhino. direction: "undo" | "redo".

    每個橋接器的修改指令都是一個 undo record，一次 undo 剛好回復一步。
    發現結果不對時先 undo 再重做，不要疊加錯誤幾何。
    """
    method = "undo" if str(direction).lower() != "redo" else "redo"
    return _run(method, {"times": times})


@mcp.tool()
def rhino_model_file(
    action: str,
    path: Optional[str] = None,
    confirm: bool = False,
    overwrite: bool = False,
    selected_only: bool = False,
    template: Optional[str] = None,
) -> str:
    """File operations. action: "new" | "open" | "save" | "save_as" | "export".

    安全規則（不可繞過）：new / open 需要 confirm=true（有未存變更時尤其要先問使用者）；
    save 需要 confirm=true；save_as / export 覆寫既有檔案需要 overwrite=true。
    export 依副檔名決定格式（.stl .obj .step .igs .3dm .dxf ...），selected_only 只匯出選取物件。
    未經使用者明確同意，不要儲存或覆蓋使用者的檔案。
    """
    action = str(action).lower()
    if action not in ("new", "open", "save", "save_as", "export"):
        return "RHINO_COMMAND_FAILED\naction 必須是 new / open / save / save_as / export。"
    if action == "export":
        return _run("export_model", _clean({"path": path, "overwrite": overwrite or None,
                                            "selected_only": selected_only or None}), timeout=_slow() * 2)
    if action == "save_as" and not path:
        return "RHINO_COMMAND_FAILED\nsave_as 需要 path。"
    method = {"new": "new_model", "open": "open_model", "save": "save_model", "save_as": "save_model"}[action]
    return _run(method, _clean({"path": path, "confirm": confirm or None, "overwrite": overwrite or None,
                                "template": template}), timeout=_slow() * 2)


@mcp.tool()
def rhino_launch(path: Optional[str] = None, wait_seconds: float = 90.0) -> str:
    """Start Rhino 7 if it is not running (optionally opening a .3dm) and wait until the bridge is ready.

    Rhino 已在執行就只回報狀態。只會啟動、永遠不會關閉或強制結束 Rhino。
    若 ready=false 且列出視窗標題，多半是授權 / 登入 / 更新對話框擋住——請告知使用者自行處理。
    """
    try:
        return _dump(launcher.launch(path=path, wait_seconds=wait_seconds, host=HOST, port=PORT))
    except Exception as exc:  # pragma: no cover - 防禦
        return f"RHINO_COMMAND_FAILED\n{type(exc).__name__}: {exc}"


# --- 7. Grasshopper --------------------------------------------------------
# 命名規則：元件 / 埠用 Name 或 NickName（不分大小寫）或索引指定；物件用 instance GUID。


@mcp.tool()
def gh_status() -> str:
    """Grasshopper status: version, editor loaded/visible, solver on/off, active document summary."""
    return _run("gh_status", timeout=max(TIMEOUT, 90.0))


@mcp.tool()
def gh_open_editor(show: bool = True) -> str:
    """Load the Grasshopper editor (first time takes seconds) and show (or hide) its window."""
    return _run("gh_open_editor", {"show": show}, timeout=max(TIMEOUT, 90.0))


@mcp.tool()
def gh_new_document(show: bool = False) -> str:
    """Create a new empty Grasshopper document and make it active (the old one stays open, unsaved)."""
    return _run("gh_new_document", {"show": show}, timeout=max(TIMEOUT, 90.0))


@mcp.tool()
def gh_open_document(path: str, show: bool = False) -> str:
    """Open a .gh / .ghx file as the active document. 需要使用者同意開啟該檔。"""
    return _run("gh_open_document", {"path": path, "show": show}, timeout=max(TIMEOUT, 90.0))


@mcp.tool()
def gh_save_document(path: Optional[str] = None, confirm: bool = False, overwrite: bool = False) -> str:
    """Save the active GH document (.gh / .ghx). 必須 confirm=true；另存到已存在的檔案需 overwrite=true。

    未經使用者明確同意，不要儲存或覆蓋檔案。
    """
    return _run("gh_save_document", _clean({"path": path, "confirm": confirm or None,
                                            "overwrite": overwrite or None}))


@mcp.tool()
def gh_search_components(query: str, category: Optional[str] = None, subcategory: Optional[str] = None,
                         limit: int = 15, with_ports: bool = True) -> str:
    """Search Grasshopper components by name / nickname / description.

    回傳 guid、名稱、分類、以及輸入輸出埠（名稱 / 暱稱 / 型別 / 是否選用）。
    同名常有多個（例如 Circle 元件與 Circle 參數）：看 category/subcategory，或之後直接用 guid 放置。
    """
    return _run("gh_search_components", _clean({"query": query, "category": category,
                                               "subcategory": subcategory, "limit": limit,
                                               "with_ports": with_ports}), timeout=_slow())


@mcp.tool()
def gh_add_component(name: Optional[str] = None, guid: Optional[str] = None, category: Optional[str] = None,
                     subcategory: Optional[str] = None, x: Optional[float] = None, y: Optional[float] = None,
                     nickname: Optional[str] = None) -> str:
    """Place a component on the active canvas (by exact name, or by guid from gh_search_components).

    名稱有歧義時會回傳候選清單，請加 category/subcategory 或改用 guid。
    x/y 是畫布座標（省略則自動排在現有物件右側）。回傳含 instance id 與完整輸入輸出埠清單。
    """
    return _run("gh_add_component", _clean({"name": name, "guid": guid, "category": category,
                                            "subcategory": subcategory, "x": x, "y": y,
                                            "nickname": nickname}), timeout=_slow())


@mcp.tool()
def gh_add_slider(min: float, max: float, value: Optional[float] = None, integer: bool = False,
                  decimals: int = 2, nickname: Optional[str] = None,
                  x: Optional[float] = None, y: Optional[float] = None) -> str:
    """Add a Number Slider (min/max/value; integer=true gives an integer slider)."""
    return _run("gh_add_slider", _clean({"min": min, "max": max, "value": value, "integer": integer or None,
                                         "decimals": decimals, "nickname": nickname, "x": x, "y": y}))


@mcp.tool()
def gh_add_panel(text: Optional[str] = None, nickname: Optional[str] = None,
                 x: Optional[float] = None, y: Optional[float] = None,
                 width: Optional[float] = None, height: Optional[float] = None) -> str:
    """Add a Panel (text / string / numeric input). 輸出就是面板文字（多行 = 多筆資料）。"""
    return _run("gh_add_panel", _clean({"text": text, "nickname": nickname, "x": x, "y": y,
                                        "width": width, "height": height}))


@mcp.tool()
def gh_add_toggle(value: bool = False, nickname: Optional[str] = None,
                  x: Optional[float] = None, y: Optional[float] = None) -> str:
    """Add a Boolean Toggle."""
    return _run("gh_add_toggle", _clean({"value": value, "nickname": nickname, "x": x, "y": y}))


@mcp.tool()
def gh_set_input(id: str, value: Any, input: Optional[Any] = None, flatten: Optional[bool] = None,
                 graft: Optional[bool] = None, reverse: Optional[bool] = None,
                 simplify: Optional[bool] = None) -> str:
    """Set a value on a GH object.

    對 slider / panel / toggle：直接設定其值（slider 超出範圍會報錯）。
    對一般元件：設定輸入埠 `input`（名稱 / 暱稱 / 索引）的 persistent data（沒連線時使用）。
    value 可為數字 / 字串 / 布林 / 點 [x,y,z]（Point / Vector 埠）/ [a,b]（Interval 埠）/ 顏色 /
    {"rhino_id": GUID}（引用 Rhino 物件）/ 陣列（多筆資料）；null 清除。
    flatten / graft / reverse / simplify 設定資料樹處理。
    """
    return _run("gh_set_input", _clean({"id": id, "value": value, "input": input, "flatten": flatten,
                                        "graft": graft, "reverse": reverse, "simplify": simplify}))


@mcp.tool()
def gh_connect(source: str, target: str, target_input: Optional[Any] = None,
               source_output: Optional[Any] = None, replace: bool = False) -> str:
    """Wire source output -> target input. 物件用 instance id；埠用名稱 / 暱稱 / 索引。

    單一輸出的物件（slider、panel、參數）可省略 source_output。replace=true 先清掉該輸入原有連線。
    """
    return _run("gh_connect", _clean({"source": source, "target": target, "target_input": target_input,
                                      "source_output": source_output, "replace": replace or None}))


@mcp.tool()
def gh_disconnect(target: str, target_input: Optional[Any] = None, source: Optional[str] = None,
                  source_output: Optional[Any] = None) -> str:
    """Remove wires into target input (all of them, or only those from `source`)."""
    return _run("gh_disconnect", _clean({"target": target, "target_input": target_input, "source": source,
                                         "source_output": source_output}))


@mcp.tool()
def gh_list_document() -> str:
    """List every object in the active GH document with ports, wires and runtime errors/warnings."""
    return _run("gh_list_document", timeout=_slow())


@mcp.tool()
def gh_solve(expire_all: bool = True) -> str:
    """Recompute the definition (synchronously) and return per-object errors / warnings."""
    return _run("gh_solve", {"expire_all": expire_all}, timeout=max(TIMEOUT, 120.0))


@mcp.tool()
def gh_get_output(id: str, output: Optional[Any] = None, input: Optional[Any] = None,
                  max_items: int = 10, max_branches: int = 10) -> str:
    """Read the data at an output port (or an input port if `input` is given) after gh_solve.

    回傳每個分支的路徑、筆數與每筆摘要（數字 / 點 / Brep 面數與是否實體 / 曲線長度 ... 與 bbox），
    不會傾印整個幾何。
    """
    return _run("gh_get_output", _clean({"id": id, "output": output, "input": input,
                                         "max_items": max_items, "max_branches": max_branches}))


@mcp.tool()
def gh_bake(id: str, output: Optional[Any] = None, layer: Optional[str] = None,
            name: Optional[str] = None, color: Optional[Any] = None) -> str:
    """Bake an output port's geometry into the Rhino document (one undo record). Returns new GUIDs."""
    return _run("gh_bake", _clean({"id": id, "output": output, "layer": layer, "name": name,
                                   "color": color}), timeout=_slow())


@mcp.tool()
def gh_delete_objects(ids: List[str]) -> str:
    """Delete GH objects (instance ids) from the active document."""
    return _run("gh_delete_objects", {"ids": ids})


@mcp.tool(structured_output=False)
def gh_canvas_screenshot(show_editor: bool = True, zoom_extents: bool = True) -> Any:
    """Capture the Grasshopper canvas (editor is shown first if needed) to an image.

    用來確認元件、連線與錯誤標示（紅 / 橘元件）。想看整個 GH 視窗可用
    rhino_window_screenshot target="grasshopper"。
    """
    try:
        result = _call("gh_canvas_screenshot", {"show_editor": show_editor, "zoom_extents": zoom_extents},
                       timeout=_slow())
    except RhinoConnectionError as exc:
        return f"RHINO_NOT_AVAILABLE\n{exc}"
    except RhinoCommandError as exc:
        return f"RHINO_COMMAND_FAILED\n{exc}"
    return _image_result(result.get("path"))


@mcp.tool()
def gh_eval_python(code: str, create_document: bool = False, timeout_seconds: Optional[float] = None) -> str:
    """Run IronPython 2.7 with the Grasshopper SDK in scope (UI thread).

    名稱：G(Grasshopper 模組)、ghdoc(作用中 GH 文件)、canvas、gh(外掛介面)、find(id 或唯一暱稱)，
    以及 rhino_eval_python 的全部名稱。結果放在 result。用於 gh_* 工具沒涵蓋的操作。
    """
    return _run("gh_eval_python", {"code": code, "create_document": create_document or None},
                timeout=timeout_seconds or _slow())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
