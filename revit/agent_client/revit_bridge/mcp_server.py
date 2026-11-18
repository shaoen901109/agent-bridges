# encoding: utf-8
"""Revit 2024 Agent Bridge 的 MCP server（stdio transport）。

只是 client.py 的一層薄殼：所有協定邏輯都在 client.py，這裡負責
把指令包成 MCP tools、把截圖包成 image content。

啟動方式：
    python -m revit_bridge.mcp_server
環境變數：
    REVIT_BRIDGE_HOST      預設 127.0.0.1
    REVIT_BRIDGE_PORT      預設 9879
    REVIT_BRIDGE_TIMEOUT   預設 30（秒）
    REVIT_BRIDGE_MAX_EDGE  預設 1280（回傳給模型的圖片最長邊像素）
    REVIT_EXE              revit_launch 使用的 Revit.exe（預設 C:\\Program Files\\Autodesk\\Revit 2024\\Revit.exe）
"""

from __future__ import annotations

import json
import os
import time
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
    RevitClient,
    RevitCommandError,
    RevitConnectionError,
    RevitTimeoutError,
)
from . import images as image_utils
from . import launcher
from . import window_capture

HOST = os.environ.get("REVIT_BRIDGE_HOST", DEFAULT_HOST)
PORT = int(os.environ.get("REVIT_BRIDGE_PORT", DEFAULT_PORT))
TIMEOUT = float(os.environ.get("REVIT_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT))
MAX_EDGE = int(os.environ.get("REVIT_BRIDGE_MAX_EDGE", image_utils.DEFAULT_MAX_EDGE))

mcp = _ServerClass(
    "revit-bridge",
    instructions=(
        "Control the Revit 2024 instance on this machine: inspect the model, create walls / floors / levels / family "
        "instances, read and set parameters, switch views, take screenshots, and run arbitrary C# (Revit API) snippets. "
        "Tools are prefixed revit_. Lengths are in millimetres (except inside revit_eval_csharp, which is feet). "
        "前提：Revit 2024 已開啟且已安裝 RevitAgentBridge 外掛（Revit 啟動時自動開伺服器）。"
        "若工具回傳 REVIT_NOT_AVAILABLE，可呼叫 revit_launch 啟動 Revit；"
        "若 Revit 停在對話框（指令逾時），請告知使用者自行處理，不要強制結束 Revit。"
    ),
)

_client: Optional[RevitClient] = None


def _get_client() -> RevitClient:
    global _client
    if _client is None:
        _client = RevitClient(host=HOST, port=PORT, timeout=TIMEOUT)
    return _client


def _call(method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
    """呼叫 Revit；連線中斷時自動重連一次。逾時（請求已送出）不重送，避免同一個建模指令做兩次。"""
    client = _get_client()
    try:
        return client.call(method, params, timeout)
    except RevitTimeoutError:
        raise
    except RevitConnectionError:
        client.close()
        return client.call(method, params, timeout)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _run(method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> str:
    """執行指令並把結果序列化成 JSON 字串；錯誤轉成人看得懂的說明。"""
    try:
        return _dump(_call(method, params, timeout))
    except RevitConnectionError as exc:
        return f"REVIT_NOT_AVAILABLE\n{exc}"
    except RevitCommandError as exc:
        return f"REVIT_COMMAND_FAILED\n{exc}"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _slow() -> float:
    return max(TIMEOUT, 60.0)


def _image_result(path: str) -> Any:
    try:
        with open(path, "rb") as handle:
            image = image_utils.load_image(handle.read())
    except OSError as exc:
        return f"REVIT_COMMAND_FAILED\n截圖檔案無法讀取：{path}（{exc}）"
    data, mime = image_utils.encode_for_model(image, MAX_EDGE)
    return Image(data=data, format="png" if mime == "image/png" else "jpeg")


# --- 1. 基本資訊 -----------------------------------------------------------


@mcp.tool()
def revit_get_info() -> str:
    """Check the bridge connection and get Revit basics (also serves as ping).

    取得 Revit 版本、目前文件（名稱、路徑、是否族群檔、是否有未存變更）、作用中視圖、選取數、橋接器狀態、
    最近一次偵測到的對話框。若 Revit 停在對話框，回傳 stale=true 的快取資訊與警告（不會卡住）。
    Always call this first to confirm the bridge is reachable.
    """
    return _run("get_info")


@mcp.tool()
def revit_get_scene_info(limit: int = 200) -> str:
    """Summarize the project: levels (name / elevation in mm), views, element counts per model category, wall / floor types.

    大模型會自動截斷（limit 控制視圖與類別清單上限）。Lengths are in millimetres.
    """
    return _run("get_scene_info", _clean({"limit": limit}), timeout=_slow())


@mcp.tool()
def revit_list_elements(
    category: Optional[str] = None,
    type_name: Optional[str] = None,
    level: Optional[str] = None,
    name_contains: Optional[str] = None,
    types_only: bool = False,
    include_bbox: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """List elements (paged) with id, name, category, type, level and location in mm.

    category: BuiltInCategory 名稱（"OST_Walls"、"Walls"、"Doors"…）或文件內的類別名稱。
    type_name: 類型名稱包含此字串。level: 樓層名稱。types_only=true 改列「類型」（牆類型、族群類型…），
    用於查有哪些類型可用。元件 id 是整數，後續操作都用它。
    """
    return _run(
        "list_elements",
        _clean({"category": category, "type_name": type_name, "level": level, "name_contains": name_contains,
                "types_only": types_only or None, "include_bbox": include_bbox or None,
                "limit": limit, "offset": offset}),
        timeout=_slow(),
    )


@mcp.tool()
def revit_get_element(id: int, parameter_names: Optional[List[str]] = None) -> str:
    """Get one element: summary, bounding box and all parameters.

    length 參數的 value 是公釐、angle 是度，其他型別是 Revit 內部單位（另見 value_string）。
    parameter_names 可只取指定參數，避免輸出過長。
    """
    return _run("get_element", _clean({"id": id, "parameter_names": parameter_names}), timeout=_slow())


@mcp.tool()
def revit_set_parameter(
    id: int,
    value: Any,
    name: Optional[str] = None,
    built_in: Optional[str] = None,
) -> str:
    """Set a parameter on an element (one Revit transaction; Revit's own undo reverts it).

    name: 參數名稱（先找實例參數，找不到再找型別參數）；或 built_in: BuiltInParameter 名稱（如 WALL_USER_HEIGHT_PARAM）。
    value: 長度參數傳「公釐」數字、角度參數傳「度」；字串值若是長度參數會照專案顯示單位解析（如 "3 m"）；
    是非題用 true/false；元件參照用 id 整數。型別參數會影響所有同型別元件，回應會註明。
    """
    return _run("set_parameter", _clean({"id": id, "value": value, "name": name, "built_in": built_in}))


@mcp.tool()
def revit_select_elements(ids: Optional[List[int]] = None, clear: bool = False, add: bool = False) -> str:
    """Set or clear the Revit selection by element id. add=true 表示加入而非取代。"""
    return _run("select_elements", _clean({"ids": ids, "clear": clear or None, "add": add or None}))


@mcp.tool()
def revit_delete_elements(ids: List[int]) -> str:
    """Delete elements by id (one transaction).

    破壞性操作：先用 revit_list_elements / revit_get_element 確認 id，不要刪除使用者既有的元件。
    Revit 會連帶刪除相依元件（例如牆上的門窗），回應的 deleted_count 可能大於 ids 數量。
    """
    return _run("delete_elements", {"ids": ids})


# --- 2. 建模 -----------------------------------------------------------------
# 座標與尺寸一律公釐；level 是樓層名稱（省略 = 作用中平面視圖的樓層，否則最低樓層）。


@mcp.tool()
def revit_create_wall(
    start: List[float],
    end: List[float],
    level: Optional[str] = None,
    height: float = 3000,
    wall_type: Optional[str] = None,
    base_offset: float = 0,
    structural: bool = False,
) -> str:
    """Create a straight wall between two points ([x, y] in mm; z comes from the level).

    height / base_offset 公釐（預設 3000 / 0）。wall_type 省略 = 專案預設牆類型；
    名稱可用「類型名」或「族群名 : 類型名」（可用 revit_list_elements(category="Walls", types_only=true) 查）。
    """
    return _run("create_wall", _clean({"start": start, "end": end, "level": level, "height": height,
                                       "wall_type": wall_type, "base_offset": base_offset or None,
                                       "structural": structural or None}))


@mcp.tool()
def revit_create_floor(
    points: List[List[float]],
    level: Optional[str] = None,
    floor_type: Optional[str] = None,
) -> str:
    """Create a floor slab from a polygon: points = [[x, y], ...] in mm, in order around the boundary (no need to repeat the first point).

    floor_type 省略 = 專案預設樓板類型。樓板放在該樓層的標高上。
    """
    return _run("create_floor", _clean({"points": points, "level": level, "floor_type": floor_type}))


@mcp.tool()
def revit_create_level(elevation: float, name: Optional[str] = None, create_plan_view: bool = True) -> str:
    """Create a level at the given elevation (mm). create_plan_view=true 同時建立該樓層的平面視圖（API 建立樓層預設不會有視圖）。"""
    return _run("create_level", _clean({"elevation": elevation, "name": name, "create_plan_view": create_plan_view}))


@mcp.tool()
def revit_place_family_instance(
    family_type: str,
    point: List[float],
    level: Optional[str] = None,
    rotation_deg: float = 0,
    host_id: Optional[int] = None,
) -> str:
    """Place a family instance at point ([x, y] or [x, y, z] in mm).

    family_type: 「族群名 : 類型名」或單獨類型名（有重名會要求改用完整寫法）。族群必須已載入專案
    （revit_list_elements(types_only=true, category="Doors") 可查）。
    門窗等牆上族群：point 放在牆線上（600 mm 內），自動找最近的牆當宿主，或用 host_id 指定；
    窗台高度請之後用 revit_set_parameter 設定。一般單樓層族群（家具、柱）可用 rotation_deg（度）。
    """
    return _run("place_family_instance", _clean({"family_type": family_type, "point": point, "level": level,
                                                 "rotation_deg": rotation_deg or None, "host_id": host_id}))


# --- 3. 視圖與截圖 -------------------------------------------------------------


@mcp.tool()
def revit_set_view(view: Optional[str] = None, zoom_extents: bool = False) -> str:
    """Switch the active view by name (or id), or "3d" to open / create a default 3D view; optionally zoom to fit.

    view 省略 = 只對目前視圖做 zoom_extents。可用視圖名稱見 revit_get_scene_info。
    截圖前常用：revit_set_view(view="3d", zoom_extents=True) 然後 revit_view_screenshot。
    """
    return _run("set_view", _clean({"view": view, "zoom_extents": zoom_extents or None}))


@mcp.tool(structured_output=False)
def revit_view_screenshot(pixel_size: int = 1600) -> Any:
    """Export the ACTIVE view to a PNG (doc.ExportImage) and return it as an image.

    這是驗證建模結果的主要方式：改完模型就截圖確認（先 revit_set_view 切到想看的視圖）。
    只能匯出圖形視圖（平面 / 立面 / 剖面 / 3D / 圖紙）；明細表不行。
    """
    try:
        result = _call("screenshot", _clean({"pixel_size": pixel_size}), timeout=_slow())
    except RevitConnectionError as exc:
        return f"REVIT_NOT_AVAILABLE\n{exc}"
    except RevitCommandError as exc:
        return f"REVIT_COMMAND_FAILED\n{exc}"
    return _image_result(result.get("path"))


@mcp.tool(structured_output=False)
def revit_window_screenshot(
    max_edge: Optional[int] = None, title_contains: Optional[str] = None, restore_if_minimized: bool = False
) -> Any:
    """Capture a whole application window at the OS level (screen grab / PrintWindow; view-only, no input simulation).

    Revit 的繪圖區是 DirectX：視窗沒被遮住時拍得到模型；被遮住時退回 PrintWindow，繪圖區可能空白
    （看結果的 notes）。要看模型本身請優先用 revit_view_screenshot。

    預設是 Revit 主視窗（含功能區、專案瀏覽器、屬性面板）。title_contains 可指定其他視窗（例如卡住的對話框標題關鍵字）。
    當需要看工具列、開著的對話框，或懷疑 Revit 停在對話框（請求逾時）時使用。看得到對話框內容後請使用者處理。
    """
    try:
        captured = window_capture.capture_window(
            title_contains=title_contains, restore_if_minimized=restore_if_minimized)
    except window_capture.WindowCaptureError as exc:
        return f"REVIT_WINDOW_NOT_FOUND\n{exc}"
    except ImportError as exc:
        return f"MISSING_DEPENDENCY\n需要 Pillow 才能做全視窗截圖：{exc}"
    data, mime = image_utils.encode_for_model(captured["image"], max_edge or MAX_EDGE)
    return Image(data=data, format="png" if mime == "image/png" else "jpeg")


# --- 4. 任意 C# ---------------------------------------------------------------


@mcp.tool()
def revit_eval_csharp(code: str, transaction: bool = True, timeout_seconds: Optional[float] = None) -> str:
    """Compile and run a C# snippet inside Revit (full Revit API), on the Revit API thread.

    片段會被包進 `public static object Run(UIApplication uiapp, UIDocument uidoc, Document doc, Action<object> print)`；
    可直接用這四個名稱。預設 using：System、System.Collections.Generic、System.Linq、Autodesk.Revit.DB、Autodesk.Revit.UI
    （其他 using 寫在片段最上面即可，例如 `using System.IO;`）。片段沒 return 會自動補 `return null;`，
    要回傳資料就 `return` 它；print(x) 的輸出一併回傳。結果會序列化成 JSON（ElementId→整數、XYZ→[x,y,z]、
    Element→{id,name,category}、集合→陣列，其餘 ToString）。
    transaction=true（預設）：整段包在一個 Transaction 內，失敗自動 RollBack；片段內不要再自己開 Transaction，
    若要自己管理交易（TransactionGroup、檔案操作、ExportImage…）請傳 transaction=false。
    注意：只支援 C# 5 語法（沒有 ?. / 字串插值 $"" / nameof / 運算式主體成員 / out var）；
    單位是 Revit 內部的「英尺」（1 ft = 304.8 mm），可用 UnitUtils.ConvertToInternalUnits(mm, UnitTypeId.Millimeters)；
    編譯錯誤會回「片段內的行號」與訊息；執行期錯誤沒有行號，請用 print 除錯。
    """
    return _run("eval_csharp", _clean({"code": code, "transaction": transaction}),
                timeout=timeout_seconds or _slow())


# --- 5. 復原 / 檔案 / 啟動 --------------------------------------------------


@mcp.tool()
def revit_undo(direction: str = "undo", times: int = 1) -> str:
    """Undo or redo in Revit (posts Revit's own Undo / Redo command). direction: "undo" | "redo".

    注意：這是非同步的（PostCommand）——指令投遞後要等 Revit 閒下來才會執行，回傳時可能還沒完成。
    橋接器的每個修改指令是一個 Transaction，一次 undo 剛好回復一步。完成後用 revit_get_info / 截圖確認。
    """
    method = "redo" if str(direction).lower() == "redo" else "undo"
    last = ""
    for i in range(max(1, min(int(times), 20))):
        last = _run(method)
        if last.startswith(("REVIT_NOT_AVAILABLE", "REVIT_COMMAND_FAILED")):
            return last
        if i + 1 < times:
            time.sleep(0.8)  # 讓上一個投遞的指令先執行完
    return last


@mcp.tool()
def revit_model_file(
    action: str,
    path: Optional[str] = None,
    confirm: bool = False,
    overwrite: bool = False,
    template: Optional[str] = None,
) -> str:
    """File operations. action: "new" | "open" | "save" | "save_as".

    安全規則（不可繞過）：new / open 需要 confirm=true（有未存變更時尤其要先問使用者）；
    save 需要 confirm=true；save_as 與 new 覆寫既有檔案需要 overwrite=true。
    new 需要 path（新專案會先存成這個 .rvt 再開啟，因為 API 無法顯示未存檔的新專案），template 省略 = Revit 預設樣板
    （找不到會回錯誤，請改指定 .rte 路徑）。open / 載入檔案若跳出升級或警告對話框，請使用者處理。
    未經使用者明確同意，不要儲存或覆蓋使用者的檔案。
    """
    action = str(action).lower()
    if action not in ("new", "open", "save", "save_as"):
        return "REVIT_COMMAND_FAILED\naction 必須是 new / open / save / save_as。"
    if action in ("new", "open", "save_as") and not path:
        return f"REVIT_COMMAND_FAILED\n{action} 需要 path。"
    method = {"new": "new_model", "open": "open_model", "save": "save_model", "save_as": "save_model"}[action]
    use_path = None if action == "save" else path
    return _run(method, _clean({"path": use_path, "confirm": confirm or None, "overwrite": overwrite or None,
                                "template": template}), timeout=_slow() * 3)


@mcp.tool()
def revit_launch(path: Optional[str] = None, wait_seconds: float = 180.0) -> str:
    """Start Revit 2024 if it is not running (optionally opening a .rvt) and wait until the bridge is ready.

    Revit 已在執行就只回報狀態。只會啟動、永遠不會關閉或強制結束 Revit。Revit 啟動很慢（一兩分鐘）。
    若 ready=false 且列出視窗標題，多半是授權 / 登入 / 更新對話框，或「是否載入此外掛」的安全性對話框擋住——請告知使用者自行處理。
    """
    try:
        return _dump(launcher.launch(path=path, wait_seconds=wait_seconds, host=HOST, port=PORT))
    except Exception as exc:  # pragma: no cover - 防禦
        return f"REVIT_COMMAND_FAILED\n{type(exc).__name__}: {exc}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
