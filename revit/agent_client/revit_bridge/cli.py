# encoding: utf-8
"""Revit Agent Bridge 的命令列介面。

用途：除錯、備援（agent 沒有 MCP 時）、批次腳本。
所有輸出都是 JSON（截圖類會印出檔案路徑）。長度一律公釐。

結束碼：0 成功 / 1 指令失敗 / 2 連不上 Revit / 3 截圖失敗。

    python -m revit_bridge launch
    python -m revit_bridge ping
    python -m revit_bridge info
    python -m revit_bridge wall --start 0,0 --end 5000,0 --height 3000
    python -m revit_bridge eval --file code.cs
    echo "return doc.Title;" | python -m revit_bridge eval -
    python -m revit_bridge screenshot --out C:\\temp\\view.png
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
    RevitClient,
    RevitCommandError,
    RevitConnectionError,
)
from . import images as image_utils


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _parse_point(text: Optional[str]) -> Optional[List[float]]:
    """把 "1,2" / "1,2,3" / "[1,2,3]" 解析成座標 list（公釐）。"""
    if text is None:
        return None
    cleaned = text.strip().strip("[]")
    parts = [p for p in cleaned.replace(" ", "").split(",") if p != ""]
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError(f"座標必須是 2 或 3 個以逗號分隔的數字（公釐），收到：{text!r}")
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


def _json_or_text(text: Optional[str]) -> Any:
    """set-param 的 value：能解析成 JSON 就用 JSON（數字 / 布林），否則當字串。"""
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="revit_bridge",
        description="Revit 代理操控插件 — 命令列介面（長度一律公釐）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=os.environ.get("REVIT_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("REVIT_BRIDGE_PORT", DEFAULT_PORT)))
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("REVIT_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT)), help="秒，預設 30")

    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser("launch", help="啟動 Revit（若未執行）並等橋接器就緒")
    p_launch.add_argument("--path", help="啟動時一併開啟的 .rvt")
    p_launch.add_argument("--wait", type=float, default=180.0, help="等待秒數，預設 180")

    sub.add_parser("ping", help="測試連線（不進 Revit API 執行緒）")
    sub.add_parser("commands", help="列出伺服器支援的所有指令")
    sub.add_parser("info", help="Revit 與文件基本資訊（含最近一次對話框）")

    p_scene = sub.add_parser("scene", help="專案全貌（樓層 / 視圖 / 類別統計 / 類型）")
    p_scene.add_argument("--limit", type=int, default=200)

    p_els = sub.add_parser("elements", help="列出元件")
    p_els.add_argument("--category")
    p_els.add_argument("--type-name")
    p_els.add_argument("--level")
    p_els.add_argument("--name-contains")
    p_els.add_argument("--types-only", action="store_true", help="改列類型（牆類型、族群類型…）")
    p_els.add_argument("--bbox", action="store_true", help="附上外框")
    p_els.add_argument("--limit", type=int, default=100)
    p_els.add_argument("--offset", type=int, default=0)

    p_el = sub.add_parser("element", help="取得單一元件（含參數）")
    p_el.add_argument("id", type=int)
    p_el.add_argument("--param", action="append", help="只取指定參數名稱（可重複）")

    p_sp = sub.add_parser("set-param", help="設定參數（長度傳公釐、角度傳度）")
    p_sp.add_argument("id", type=int)
    p_sp.add_argument("value", help="數字 / true / false / 字串")
    p_sp.add_argument("--name", help="參數名稱")
    p_sp.add_argument("--built-in", help="BuiltInParameter 名稱")

    p_sel = sub.add_parser("select", help="設定選取集")
    p_sel.add_argument("ids", nargs="*")
    p_sel.add_argument("--clear", action="store_true")
    p_sel.add_argument("--add", action="store_true")

    p_del = sub.add_parser("delete", help="刪除元件")
    p_del.add_argument("ids", nargs="+")

    p_wall = sub.add_parser("wall", help="建立牆")
    p_wall.add_argument("--start", type=_parse_point, required=True, help="x,y（公釐）")
    p_wall.add_argument("--end", type=_parse_point, required=True, help="x,y（公釐）")
    p_wall.add_argument("--level")
    p_wall.add_argument("--height", type=float, default=3000)
    p_wall.add_argument("--wall-type")
    p_wall.add_argument("--base-offset", type=float)
    p_wall.add_argument("--structural", action="store_true")

    p_floor = sub.add_parser("floor", help="建立樓板（多邊形）")
    p_floor.add_argument("--points", type=_parse_json_arg, required=True, help='JSON：[[0,0],[5000,0],[5000,4000],[0,4000]]')
    p_floor.add_argument("--level")
    p_floor.add_argument("--floor-type")

    p_level = sub.add_parser("level", help="建立樓層")
    p_level.add_argument("--elevation", type=float, required=True, help="標高（公釐）")
    p_level.add_argument("--name")
    p_level.add_argument("--no-plan-view", action="store_true")

    p_place = sub.add_parser("place", help="放置族群")
    p_place.add_argument("family_type", help='"族群名 : 類型名" 或類型名')
    p_place.add_argument("--point", type=_parse_point, required=True, help="x,y[,z]（公釐）")
    p_place.add_argument("--level")
    p_place.add_argument("--rotation", type=float, help="度")
    p_place.add_argument("--host-id", type=int)

    p_view = sub.add_parser("view", help="切換視圖 / zoom")
    p_view.add_argument("--view", help='視圖名稱、id，或 "3d"')
    p_view.add_argument("--zoom-extents", action="store_true")

    p_shot = sub.add_parser("screenshot", help="匯出作用中視圖 PNG（ExportImage）")
    p_shot.add_argument("--pixel-size", type=int, default=1600)
    p_shot.add_argument("--out", help="輸出路徑（省略 = 暫存資料夾）")
    p_shot.add_argument("--max-edge", type=int, default=0, help="縮到最長邊不超過此像素（0 = 不縮）")

    p_window = sub.add_parser("window-shot", help="全視窗截圖（含功能區 / 對話框，僅觀看）")
    p_window.add_argument("--out")
    p_window.add_argument("--max-edge", type=int, default=1600)
    p_window.add_argument("--title-contains", help="指定其他視窗（例如對話框標題關鍵字）")
    p_window.add_argument("--restore", action="store_true", help="視窗最小化時先還原")
    p_window.add_argument("--list", action="store_true", help="只列出 Revit 的可見視窗")

    p_eval = sub.add_parser("eval", help="編譯並執行 C# 片段（Revit API）")
    p_eval.add_argument("code", nargs="?", help='程式片段，或 "-" 從 stdin 讀')
    p_eval.add_argument("--file", help="從檔案讀取片段（建議）")
    p_eval.add_argument("--no-transaction", action="store_true", help="不要自動包 Transaction")

    sub.add_parser("undo", help="復原（非同步）")
    sub.add_parser("redo", help="重做（非同步）")

    p_model = sub.add_parser("model", help="開新檔 / 開啟 / 儲存 / 另存")
    p_model.add_argument("action", choices=["new", "open", "save", "save_as"])
    p_model.add_argument("--path")
    p_model.add_argument("--confirm", action="store_true", help="new / open / save 需要（使用者同意後才加）")
    p_model.add_argument("--overwrite", action="store_true", help="覆寫既有檔案（使用者同意後才加）")
    p_model.add_argument("--template", help="new 用的 .rte 樣板路徑（省略 = Revit 預設）")

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
        raise SystemExit("沒有讀到 C# 程式碼。請用 `eval \"code\"`、`--file path` 或從 stdin 輸入。")
    return data


def _window_shot(args: argparse.Namespace) -> int:
    from . import window_capture

    if args.list:
        _print_json(window_capture.find_windows())
        return 0

    captured = window_capture.capture_window(title_contains=args.title_contains, restore_if_minimized=args.restore)
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


def _shot_result(client: RevitClient, params: Dict[str, Any], args: argparse.Namespace) -> int:
    """截圖：Revit 端存檔 → （必要時）縮圖 → 複製到使用者指定路徑。失敗回傳 3。"""
    try:
        result = client.call("screenshot", params, timeout=120)
    except RevitCommandError as exc:
        sys.stderr.write(f"{exc}\n")
        return 3
    path = result.get("path")
    wants_copy = bool(args.out) and os.path.abspath(args.out) != os.path.abspath(path or "")
    if (args.max_edge and args.max_edge > 0) or wants_copy:
        try:
            with open(path, "rb") as handle:
                image = image_utils.load_image(handle.read())
            if args.max_edge and args.max_edge > 0:
                image = image_utils.shrink_to_max_edge(image, args.max_edge)
                result["resized_to"] = list(image.size)
            result["original_path"] = path
            result["path"] = image_utils.save_image(image, args.out or image_utils.timestamped_path("view"))
        except OSError as exc:
            sys.stderr.write(f"截圖檔案無法讀取：{path}（{exc}）\n")
            return 3
    _print_json(result)
    return 0


def dispatch(args: argparse.Namespace, client: RevitClient) -> int:
    command = args.command
    call = client.call

    if command == "ping":
        _print_json(call("ping"))
    elif command == "commands":
        _print_json(call("list_commands"))
    elif command == "info":
        _print_json(call("get_info"))
    elif command == "scene":
        _print_json(call("get_scene_info", {"limit": args.limit}, timeout=120))
    elif command == "elements":
        _print_json(call("list_elements", _clean({
            "category": args.category, "type_name": args.type_name, "level": args.level,
            "name_contains": args.name_contains, "types_only": args.types_only or None,
            "include_bbox": args.bbox or None, "limit": args.limit, "offset": args.offset}), timeout=120))
    elif command == "element":
        _print_json(call("get_element", _clean({"id": args.id, "parameter_names": args.param}), timeout=120))
    elif command == "set-param":
        _print_json(call("set_parameter", _clean({
            "id": args.id, "value": _json_or_text(args.value), "name": args.name, "built_in": args.built_in})))
    elif command == "select":
        params: Dict[str, Any] = {"clear": True} if args.clear else {"ids": _parse_ids(args.ids)}
        if args.add:
            params["add"] = True
        _print_json(call("select_elements", params))
    elif command == "delete":
        _print_json(call("delete_elements", {"ids": _parse_ids(args.ids)}))
    elif command == "wall":
        _print_json(call("create_wall", _clean({
            "start": args.start, "end": args.end, "level": args.level, "height": args.height,
            "wall_type": args.wall_type, "base_offset": args.base_offset,
            "structural": args.structural or None})))
    elif command == "floor":
        _print_json(call("create_floor", _clean({
            "points": args.points, "level": args.level, "floor_type": args.floor_type})))
    elif command == "level":
        _print_json(call("create_level", _clean({
            "elevation": args.elevation, "name": args.name, "create_plan_view": False if args.no_plan_view else None})))
    elif command == "place":
        _print_json(call("place_family_instance", _clean({
            "family_type": args.family_type, "point": args.point, "level": args.level,
            "rotation_deg": args.rotation, "host_id": args.host_id})))
    elif command == "view":
        _print_json(call("set_view", _clean({"view": args.view, "zoom_extents": args.zoom_extents or None})))
    elif command == "screenshot":
        return _shot_result(client, _clean({"pixel_size": args.pixel_size}), args)
    elif command == "eval":
        _print_json(call("eval_csharp", _clean({
            "code": _read_code(args), "transaction": False if args.no_transaction else None}), timeout=120))
    elif command == "undo":
        _print_json(call("undo"))
    elif command == "redo":
        _print_json(call("redo"))
    elif command == "model":
        method = {"new": "new_model", "open": "open_model", "save": "save_model", "save_as": "save_model"}[args.action]
        if args.action in ("new", "open", "save_as") and not args.path:
            raise SystemExit(f"{args.action} 需要 --path。")
        path = None if args.action == "save" else args.path
        _print_json(call(method, _clean({
            "path": path, "confirm": args.confirm or None, "overwrite": args.overwrite or None,
            "template": args.template}), timeout=180))
    elif command == "raw":
        _print_json(call(args.method, _parse_json_arg(args.params), timeout=180))
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

    # launch / window-shot 不需要（或不能先）連 Revit 伺服器
    if args.command == "launch":
        from . import launcher

        result = launcher.launch(path=args.path, wait_seconds=args.wait, host=args.host, port=args.port)
        _print_json(result)
        return 0 if result.get("ready") else 2
    if args.command == "window-shot":
        try:
            return _window_shot(args)
        except Exception as exc:
            sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
            return 3

    try:
        with RevitClient(host=args.host, port=args.port, timeout=args.timeout) as client:
            return dispatch(args, client)
    except RevitConnectionError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except RevitCommandError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
