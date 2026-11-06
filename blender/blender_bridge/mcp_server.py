# encoding: utf-8
"""Blender Agent Bridge 的 MCP server（stdio transport）。

只是 client.py 的一層薄殼：所有協定邏輯都在 client.py，這裡負責把指令包成 MCP tools、
把截圖包成 image content。

啟動方式：
    python -m blender_bridge.mcp_server
環境變數：
    BLENDER_BRIDGE_HOST      預設 127.0.0.1
    BLENDER_BRIDGE_PORT      預設 9877
    BLENDER_BRIDGE_TIMEOUT   預設 120（秒）
    BLENDER_BRIDGE_MAX_EDGE  預設 1280（回傳給模型的圖片最長邊像素）
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
    BlenderClient,
    BlenderCommandError,
    BlenderNotAvailable,
)
from . import images as image_utils

HOST = os.environ.get("BLENDER_BRIDGE_HOST", DEFAULT_HOST)
PORT = int(os.environ.get("BLENDER_BRIDGE_PORT", DEFAULT_PORT))
TIMEOUT = float(os.environ.get("BLENDER_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT))
MAX_EDGE = int(os.environ.get("BLENDER_BRIDGE_MAX_EDGE", image_utils.DEFAULT_MAX_EDGE))

NOT_AVAILABLE_PREFIX = "BLENDER_NOT_AVAILABLE:"
COMMAND_FAILED_PREFIX = "BLENDER_COMMAND_FAILED:"

mcp = _ServerClass(
    "blender-bridge",
    instructions=(
        "Control the Blender 3.4 instance the user already has open on this machine: "
        "inspect the scene, run bpy Python, select/transform/delete objects, set the 3D "
        "viewport, take screenshots, undo, open/save .blend files and render stills. "
        "前提：Blender 必須已由使用者開啟，且 startup 腳本 blender_bridge.py 已安裝"
        "（安裝後要重啟 Blender 一次；之後 Blender 啟動時自動開伺服器，不需手動啟動）。"
        "若工具回傳 BLENDER_NOT_AVAILABLE，請把這些步驟告訴使用者，不要自行啟動或關閉 Blender。"
    ),
)

_client: Optional[BlenderClient] = None


def _get_client() -> BlenderClient:
    global _client
    if _client is None:
        _client = BlenderClient(host=HOST, port=PORT, timeout=TIMEOUT)
    return _client


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _not_available(exc: BlenderNotAvailable) -> str:
    return (
        f"{NOT_AVAILABLE_PREFIX} Blender 橋接連不上。Blender 必須已由使用者開啟，"
        "且 startup 腳本已安裝（安裝後重啟 Blender 才會生效）。\n"
        f"{exc}"
    )


def _run(cmd: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> str:
    """執行指令並把結果序列化成 JSON 字串；錯誤轉成帶前綴的文字。"""
    try:
        return _dump(_get_client().call(cmd, params, timeout))
    except BlenderNotAvailable as exc:
        return _not_available(exc)
    except BlenderCommandError as exc:
        tag = "BLENDER_REFUSED:" if exc.refused else COMMAND_FAILED_PREFIX
        return f"{tag} {exc}"


# --- 1. 基本資訊 -----------------------------------------------------------


@mcp.tool()
def blender_ping() -> str:
    """Check the bridge connection and get Blender basics.

    回傳 Blender 版本、是否 --background、目前 .blend 路徑、是否有未儲存變更（is_dirty）、
    pid 與埠。Always call this first to confirm the bridge is reachable.
    """
    return _run("ping", timeout=10)


@mcp.tool()
def blender_get_scene_info(limit: int = 50, offset: int = 0,
                           types: Optional[List[str]] = None) -> str:
    """Summarize the current scene: units, frame range, render engine, object counts
    by type, collection tree, selection, active object, and a paged object list.

    limit/offset 控制物件清單分頁（limit=-1 全部）；types 例如 ["MESH","CAMERA"]。
    Lengths are in Blender units (metres by default).
    """
    return _run("scene_info", _clean({"limit": limit, "offset": offset, "types": types}))


@mcp.tool()
def blender_list_objects(name_contains: Optional[str] = None, types: Optional[List[str]] = None,
                         limit: int = 100, offset: int = 0) -> str:
    """List objects with name, type, location, rotation (deg), scale, dimensions, parent,
    collections, visibility, vertex/face counts (meshes) and modifiers.

    name_contains 不分大小寫；types 例如 ["MESH"]。物件名稱是後續操作的識別碼。
    """
    return _run("list_objects", _clean(
        {"name_contains": name_contains, "types": types, "limit": limit, "offset": offset}))


# --- 2. 任意 Python --------------------------------------------------------


@mcp.tool()
def blender_eval_python(code: str, message: Optional[str] = None, undo_push: bool = True,
                        timeout_seconds: Optional[float] = None) -> str:
    """Run arbitrary Python inside Blender's main thread (full bpy API).

    Globals persist between calls and already contain bpy, bmesh, mathutils, math,
    Vector, Matrix, Euler, Quaternion. Assign a JSON-friendly value to `result` to get it
    back. Returns stdout/stderr/result, or a traceback on error.
    這是最強大也最危險的工具。執行前會自動 undo_push（message 為步驟名稱），使用者一次
    Ctrl+Z 可回復。timer context 下 bpy.context.object/selected_objects 可能不可用，
    請用 bpy.data 與 view_layer.objects.active；operator 需要 bpy.context.temp_override。
    只用於 Blender 相關的事。
    """
    return _run("exec", _clean({"code": code, "message": message,
                                "undo_push": None if undo_push else False}),
                timeout=timeout_seconds or max(TIMEOUT, 600.0))


# --- 3. 物件操作 -----------------------------------------------------------


@mcp.tool()
def blender_select_objects(names: Optional[List[str]] = None, clear: bool = False,
                           active: Optional[str] = None) -> str:
    """Select objects by name and set the active object.

    clear=true 先清空選取（names 為空時只清空）。active 預設為 names 的最後一個。
    回傳 selected / missing / selection_now。
    """
    return _run("select", _clean({"names": names, "clear": clear or None, "active": active}))


@mcp.tool()
def blender_delete_objects(names: List[str], remove_data: bool = True) -> str:
    """Delete objects by name (only the listed names; missing names are reported).

    破壞性操作：刪除前先用 list_objects 確認名稱；不要刪使用者既有的物件。
    會先 undo_push，一次 Ctrl+Z 可回復。remove_data=true 順便清掉無人使用的 mesh 等資料。
    """
    return _run("delete", _clean({"names": names, "remove_data": None if remove_data else False}))


@mcp.tool()
def blender_transform_objects(
    names: List[str],
    location: Optional[List[float]] = None,
    rotation_deg: Optional[List[float]] = None,
    scale: Optional[Any] = None,
    delta: Optional[List[float]] = None,
    delta_rotation_deg: Optional[List[float]] = None,
    delta_scale: Optional[Any] = None,
) -> str:
    """Set or nudge object transforms (one undo step).

    location / rotation_deg / scale 是絕對值（rotation 為 XYZ 歐拉角、單位度；
    scale 可為單一數值或 [sx,sy,sz]）。delta / delta_rotation_deg / delta_scale 是相對變更
    （delta_scale 為乘法）。單位為 Blender 單位（預設公尺）。
    """
    return _run("transform", _clean({
        "names": names, "location": location, "rotation": rotation_deg, "scale": scale,
        "delta": delta, "delta_rotation": delta_rotation_deg, "delta_scale": delta_scale,
    }))


# --- 4. 視埠與截圖 ---------------------------------------------------------


@mcp.tool()
def blender_set_view(view: Optional[str] = None, projection: Optional[str] = None,
                     shading: Optional[str] = None, zoom_extents: bool = False,
                     view_selected: bool = False) -> str:
    """Set the 3D viewport: standard view, projection, shading, zoom to all/selected.

    view: front/back/left/right/top/bottom/iso。projection: ortho/persp。
    shading: wireframe/solid/material/rendered。zoom_extents=true 把整個場景帶進畫面
    ——截圖前常用。Requires the Blender GUI (fails in --background).
    """
    return _run("set_view", _clean({
        "view": view, "projection": projection, "shading": shading,
        "zoom_extents": zoom_extents or None, "view_selected": view_selected or None,
    }))


def _screenshot(mode: str, max_edge: Optional[int]) -> Any:
    edge = max_edge or MAX_EDGE
    try:
        result = _get_client().call(
            "screenshot", {"mode": mode, "max_edge": edge}, timeout=max(TIMEOUT, 60.0))
    except BlenderNotAvailable as exc:
        return _not_available(exc)
    except BlenderCommandError as exc:
        return f"{COMMAND_FAILED_PREFIX} {exc}"
    path = result.get("path")
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return f"{COMMAND_FAILED_PREFIX} 截圖檔案無法讀取：{path}（{exc}）"
    data, mime = image_utils.encode_for_model(data, edge)
    return [
        Image(data=data, format="png" if mime == "image/png" else "jpeg"),
        _dump({"path": path, "method": result.get("method"), "size": result.get("size")}),
    ]


@mcp.tool(structured_output=False)
def blender_viewport_screenshot(max_edge: Optional[int] = None) -> Any:
    """Capture the 3D viewport (screen.screenshot_area, fallback render.opengl) as an image.

    這是驗證建模結果的主要方式：改完就截圖確認。先 blender_set_view(zoom_extents=True)
    把東西帶進畫面。max_edge 控制回傳圖片最長邊（預設 1280）。Fails in --background.
    """
    return _screenshot("viewport", max_edge)


@mcp.tool(structured_output=False)
def blender_window_screenshot(max_edge: Optional[int] = None) -> Any:
    """Capture the whole Blender window (toolbars, panels, dialogs) as an image.

    當需要看工具列/面板狀態、開啟中的對話框、或視埠截圖看不到的 UI 時使用。只能看，不能點。
    """
    return _screenshot("window", max_edge)


# --- 5. 復原 / 檔案 / 算圖 -------------------------------------------------


@mcp.tool()
def blender_undo_redo(direction: str = "undo", times: int = 1) -> str:
    """Undo or redo in Blender. direction: "undo" or "redo".

    每個橋接器的修改指令（eval_python/transform/delete）都先 undo_push，所以一次 undo
    剛好回復一步。回傳 before/after 的物件清單以便確認。--background 模式通常不支援。
    """
    cmd = "redo" if str(direction).lower() == "redo" else "undo"
    return _run(cmd, {"times": times})


@mcp.tool()
def blender_model_file(action: str, path: Optional[str] = None, confirm: bool = False,
                       overwrite: bool = False, copy: bool = False, empty: bool = False) -> str:
    """Open / save / new .blend file. action: "open" | "save" | "new".

    安全規則（不可繞過）：目前檔案有未存變更時 open/new 需要 confirm=true；
    save 存回原路徑需要 confirm=true、存到既有檔案需要 overwrite=true。
    這些旗標只在使用者親口要求時才加。copy=true 另存副本不改目前檔案路徑；
    empty=true 讓 new 建空場景。
    """
    action = str(action).lower()
    if action not in ("open", "save", "new"):
        return f"{COMMAND_FAILED_PREFIX} action 必須是 open / save / new。"
    if action == "open" and not path:
        return f"{COMMAND_FAILED_PREFIX} open 需要 path。"
    return _run(action, _clean({
        "path": os.path.abspath(path) if path else None,
        "confirm": confirm or None, "overwrite": overwrite or None,
        "copy": copy or None, "empty": empty or None,
    }), timeout=max(TIMEOUT, 300.0))


@mcp.tool(structured_output=False)
def blender_render(path: Optional[str] = None, engine: Optional[str] = None,
                   resolution: Optional[List[int]] = None, camera: Optional[str] = None,
                   samples: Optional[int] = None, percentage: Optional[int] = None,
                   transparent: bool = False, max_edge: Optional[int] = None) -> Any:
    """Render one still frame to PNG through the scene camera and return it as an image.

    engine: WORKBENCH（最快，預設建議）/ EEVEE / CYCLES；不給則用場景目前引擎。
    resolution: [寬, 高]。camera: 相機物件名稱（場景必須有相機）。
    設定在算完後會還原。Works in --background too. Also returns the PNG path.
    """
    out = os.path.abspath(path) if path else image_utils.timestamped_path("render")
    try:
        result = _get_client().call("render", _clean({
            "path": out, "engine": engine, "resolution": resolution, "camera": camera,
            "samples": samples, "percentage": percentage, "transparent": transparent or None,
        }), timeout=max(TIMEOUT, 900.0))
    except BlenderNotAvailable as exc:
        return _not_available(exc)
    except BlenderCommandError as exc:
        return f"{COMMAND_FAILED_PREFIX} {exc}"
    real = result.get("path", out)
    try:
        with open(real, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return f"{COMMAND_FAILED_PREFIX} 算圖檔案無法讀取：{real}（{exc}）"
    data, mime = image_utils.encode_for_model(data, max_edge or MAX_EDGE)
    return [Image(data=data, format="png" if mime == "image/png" else "jpeg"), _dump(result)]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
