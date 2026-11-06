# encoding: utf-8
"""Blender Agent Bridge 的命令列介面。

用途：除錯、備援（agent 沒有 MCP 時）、批次腳本。所有輸出都是 JSON。

    python -m blender_bridge ping
    python -m blender_bridge scene --limit 20
    python -m blender_bridge objects --types MESH --contains Cube
    python -m blender_bridge eval --file code.py
    Get-Content code.py -Raw | python -m blender_bridge eval -
    python -m blender_bridge select Cube Camera
    python -m blender_bridge transform Cube --location 1,2,3 --rotation 0,0,45
    python -m blender_bridge view --view iso --zoom-extents --shading solid
    python -m blender_bridge screenshot --out C:\\temp\\view.png --max-edge 1280
    python -m blender_bridge undo
    python -m blender_bridge model save --path C:\\temp\\a.blend
    python -m blender_bridge render --out C:\\temp\\r.png --engine WORKBENCH --resolution 800x600

離開碼：0 成功、1 指令執行失敗、2 連不上 Blender、3 截圖/算圖失敗。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    BlenderClient,
    BlenderCommandError,
    BlenderNotAvailable,
)
from . import images as image_utils

EXIT_OK = 0
EXIT_COMMAND_FAILED = 1
EXIT_NOT_AVAILABLE = 2
EXIT_SCREENSHOT_FAILED = 3


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _parse_vec3(text: Optional[str]) -> Optional[List[float]]:
    if text is None:
        return None
    cleaned = text.strip().strip("[]()")
    parts = [p for p in cleaned.replace(" ", "").split(",") if p != ""]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"必須是三個以逗號分隔的數字，收到：{text!r}")
    return [float(p) for p in parts]


def _parse_scale(text: Optional[str]) -> Any:
    if text is None:
        return None
    if "," in text:
        return _parse_vec3(text)
    return float(text)


def _parse_resolution(text: Optional[str]) -> Optional[List[int]]:
    if text is None:
        return None
    sep = "x" if "x" in text.lower() else ","
    parts = text.lower().split(sep)
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"解析度格式為 寬x高，收到：{text!r}")
    return [int(parts[0]), int(parts[1])]


def _parse_json_arg(text: Optional[str]) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"不是合法的 JSON：{text!r}（{exc}）") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blender_bridge",
        description="Blender Agent Bridge — 命令列介面（輸出 JSON）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"預設 {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"預設 {DEFAULT_PORT}（或環境變數 BLENDER_BRIDGE_PORT）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="秒，預設 120")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="測試連線；回 Blender 版本、是否 background、檔案路徑、是否有未存變更")
    sub.add_parser("commands", help="列出 Blender 端支援的所有指令")

    p_scene = sub.add_parser("scene", help="場景總覽（單位、統計、collections、選取、active、物件清單）")
    p_scene.add_argument("--limit", type=int, default=50, help="物件清單上限（-1 = 全部）")
    p_scene.add_argument("--offset", type=int, default=0)
    p_scene.add_argument("--types", nargs="*", help="例如 MESH CAMERA LIGHT")

    p_obj = sub.add_parser("objects", help="列出物件（名稱/type/位置/尺寸/parent/collection/頂點數）")
    p_obj.add_argument("--contains", help="名稱包含（不分大小寫）")
    p_obj.add_argument("--types", nargs="*")
    p_obj.add_argument("--limit", type=int, default=100)
    p_obj.add_argument("--offset", type=int, default=0)

    for name in ("eval", "exec"):
        p_eval = sub.add_parser(name, help="在 Blender 主執行緒執行 Python（bpy/bmesh/mathutils/math 已預載）")
        p_eval.add_argument("code", nargs="?", help='Python 程式碼；用 "-" 表示從 stdin 讀取')
        p_eval.add_argument("--file", help="從檔案讀取程式碼（UTF-8）")
        p_eval.add_argument("--message", help="undo 步驟名稱")
        p_eval.add_argument("--no-undo", action="store_true", help="不要先 undo_push")

    p_select = sub.add_parser("select", help="依名稱選取物件並設 active")
    p_select.add_argument("names", nargs="*")
    p_select.add_argument("--clear", action="store_true", help="先清空選取")
    p_select.add_argument("--active", help="指定 active 物件（預設為最後一個）")

    p_delete = sub.add_parser("delete", help="依名稱刪除物件")
    p_delete.add_argument("names", nargs="+")
    p_delete.add_argument("--keep-data", action="store_true", help="保留孤兒 data-block")

    p_tr = sub.add_parser("transform", help="設定/相對變更 location / rotation(度) / scale")
    p_tr.add_argument("names", nargs="+")
    p_tr.add_argument("--location", help="x,y,z（絕對）")
    p_tr.add_argument("--rotation", help="x,y,z 度（絕對）")
    p_tr.add_argument("--scale", help="數值或 x,y,z（絕對）")
    p_tr.add_argument("--delta", help="x,y,z 相對位移")
    p_tr.add_argument("--delta-rotation", help="x,y,z 度，相對旋轉")
    p_tr.add_argument("--delta-scale", help="數值或 x,y,z，相對縮放（乘法）")

    p_view = sub.add_parser("view", help="設定 3D 視埠：視角/投影/著色/框到全部")
    p_view.add_argument("--view", choices=["front", "back", "left", "right", "top", "bottom", "iso"])
    p_view.add_argument("--projection", choices=["ortho", "persp"])
    p_view.add_argument("--shading", choices=["wireframe", "solid", "material", "rendered"])
    p_view.add_argument("--zoom-extents", action="store_true")
    p_view.add_argument("--view-selected", action="store_true")

    p_shot = sub.add_parser("screenshot", help="3D 視埠截圖（screenshot_area，失敗退 render.opengl）")
    p_shot.add_argument("--out", help="輸出 PNG 路徑（預設存到系統暫存資料夾）")
    p_shot.add_argument("--max-edge", type=int, default=0, help=">0 時縮圖到指定最長邊")

    p_window = sub.add_parser("window-shot", help="整個 Blender 視窗截圖（含工具列/面板）")
    p_window.add_argument("--out")
    p_window.add_argument("--max-edge", type=int, default=0)

    p_undo = sub.add_parser("undo", help="復原")
    p_undo.add_argument("--times", type=int, default=1)
    p_redo = sub.add_parser("redo", help="重做")
    p_redo.add_argument("--times", type=int, default=1)

    p_model = sub.add_parser("model", help="開新檔 / 開啟 / 儲存 .blend")
    p_model.add_argument("action", choices=["open", "save", "new"])
    p_model.add_argument("--path")
    p_model.add_argument("--confirm", action="store_true", help="放棄未存變更 / 存回原路徑 需要")
    p_model.add_argument("--overwrite", action="store_true", help="存到既有檔案 需要")
    p_model.add_argument("--copy", action="store_true", help="save：另存副本，目前檔案路徑不變")
    p_model.add_argument("--empty", action="store_true", help="new：空場景（不含預設 Cube/燈/相機）")
    p_model.add_argument("--load-ui", action="store_true", help="open：一併載入檔案內的 UI 佈局")

    p_render = sub.add_parser("render", help="算一張 PNG（預設用目前引擎；WORKBENCH 最快）")
    p_render.add_argument("--out", help="輸出 PNG 路徑")
    p_render.add_argument("--engine", choices=["WORKBENCH", "EEVEE", "CYCLES"])
    p_render.add_argument("--resolution", help="寬x高，例如 1280x720")
    p_render.add_argument("--percentage", type=int)
    p_render.add_argument("--camera", help="相機物件名稱")
    p_render.add_argument("--samples", type=int)
    p_render.add_argument("--transparent", action="store_true")

    p_raw = sub.add_parser("raw", help="直接送出任意 cmd + JSON params")
    p_raw.add_argument("cmd")
    p_raw.add_argument("--params", default="{}")

    return parser


def _read_code(args: argparse.Namespace) -> str:
    if args.file:
        with open(args.file, "r", encoding="utf-8") as handle:
            return handle.read()
    if args.code and args.code != "-":
        return args.code
    data = sys.stdin.read()
    if not data.strip():
        raise SystemExit("沒有讀到 Python 程式碼。請用 `eval \"code\"`、`--file path` 或從 stdin 輸入。")
    return data


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None and v is not False}


def dispatch(args: argparse.Namespace, client: BlenderClient) -> int:
    command = args.command

    if command == "ping":
        _print_json(client.call("ping"))
    elif command == "commands":
        _print_json(client.call("list_commands"))
    elif command == "scene":
        _print_json(client.call("scene_info", _clean(
            {"limit": args.limit, "offset": args.offset, "types": args.types or None})))
    elif command == "objects":
        _print_json(client.call("list_objects", _clean(
            {"name_contains": args.contains, "types": args.types or None,
             "limit": args.limit, "offset": args.offset})))
    elif command in ("eval", "exec"):
        params: Dict[str, Any] = {"code": _read_code(args)}
        if args.message:
            params["message"] = args.message
        if args.no_undo:
            params["undo_push"] = False
        _print_json(client.call("exec", params, timeout=max(args.timeout, 600)))
    elif command == "select":
        _print_json(client.call("select", _clean(
            {"names": args.names, "clear": args.clear, "active": args.active})))
    elif command == "delete":
        params = {"names": args.names}
        if args.keep_data:
            params["remove_data"] = False
        _print_json(client.call("delete", params))
    elif command == "transform":
        _print_json(client.call("transform", _clean({
            "names": args.names,
            "location": _parse_vec3(args.location),
            "rotation": _parse_vec3(args.rotation),
            "scale": _parse_scale(args.scale),
            "delta": _parse_vec3(args.delta),
            "delta_rotation": _parse_vec3(args.delta_rotation),
            "delta_scale": _parse_scale(args.delta_scale),
        })))
    elif command == "view":
        _print_json(client.call("set_view", _clean({
            "view": args.view, "projection": args.projection, "shading": args.shading,
            "zoom_extents": args.zoom_extents, "view_selected": args.view_selected,
        })))
    elif command in ("screenshot", "window-shot"):
        mode = "window" if command == "window-shot" else "viewport"
        out = os.path.abspath(args.out) if args.out else image_utils.timestamped_path(mode)
        try:
            result = client.call("screenshot", _clean(
                {"mode": mode, "path": out, "max_edge": args.max_edge or None}), timeout=60)
        except BlenderCommandError as exc:
            sys.stderr.write(f"{exc}\n")
            return EXIT_SCREENSHOT_FAILED
        if not os.path.exists(result.get("path", out)):
            sys.stderr.write(f"截圖檔案不存在：{result.get('path', out)}\n")
            return EXIT_SCREENSHOT_FAILED
        _print_json(result)
    elif command in ("undo", "redo"):
        _print_json(client.call(command, {"times": args.times}))
    elif command == "model":
        params = _clean({
            "path": os.path.abspath(args.path) if args.path else None,
            "confirm": args.confirm, "overwrite": args.overwrite, "copy": args.copy,
            "empty": args.empty, "load_ui": args.load_ui,
        })
        _print_json(client.call(args.action, params, timeout=max(args.timeout, 300)))
    elif command == "render":
        out = os.path.abspath(args.out) if args.out else image_utils.timestamped_path("render")
        try:
            result = client.call("render", _clean({
                "path": out, "engine": args.engine,
                "resolution": _parse_resolution(args.resolution),
                "percentage": args.percentage, "camera": args.camera,
                "samples": args.samples, "transparent": args.transparent,
            }), timeout=max(args.timeout, 600))
        except BlenderCommandError as exc:
            sys.stderr.write(f"{exc}\n")
            return EXIT_SCREENSHOT_FAILED
        _print_json(result)
    elif command == "raw":
        _print_json(client.call(args.cmd, _parse_json_arg(args.params), timeout=max(args.timeout, 600)))
    else:  # pragma: no cover
        raise SystemExit(f"未知的子指令：{command}")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 主控台預設不是 UTF-8，強制設定避免中文變亂碼
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        client = BlenderClient(host=args.host, port=args.port, timeout=args.timeout)
        return dispatch(args, client)
    except BlenderNotAvailable as exc:
        sys.stderr.write(f"BLENDER_NOT_AVAILABLE: {exc}\n")
        return EXIT_NOT_AVAILABLE
    except BlenderCommandError as exc:
        sys.stderr.write(f"{exc}\n")
        if exc.response:
            _print_json(exc.response)
        return EXIT_COMMAND_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
