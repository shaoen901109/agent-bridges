# encoding: utf-8
"""SketchUp Agent Bridge 的命令列介面。

用途：除錯、備援（agent 沒有 MCP 時）、批次腳本。
所有輸出都是 JSON（截圖類會印出檔案路徑）。

    python -m sketchup_bridge ping
    python -m sketchup_bridge info
    python -m sketchup_bridge scene --limit 50
    python -m sketchup_bridge box --width 1000 --depth 2000 --height 3000
    python -m sketchup_bridge eval --file code.rb
    echo 'Sketchup.active_model.entities.size' | python -m sketchup_bridge eval -
    python -m sketchup_bridge screenshot --out C:\\temp\\view.png
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
    SketchUpClient,
    SketchUpCommandError,
    SketchUpConnectionError,
)
from . import images as image_utils


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _parse_point(text: Optional[str]) -> Optional[List[float]]:
    """把 "1,2,3" 或 "[1,2,3]" 解析成 [1.0, 2.0, 3.0]。"""
    if text is None:
        return None
    cleaned = text.strip().strip("[]")
    parts = [p for p in cleaned.replace(" ", "").split(",") if p != ""]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"座標必須是三個以逗號分隔的數字，收到：{text!r}")
    return [float(p) for p in parts]


def _parse_ids(values: Optional[List[str]]) -> Optional[List[int]]:
    if not values:
        return None
    ids: List[int] = []
    for value in values:
        for part in str(value).replace(" ", "").split(","):
            if part:
                ids.append(int(part))
    return ids


def _parse_json_arg(text: Optional[str]) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"不是合法的 JSON：{text!r}（{exc}）") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sketchup_bridge",
        description="SketchUp 代理操控插件 — 命令列介面",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"預設 {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SKETCHUP_BRIDGE_PORT", DEFAULT_PORT)))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="秒，預設 30")
    parser.add_argument("--units", default="mm", help="長度單位：mm(預設)/cm/m/in/ft")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="測試連線")
    sub.add_parser("commands", help="列出伺服器支援的所有指令")
    sub.add_parser("info", help="SketchUp 與模型基本資訊")
    sub.add_parser("actions", help="列出常用的 send_action 字串")

    p_scene = sub.add_parser("scene", help="模型全貌（統計/圖層/材質/元件/選取）")
    p_scene.add_argument("--limit", type=int, default=200)

    p_entities = sub.add_parser("entities", help="列出實體")
    p_entities.add_argument("--scope", default="top", choices=["top", "all", "selection"])
    p_entities.add_argument("--types", nargs="*", help="例如 Face Edge Group")
    p_entities.add_argument("--limit", type=int, default=100)
    p_entities.add_argument("--offset", type=int, default=0)

    p_selection = sub.add_parser("selection", help="目前選取集")
    p_selection.add_argument("--limit", type=int, default=200)

    p_select = sub.add_parser("select", help="設定選取集")
    p_select.add_argument("ids", nargs="*", help="persistent_id")
    p_select.add_argument("--clear", action="store_true")
    p_select.add_argument("--add", action="store_true")

    p_delete = sub.add_parser("delete", help="刪除實體")
    p_delete.add_argument("ids", nargs="+")

    p_shot = sub.add_parser("screenshot", help="視埠截圖（SketchUp 3D 視窗內容）")
    p_shot.add_argument("--out", help="輸出檔案路徑（預設存到系統暫存資料夾）")
    p_shot.add_argument("--width", type=int)
    p_shot.add_argument("--height", type=int)
    p_shot.add_argument("--transparent", action="store_true")
    p_shot.add_argument("--max-edge", type=int, default=0, help=">0 時縮圖到指定最長邊")

    p_window = sub.add_parser("window-shot", help="全視窗截圖（含工具列/對話框，僅觀看）")
    p_window.add_argument("--out")
    p_window.add_argument("--max-edge", type=int, default=0)
    p_window.add_argument("--restore", action="store_true", help="視窗最小化時先還原")
    p_window.add_argument("--list", action="store_true", help="只列出找到的 SketchUp 視窗")

    sub.add_parser("camera", help="取得相機")

    p_setcam = sub.add_parser("set-camera", help="設定相機")
    p_setcam.add_argument("--view", choices=["top", "bottom", "front", "back", "left", "right", "iso"])
    p_setcam.add_argument("--eye")
    p_setcam.add_argument("--target")
    p_setcam.add_argument("--up")
    p_setcam.add_argument("--fov", type=float)
    p_setcam.add_argument("--perspective", choices=["true", "false"])
    p_setcam.add_argument("--zoom-extents", action="store_true")

    p_action = sub.add_parser("action", help="送出 Sketchup.send_action")
    p_action.add_argument("action")

    p_tool = sub.add_parser("tool", help="切換工具（line / pushPull / select …）")
    p_tool.add_argument("tool")

    p_box = sub.add_parser("box", help="建立方塊")
    p_box.add_argument("--width", type=float, required=True)
    p_box.add_argument("--depth", type=float, required=True)
    p_box.add_argument("--height", type=float, required=True)
    p_box.add_argument("--origin", default="0,0,0")
    p_box.add_argument("--name")
    p_box.add_argument("--no-group", action="store_true")

    p_face = sub.add_parser("face", help="用點陣列建立面")
    p_face.add_argument("--points", required=True, help='JSON，例如 "[[0,0,0],[1000,0,0],[1000,1000,0]]"')
    p_face.add_argument("--push-pull", type=float)
    p_face.add_argument("--name")
    p_face.add_argument("--no-group", action="store_true")

    p_pp = sub.add_parser("pushpull", help="推拉既有的面")
    p_pp.add_argument("id", type=int)
    p_pp.add_argument("distance", type=float)
    p_pp.add_argument("--copy", action="store_true")

    p_group = sub.add_parser("group", help="把實體組成群組")
    p_group.add_argument("ids", nargs="+")
    p_group.add_argument("--name")

    p_tr = sub.add_parser("transform", help="移動/旋轉/縮放實體")
    p_tr.add_argument("ids", nargs="+")
    p_tr.add_argument("--translate")
    p_tr.add_argument("--rotate", help='JSON，例如 {"origin":[0,0,0],"axis":[0,0,1],"angle_deg":45}')
    p_tr.add_argument("--scale", help="數值或 JSON 陣列 [sx,sy,sz]")
    p_tr.add_argument("--scale-origin")
    p_tr.add_argument("--copy", action="store_true")

    p_mat = sub.add_parser("material", help="設定材質/顏色")
    p_mat.add_argument("ids", nargs="+")
    p_mat.add_argument("--material")
    p_mat.add_argument("--color", help='"#RRGGBB" 或 JSON [r,g,b]')
    p_mat.add_argument("--alpha", type=float)

    p_eval = sub.add_parser("eval", help="執行任意 Ruby")
    p_eval.add_argument("code", nargs="?", help='Ruby 程式碼；用 "-" 表示從 stdin 讀取')
    p_eval.add_argument("--file", help="從檔案讀取 Ruby 程式碼（UTF-8）")

    p_undo = sub.add_parser("undo", help="復原")
    p_undo.add_argument("--times", type=int, default=1)
    p_redo = sub.add_parser("redo", help="重做")
    p_redo.add_argument("--times", type=int, default=1)

    p_model = sub.add_parser("model", help="開新檔 / 開啟 / 儲存")
    p_model.add_argument("action", choices=["new", "open", "save"])
    p_model.add_argument("--path")
    p_model.add_argument("--confirm", action="store_true")
    p_model.add_argument("--overwrite", action="store_true")

    p_raw = sub.add_parser("raw", help="直接送出任意 method + JSON params")
    p_raw.add_argument("method")
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
        raise SystemExit("沒有讀到 Ruby 程式碼。請用 `eval \"code\"`、`--file path` 或從 stdin 輸入。")
    return data


def _window_shot(args: argparse.Namespace) -> int:
    from . import window_capture

    if args.list:
        _print_json(window_capture.find_windows())
        return 0

    captured = window_capture.capture_window(restore_if_minimized=args.restore)
    image = captured["image"]
    if args.max_edge and args.max_edge > 0:
        image = image_utils.shrink_to_max_edge(image, args.max_edge)
    path = args.out or image_utils.timestamped_path("window")
    saved = image_utils.save_image(image, path)
    _print_json(
        {
            "path": saved,
            "method": captured["method"],
            "size": list(image.size),
            "window": captured["window"],
            "notes": captured["notes"],
        }
    )
    return 0


def dispatch(args: argparse.Namespace, client: SketchUpClient) -> int:
    units = args.units
    command = args.command

    if command == "ping":
        _print_json(client.call("ping"))
    elif command == "commands":
        _print_json(client.call("list_commands"))
    elif command == "info":
        _print_json(client.call("get_info", {"units": units}))
    elif command == "actions":
        _print_json(client.call("list_actions"))
    elif command == "scene":
        _print_json(client.call("get_scene_info", {"units": units, "limit": args.limit}, timeout=120))
    elif command == "entities":
        _print_json(
            client.call(
                "list_entities",
                {
                    "units": units,
                    "scope": args.scope,
                    "types": args.types,
                    "limit": args.limit,
                    "offset": args.offset,
                },
                timeout=120,
            )
        )
    elif command == "selection":
        _print_json(client.call("get_selection", {"units": units, "limit": args.limit}))
    elif command == "select":
        params: Dict[str, Any] = {"clear": True} if args.clear else {"ids": _parse_ids(args.ids)}
        if args.add:
            params["add"] = True
        _print_json(client.call("select_entities", params))
    elif command == "delete":
        _print_json(client.call("delete_entities", {"ids": _parse_ids(args.ids)}))
    elif command == "screenshot":
        needs_resize = bool(args.max_edge and args.max_edge > 0)
        result = client.call(
            "screenshot",
            {
                "width": args.width,
                "height": args.height,
                "transparent": args.transparent,
                "return_base64": False,
                # 需要縮圖時先讓 SketchUp 存到暫存檔，縮完再寫到使用者指定的路徑
                "path": None if needs_resize else args.out,
            },
            timeout=120,
        )
        result.pop("image_base64", None)
        if needs_resize:
            with open(result["path"], "rb") as handle:
                image = image_utils.load_image(handle.read())
            image = image_utils.shrink_to_max_edge(image, args.max_edge)
            result["original_path"] = result["path"]
            result["path"] = image_utils.save_image(
                image, args.out or image_utils.timestamped_path("viewport")
            )
            result["resized_to"] = list(image.size)
        _print_json(result)
    elif command == "window-shot":
        return _window_shot(args)
    elif command == "camera":
        _print_json(client.call("get_camera", {"units": units}))
    elif command == "set-camera":
        params = {
            "units": units,
            "view": args.view,
            "eye": _parse_point(args.eye),
            "target": _parse_point(args.target),
            "up": _parse_point(args.up),
            "fov": args.fov,
            "zoom_extents": args.zoom_extents or None,
        }
        if args.perspective:
            params["perspective"] = args.perspective == "true"
        _print_json(client.call("set_camera", {k: v for k, v in params.items() if v is not None}))
    elif command == "action":
        _print_json(client.call("send_action", {"action": args.action}))
    elif command == "tool":
        _print_json(client.call("select_tool", {"tool": args.tool}))
    elif command == "box":
        _print_json(
            client.call(
                "create_box",
                {
                    "units": units,
                    "origin": _parse_point(args.origin),
                    "width": args.width,
                    "depth": args.depth,
                    "height": args.height,
                    "name": args.name,
                    "group": not args.no_group,
                },
            )
        )
    elif command == "face":
        _print_json(
            client.call(
                "create_face",
                {
                    "units": units,
                    "points": _parse_json_arg(args.points),
                    "push_pull": args.push_pull,
                    "name": args.name,
                    "group": not args.no_group,
                },
            )
        )
    elif command == "pushpull":
        _print_json(
            client.call(
                "push_pull",
                {"units": units, "id": args.id, "distance": args.distance, "copy": args.copy},
            )
        )
    elif command == "group":
        _print_json(client.call("create_group", {"ids": _parse_ids(args.ids), "name": args.name}))
    elif command == "transform":
        params = {
            "units": units,
            "ids": _parse_ids(args.ids),
            "translate": _parse_point(args.translate),
            "rotate": _parse_json_arg(args.rotate),
            "scale": _parse_json_arg(args.scale) if args.scale and args.scale.startswith("[") else (
                float(args.scale) if args.scale else None
            ),
            "scale_origin": _parse_point(args.scale_origin),
            "copy": args.copy or None,
        }
        _print_json(client.call("transform_entities", {k: v for k, v in params.items() if v is not None}))
    elif command == "material":
        color = args.color
        if color and color.startswith("["):
            color = _parse_json_arg(color)
        params = {
            "ids": _parse_ids(args.ids),
            "material": args.material,
            "color": color,
            "alpha": args.alpha,
        }
        _print_json(client.call("set_material", {k: v for k, v in params.items() if v is not None}))
    elif command == "eval":
        _print_json(client.call("eval_ruby", {"code": _read_code(args)}, timeout=120))
    elif command == "undo":
        _print_json(client.call("undo", {"times": args.times}))
    elif command == "redo":
        _print_json(client.call("redo", {"times": args.times}))
    elif command == "model":
        method = {"new": "new_model", "open": "open_model", "save": "save_model"}[args.action]
        params = {"path": args.path, "confirm": args.confirm or None, "overwrite": args.overwrite or None}
        _print_json(client.call(method, {k: v for k, v in params.items() if v is not None}, timeout=180))
    elif command == "raw":
        _print_json(client.call(args.method, _parse_json_arg(args.params), timeout=180))
    else:  # pragma: no cover - argparse 會先擋掉
        raise SystemExit(f"未知的子指令：{command}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 主控台預設不是 UTF-8，強制設定避免中文變亂碼
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - 舊版 Python
        pass

    parser = build_parser()
    args = parser.parse_args(argv)

    # window-shot 不需要連 SketchUp 伺服器
    if args.command == "window-shot":
        try:
            return _window_shot(args)
        except Exception as exc:
            sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
            return 3

    try:
        with SketchUpClient(host=args.host, port=args.port, timeout=args.timeout) as client:
            return dispatch(args, client)
    except SketchUpConnectionError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except SketchUpCommandError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
