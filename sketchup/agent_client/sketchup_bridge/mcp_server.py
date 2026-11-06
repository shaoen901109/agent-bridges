# encoding: utf-8
"""SketchUp Agent Bridge 的 MCP server（stdio transport）。

只是 client.py 的一層薄殼：所有協定邏輯都在 client.py，這裡負責
把指令包成 MCP tools、把截圖包成 image content。

啟動方式：
    python -m sketchup_bridge.mcp_server
環境變數：
    SKETCHUP_BRIDGE_HOST      預設 127.0.0.1
    SKETCHUP_BRIDGE_PORT      預設 9876
    SKETCHUP_BRIDGE_TIMEOUT   預設 30（秒）
    SKETCHUP_BRIDGE_MAX_EDGE  預設 1280（回傳給模型的圖片最長邊像素）
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
    SketchUpClient,
    SketchUpCommandError,
    SketchUpConnectionError,
)
from . import images as image_utils
from . import window_capture

HOST = os.environ.get("SKETCHUP_BRIDGE_HOST", DEFAULT_HOST)
PORT = int(os.environ.get("SKETCHUP_BRIDGE_PORT", DEFAULT_PORT))
TIMEOUT = float(os.environ.get("SKETCHUP_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT))
MAX_EDGE = int(os.environ.get("SKETCHUP_BRIDGE_MAX_EDGE", image_utils.DEFAULT_MAX_EDGE))

mcp = _ServerClass(
    "sketchup-bridge",
    instructions=(
        "Control a running SketchUp 2024 instance on this machine: inspect the model, "
        "run Ruby API code, switch tools, and take screenshots. "
        "前提：使用者必須先在 SketchUp 內從 延伸程式(Extensions) → Agent Bridge → 啟動伺服器。"
        "若連不上，請告知使用者這個步驟，不要嘗試自行啟動或關閉 SketchUp。"
    ),
)

_client: Optional[SketchUpClient] = None


def _get_client() -> SketchUpClient:
    global _client
    if _client is None:
        _client = SketchUpClient(host=HOST, port=PORT, timeout=TIMEOUT)
    return _client


def _call(method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Any:
    """呼叫 SketchUp；連線中斷時自動重連一次。"""
    client = _get_client()
    try:
        return client.call(method, params, timeout)
    except SketchUpConnectionError:
        client.close()
        return client.call(method, params, timeout)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _run(method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> str:
    """執行指令並把結果序列化成 JSON 字串；錯誤轉成人看得懂的說明。"""
    try:
        return _dump(_call(method, params, timeout))
    except SketchUpConnectionError as exc:
        return f"SKETCHUP_NOT_AVAILABLE\n{exc}"
    except SketchUpCommandError as exc:
        return f"SKETCHUP_COMMAND_FAILED\n{exc}"


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


# --- 1. 基本資訊 -----------------------------------------------------------


@mcp.tool()
def sketchup_get_info() -> str:
    """Check the bridge connection and get SketchUp/model basics.

    取得 SketchUp 版本、目前模型標題與路徑、模型單位、選取數量、伺服器狀態。
    Always call this first to confirm the bridge is reachable.
    """
    return _run("get_info")


@mcp.tool()
def sketchup_get_scene_info(limit: int = 200) -> str:
    """Summarize the current model: entity counts, layers/tags, materials,
    component definitions, scene pages, selection and overall bounding box.

    取得模型全貌。大模型會自動截斷（limit 控制每類清單的上限）。
    Lengths are returned in millimetres.
    """
    return _run("get_scene_info", _clean({"limit": limit}), timeout=max(TIMEOUT, 60.0))


@mcp.tool()
def sketchup_list_entities(
    scope: str = "top",
    types: Optional[List[str]] = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """List entities with persistent_id, type, name, bounds, layer, material.

    scope: "top" (model root only), "all" (recurse into groups/components),
    "selection" (current selection).
    types: optional filter, e.g. ["Face", "Edge", "Group", "ComponentInstance"].
    persistent_id 是後續操作實體的唯一識別碼。Lengths in millimetres.
    """
    return _run(
        "list_entities",
        _clean({"scope": scope, "types": types, "limit": limit, "offset": offset}),
        timeout=max(TIMEOUT, 60.0),
    )


@mcp.tool()
def sketchup_select_entities(
    ids: Optional[List[int]] = None, clear: bool = False, add: bool = False
) -> str:
    """Set or clear the SketchUp selection by persistent_id.

    ids: persistent_id 陣列。clear=true 清空選取。add=true 表示加入而非取代。
    """
    return _run("select_entities", _clean({"ids": ids, "clear": clear or None, "add": add or None}))


@mcp.tool()
def sketchup_delete_entities(ids: List[int]) -> str:
    """Delete entities by persistent_id (wrapped in one undoable operation).

    刪除實體。破壞性操作：刪除前請先用 list_entities 確認 id 正確，
    不要一次刪除使用者既有的幾何。
    """
    return _run("delete_entities", {"ids": ids})


# --- 2. 截圖 ---------------------------------------------------------------


@mcp.tool(structured_output=False)
def sketchup_viewport_screenshot(
    width: Optional[int] = None,
    height: Optional[int] = None,
    transparent: bool = False,
    max_edge: Optional[int] = None,
) -> Any:
    """Render the SketchUp 3D viewport to a PNG image (view.write_image).

    這是驗證建模結果的主要方式：改完模型就截圖確認。
    Returns an image. width/height default to the current viewport size.
    """
    try:
        result = _call(
            "screenshot",
            _clean(
                {
                    "width": width,
                    "height": height,
                    "transparent": transparent or None,
                    "return_base64": False,
                }
            ),
            timeout=max(TIMEOUT, 60.0),
        )
    except SketchUpConnectionError as exc:
        return f"SKETCHUP_NOT_AVAILABLE\n{exc}"
    except SketchUpCommandError as exc:
        return f"SKETCHUP_COMMAND_FAILED\n{exc}"

    path = result.get("path")
    try:
        with open(path, "rb") as handle:
            image = image_utils.load_image(handle.read())
    except OSError as exc:
        return f"SKETCHUP_COMMAND_FAILED\n截圖檔案無法讀取：{path}（{exc}）"

    data, mime = image_utils.encode_for_model(image, max_edge or MAX_EDGE)
    return Image(data=data, format="png" if mime == "image/png" else "jpeg")


@mcp.tool(structured_output=False)
def sketchup_window_screenshot(
    max_edge: Optional[int] = None, restore_if_minimized: bool = False
) -> Any:
    """Capture the whole SketchUp application window (toolbars, trays, dialogs)
    at the OS level. View-only; no mouse/keyboard simulation.

    當需要看工具列狀態、開啟中的對話框、或視埠截圖看不到的 UI 時使用。
    """
    try:
        captured = window_capture.capture_window(restore_if_minimized=restore_if_minimized)
    except window_capture.WindowCaptureError as exc:
        return f"SKETCHUP_WINDOW_NOT_FOUND\n{exc}"
    except ImportError as exc:
        return f"MISSING_DEPENDENCY\n需要 Pillow 才能做全視窗截圖：{exc}"

    data, mime = image_utils.encode_for_model(captured["image"], max_edge or MAX_EDGE)
    return Image(data=data, format="png" if mime == "image/png" else "jpeg")


# --- 3. 相機 ---------------------------------------------------------------


@mcp.tool()
def sketchup_get_camera() -> str:
    """Get the current camera: eye/target/up (mm), perspective flag and field of view."""
    return _run("get_camera")


@mcp.tool()
def sketchup_set_camera(
    view: Optional[str] = None,
    eye: Optional[List[float]] = None,
    target: Optional[List[float]] = None,
    up: Optional[List[float]] = None,
    perspective: Optional[bool] = None,
    fov: Optional[float] = None,
    zoom_extents: bool = False,
) -> str:
    """Set the camera, or switch to a standard view, or zoom to extents.

    view: one of top/bottom/front/back/left/right/iso.
    eye/target: [x, y, z] in millimetres. up: unit vector.
    zoom_extents=true 會把整個模型帶進畫面——截圖前常用。
    """
    return _run(
        "set_camera",
        _clean(
            {
                "view": view,
                "eye": eye,
                "target": target,
                "up": up,
                "perspective": perspective,
                "fov": fov,
                "zoom_extents": zoom_extents or None,
            }
        ),
    )


# --- 4. 工具切換 -----------------------------------------------------------


@mcp.tool()
def sketchup_send_action(action: Optional[str] = None, list_actions: bool = False) -> str:
    """Invoke a SketchUp UI command via Sketchup.send_action, e.g. switch the
    active tool ("selectLineTool:", "selectPushPullTool:") or change the view.

    list_actions=true 會列出所有常用 action 字串與中文說明。
    切換工具只是改變 UI 狀態，不會自己畫東西（本橋接器不模擬滑鼠鍵盤）。
    """
    if list_actions or not action:
        return _run("list_actions")
    return _run("send_action", {"action": action})


# --- 5. 建模捷徑 -----------------------------------------------------------


@mcp.tool()
def sketchup_create_box(
    width: float,
    depth: float,
    height: float,
    origin: Optional[List[float]] = None,
    units: str = "mm",
    name: Optional[str] = None,
    group: bool = True,
) -> str:
    """Create an axis-aligned box. Dimensions are in `units` (default mm).

    origin 是底面左前角 [x, y, z]，預設 [0,0,0]；height 往 +Z 長。
    group=true（預設）會把結果包成一個 Group，避免跟既有幾何黏在一起。
    """
    return _run(
        "create_box",
        _clean(
            {
                "width": width,
                "depth": depth,
                "height": height,
                "origin": origin,
                "units": units,
                "name": name,
                "group": group,
            }
        ),
    )


@mcp.tool()
def sketchup_create_face(
    points: List[List[float]],
    units: str = "mm",
    push_pull: Optional[float] = None,
    name: Optional[str] = None,
    group: bool = True,
) -> str:
    """Create a face from 3+ coplanar points, optionally extruding it.

    points: [[x,y,z], ...] in `units` (default mm), must be coplanar and non-collinear.
    push_pull: 選填，建立後立刻擠出這個距離（正負決定方向）。
    """
    return _run(
        "create_face",
        _clean(
            {
                "points": points,
                "units": units,
                "push_pull": push_pull,
                "name": name,
                "group": group,
            }
        ),
    )


@mcp.tool()
def sketchup_push_pull(id: int, distance: float, units: str = "mm", copy: bool = False) -> str:
    """Push/pull an existing face by persistent_id.

    distance 正值沿面的法線方向、負值反方向；面的法線可用 list_entities 查看。
    copy=true 會保留原面（等同按住 Ctrl 推拉）。
    """
    return _run("push_pull", _clean({"id": id, "distance": distance, "units": units, "copy": copy or None}))


@mcp.tool()
def sketchup_transform_entities(
    ids: List[int],
    translate: Optional[List[float]] = None,
    rotate: Optional[Dict[str, Any]] = None,
    scale: Optional[Any] = None,
    scale_origin: Optional[List[float]] = None,
    units: str = "mm",
    copy: bool = False,
) -> str:
    """Move / rotate / scale entities by persistent_id (one undoable operation).

    translate: [dx, dy, dz] in `units`.
    rotate: {"origin": [x,y,z], "axis": [x,y,z], "angle_deg": 45}.
    scale: a number, or [sx, sy, sz]; scale_origin defaults to [0,0,0].
    copy=true 只支援 Group / ComponentInstance（產生副本後再變換）。
    """
    return _run(
        "transform_entities",
        _clean(
            {
                "ids": ids,
                "translate": translate,
                "rotate": rotate,
                "scale": scale,
                "scale_origin": scale_origin,
                "units": units,
                "copy": copy or None,
            }
        ),
    )


@mcp.tool()
def sketchup_set_material(
    ids: List[int],
    material: Optional[str] = None,
    color: Optional[Any] = None,
    alpha: Optional[float] = None,
) -> str:
    """Apply a material / colour to entities by persistent_id.

    material: 既有材質名稱；不存在會自動建立。
    color: "#RRGGBB" 或 [r, g, b]（0-255）。alpha: 0.0-1.0 透明度。
    """
    return _run("set_material", _clean({"ids": ids, "material": material, "color": color, "alpha": alpha}))


# --- 6. 任意 Ruby ----------------------------------------------------------


@mcp.tool()
def sketchup_eval_ruby(code: str, timeout_seconds: Optional[float] = None) -> str:
    """Run arbitrary Ruby in SketchUp's main thread (full SketchUp Ruby API).

    這是最強大也最危險的工具。回傳 result 的 class / inspect / 可 JSON 化的值
    以及 puts 的 stdout 輸出；例外會回傳訊息與 backtrace。
    規則：會改模型的程式碼請自己包 model.start_operation("名稱", true) …
    model.commit_operation，讓使用者一次 Ctrl+Z 可復原。
    長度單位：SketchUp 內部是英吋，請寫 100.mm / 2.5.m 之類的轉換。
    只用於 SketchUp API；不要用它做與建模無關的系統操作。
    """
    return _run("eval_ruby", {"code": code}, timeout=timeout_seconds or max(TIMEOUT, 60.0))


# --- 7. 復原 / 檔案 --------------------------------------------------------


@mcp.tool()
def sketchup_undo_redo(direction: str = "undo", times: int = 1) -> str:
    """Undo or redo in SketchUp. direction: "undo" or "redo".

    每個橋接器的修改指令都是一個 operation，所以一次 undo 剛好回復一步。
    發現操作結果不對時，先 undo 再重做，不要疊加錯誤幾何。
    """
    method = "undo" if str(direction).lower() != "redo" else "redo"
    return _run(method, {"times": times})


@mcp.tool()
def sketchup_model_file(
    action: str,
    path: Optional[str] = None,
    confirm: bool = False,
    overwrite: bool = False,
) -> str:
    """Open / save / new SketchUp model file. action: "open" | "save" | "new".

    安全規則（不可繞過）：new 與 open 需要 confirm=true；save 覆寫既有檔案需要
    overwrite=true（存回原路徑則需要 confirm=true）。
    未經使用者明確同意，不要儲存或覆蓋使用者的檔案。
    """
    action = str(action).lower()
    if action not in ("open", "save", "new"):
        return "SKETCHUP_COMMAND_FAILED\naction 必須是 open / save / new。"
    method = {"open": "open_model", "save": "save_model", "new": "new_model"}[action]
    return _run(
        method,
        _clean({"path": path, "confirm": confirm or None, "overwrite": overwrite or None}),
        timeout=max(TIMEOUT, 120.0),
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
