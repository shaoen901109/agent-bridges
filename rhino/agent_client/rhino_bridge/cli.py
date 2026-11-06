# encoding: utf-8
"""Rhino Agent Bridge 的命令列介面。

用途：除錯、備援（agent 沒有 MCP 時）、批次腳本。
所有輸出都是 JSON（截圖類會印出檔案路徑）。

結束碼：0 成功 / 1 指令失敗 / 2 連不上 Rhino / 3 截圖失敗。

    python -m rhino_bridge launch
    python -m rhino_bridge ping
    python -m rhino_bridge info
    python -m rhino_bridge box --width 10 --depth 20 --height 30
    python -m rhino_bridge eval --file code.py
    echo "result = doc.Objects.Count" | python -m rhino_bridge eval -
    python -m rhino_bridge screenshot --out C:\\temp\\view.png
    python -m rhino_bridge gh-status
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
    RhinoClient,
    RhinoCommandError,
    RhinoConnectionError,
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


def _parse_ids(values: Optional[List[str]]) -> Optional[List[str]]:
    if not values:
        return None
    ids: List[str] = []
    for value in values:
        for part in str(value).replace(" ", "").split(","):
            if part:
                ids.append(part)
    return ids


def _parse_json_arg(text: Optional[str]) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"不是合法的 JSON：{text!r}（{exc}）") from exc


def _port_value(text: Optional[str]) -> Any:
    """埠參數：純數字當索引（整數），其餘當名稱。"""
    if text is None:
        return None
    return int(text) if text.lstrip("-").isdigit() else text


def _json_or_text(text: Optional[str]) -> Any:
    """gh set 的 value：能解析成 JSON 就用 JSON（數字 / 陣列 / 布林），否則當字串。"""
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
        prog="rhino_bridge",
        description="Rhino 代理操控插件 — 命令列介面（含 Grasshopper）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=os.environ.get("RHINO_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RHINO_BRIDGE_PORT", DEFAULT_PORT)))
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("RHINO_BRIDGE_TIMEOUT", DEFAULT_TIMEOUT)), help="秒，預設 30")
    parser.add_argument("--units", default=None, help="長度單位：mm/cm/m/km/in/ft；省略 = 模型單位")

    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser("launch", help="啟動 Rhino（若未執行）並等橋接器就緒")
    p_launch.add_argument("--path", help="啟動時一併開啟的 .3dm")
    p_launch.add_argument("--wait", type=float, default=90.0, help="等待秒數，預設 90")

    sub.add_parser("ping", help="測試連線")
    sub.add_parser("commands", help="列出伺服器支援的所有指令")
    sub.add_parser("info", help="Rhino 與模型基本資訊")

    p_scene = sub.add_parser("scene", help="模型全貌（統計/圖層/邊界）")
    p_scene.add_argument("--limit", type=int, default=200)

    p_objs = sub.add_parser("objects", help="列出物件")
    p_objs.add_argument("--types", nargs="*", help="例如 brep curve mesh")
    p_objs.add_argument("--layer")
    p_objs.add_argument("--name-contains")
    p_objs.add_argument("--selected", action="store_true")
    p_objs.add_argument("--hidden", action="store_true", help="包含隱藏物件")
    p_objs.add_argument("--limit", type=int, default=100)
    p_objs.add_argument("--offset", type=int, default=0)

    p_select = sub.add_parser("select", help="設定選取集")
    p_select.add_argument("ids", nargs="*", help="GUID")
    p_select.add_argument("--clear", action="store_true")
    p_select.add_argument("--add", action="store_true")

    p_delete = sub.add_parser("delete", help="刪除物件")
    p_delete.add_argument("ids", nargs="+")

    p_shot = sub.add_parser("screenshot", help="視埠截圖（ViewCapture）")
    p_shot.add_argument("--out", help="輸出檔案路徑（預設存到系統暫存資料夾）")
    p_shot.add_argument("--width", type=int)
    p_shot.add_argument("--height", type=int)
    p_shot.add_argument("--viewport")
    p_shot.add_argument("--transparent", action="store_true")
    p_shot.add_argument("--max-edge", type=int, default=0, help=">0 時縮圖到指定最長邊")

    p_window = sub.add_parser("window-shot", help="全視窗截圖（含命令列/面板/對話框，僅觀看）")
    p_window.add_argument("--out")
    p_window.add_argument("--target", choices=["rhino", "grasshopper"], default="rhino")
    p_window.add_argument("--max-edge", type=int, default=0)
    p_window.add_argument("--restore", action="store_true", help="視窗最小化時先還原")
    p_window.add_argument("--list", action="store_true", help="只列出找到的 Rhino 視窗")

    p_view = sub.add_parser("view", help="設定視埠")
    p_view.add_argument("--view", choices=["top", "bottom", "front", "back", "left", "right", "perspective", "iso"])
    p_view.add_argument("--display-mode")
    p_view.add_argument("--eye")
    p_view.add_argument("--target")
    p_view.add_argument("--projection", choices=["perspective", "parallel"])
    p_view.add_argument("--viewport")
    p_view.add_argument("--zoom-extents", action="store_true")
    p_view.add_argument("--selected", action="store_true", help="zoom 到選取物件")

    def style(p: argparse.ArgumentParser) -> None:
        p.add_argument("--name")
        p.add_argument("--layer")
        p.add_argument("--color", help='"#RRGGBB" 或 JSON [r,g,b]')

    p_box = sub.add_parser("box", help="建立方塊")
    p_box.add_argument("--width", type=float, required=True)
    p_box.add_argument("--depth", type=float, required=True)
    p_box.add_argument("--height", type=float, required=True)
    p_box.add_argument("--origin", default="0,0,0")
    p_box.add_argument("--centered", action="store_true")
    style(p_box)

    p_sph = sub.add_parser("sphere", help="建立球")
    p_sph.add_argument("--radius", type=float, required=True)
    p_sph.add_argument("--center", default="0,0,0")
    style(p_sph)

    p_cyl = sub.add_parser("cylinder", help="建立圓柱")
    p_cyl.add_argument("--radius", type=float, required=True)
    p_cyl.add_argument("--height", type=float, required=True)
    p_cyl.add_argument("--base", default="0,0,0")
    p_cyl.add_argument("--axis")
    p_cyl.add_argument("--no-cap", action="store_true")
    style(p_cyl)

    p_cir = sub.add_parser("circle", help="建立圓（曲線）")
    p_cir.add_argument("--radius", type=float, required=True)
    p_cir.add_argument("--center", default="0,0,0")
    p_cir.add_argument("--normal")
    style(p_cir)

    p_pl = sub.add_parser("polyline", help="建立多段線")
    p_pl.add_argument("--points", required=True, help='JSON，例如 "[[0,0,0],[10,0,0],[10,10,0]]"')
    p_pl.add_argument("--closed", action="store_true")
    style(p_pl)

    p_cv = sub.add_parser("curve", help="建立 NURBS 曲線")
    p_cv.add_argument("--points", required=True)
    p_cv.add_argument("--degree", type=int, default=3)
    p_cv.add_argument("--mode", choices=["control", "interpolate"], default="control")
    p_cv.add_argument("--closed", action="store_true")
    style(p_cv)

    p_ex = sub.add_parser("extrude", help="擠出曲線 / 曲面")
    p_ex.add_argument("ids", nargs="+")
    p_ex.add_argument("--height", type=float, required=True)
    p_ex.add_argument("--direction")
    p_ex.add_argument("--no-cap", action="store_true")
    p_ex.add_argument("--delete-input", action="store_true")
    style(p_ex)

    p_bool = sub.add_parser("boolean", help="布林運算")
    p_bool.add_argument("operation", choices=["union", "difference", "intersection"])
    p_bool.add_argument("--a", nargs="+", required=True)
    p_bool.add_argument("--b", nargs="+", required=True)
    p_bool.add_argument("--keep-inputs", action="store_true")

    p_loft = sub.add_parser("loft", help="放樣")
    p_loft.add_argument("ids", nargs="+")
    p_loft.add_argument("--type", dest="loft_type", default="normal")
    p_loft.add_argument("--closed", action="store_true")
    p_loft.add_argument("--cap", action="store_true")
    p_loft.add_argument("--delete-input", action="store_true")

    p_tr = sub.add_parser("transform", help="移動/旋轉/縮放/鏡射")
    p_tr.add_argument("ids", nargs="+")
    p_tr.add_argument("--translate")
    p_tr.add_argument("--rotate", help='JSON，例如 {"origin":[0,0,0],"axis":[0,0,1],"angle_deg":45}')
    p_tr.add_argument("--scale", help="數值或 JSON 陣列 [sx,sy,sz]")
    p_tr.add_argument("--scale-origin")
    p_tr.add_argument("--mirror", help='JSON，例如 {"origin":[0,0,0],"normal":[1,0,0]}')
    p_tr.add_argument("--copy", action="store_true")

    p_lay = sub.add_parser("layer", help="移到圖層 / 編輯圖層")
    p_lay.add_argument("layer")
    p_lay.add_argument("ids", nargs="*")
    p_lay.add_argument("--color")
    p_lay.add_argument("--visible", choices=["true", "false"])
    p_lay.add_argument("--locked", choices=["true", "false"])
    p_lay.add_argument("--current", action="store_true")

    p_mat = sub.add_parser("material", help="設定顏色 / 材質")
    p_mat.add_argument("ids", nargs="+")
    p_mat.add_argument("--material")
    p_mat.add_argument("--color")
    p_mat.add_argument("--transparency", type=float)

    p_eval = sub.add_parser("eval", help="執行任意 IronPython 2.7")
    p_eval.add_argument("code", nargs="?", help='Python 程式碼；用 "-" 表示從 stdin 讀取')
    p_eval.add_argument("--file", help="從檔案讀取程式碼（UTF-8）")

    p_run = sub.add_parser("run", help="執行 Rhino 指令（命令列腳本）")
    p_run.add_argument("command_text", nargs="+", help='例如 "_-Box 0,0,0 10,10,10 5"')
    p_run.add_argument("--echo", action="store_true")
    p_run.add_argument("--continue-on-error", action="store_true")

    p_can = sub.add_parser("cancel", help="取消 / 結束卡在提示的指令")
    p_can.add_argument("--how", choices=["escape", "enter"], default="escape")

    p_undo = sub.add_parser("undo", help="復原")
    p_undo.add_argument("--times", type=int, default=1)
    p_redo = sub.add_parser("redo", help="重做")
    p_redo.add_argument("--times", type=int, default=1)

    p_model = sub.add_parser("model", help="開新檔 / 開啟 / 儲存 / 另存 / 匯出")
    p_model.add_argument("action", choices=["new", "open", "save", "save_as", "export"])
    p_model.add_argument("--path")
    p_model.add_argument("--confirm", action="store_true")
    p_model.add_argument("--overwrite", action="store_true")
    p_model.add_argument("--selected", action="store_true", help="export 只匯出選取物件")

    # --- Grasshopper ---
    sub.add_parser("gh-status", help="Grasshopper 狀態")
    p_ghe = sub.add_parser("gh-editor", help="載入 / 顯示 GH 編輯器")
    p_ghe.add_argument("--hide", action="store_true")
    p_ghn = sub.add_parser("gh-new", help="新增 GH 文件")
    p_ghn.add_argument("--show", action="store_true")
    p_gho = sub.add_parser("gh-open", help="開啟 .gh / .ghx")
    p_gho.add_argument("path")
    p_gho.add_argument("--show", action="store_true")
    p_ghs = sub.add_parser("gh-save", help="儲存 GH 文件")
    p_ghs.add_argument("--path")
    p_ghs.add_argument("--confirm", action="store_true")
    p_ghs.add_argument("--overwrite", action="store_true")

    p_ghq = sub.add_parser("gh-search", help="搜尋元件")
    p_ghq.add_argument("query")
    p_ghq.add_argument("--category")
    p_ghq.add_argument("--subcategory")
    p_ghq.add_argument("--limit", type=int, default=15)
    p_ghq.add_argument("--no-ports", action="store_true")

    p_gha = sub.add_parser("gh-add", help="放置元件")
    p_gha.add_argument("name", nargs="?")
    p_gha.add_argument("--guid")
    p_gha.add_argument("--category")
    p_gha.add_argument("--subcategory")
    p_gha.add_argument("--nickname")
    p_gha.add_argument("--x", type=float)
    p_gha.add_argument("--y", type=float)

    p_ghsl = sub.add_parser("gh-slider", help="新增 Number Slider")
    p_ghsl.add_argument("--min", type=float, required=True)
    p_ghsl.add_argument("--max", type=float, required=True)
    p_ghsl.add_argument("--value", type=float)
    p_ghsl.add_argument("--integer", action="store_true")
    p_ghsl.add_argument("--decimals", type=int, default=2)
    p_ghsl.add_argument("--nickname")

    p_ghp = sub.add_parser("gh-panel", help="新增 Panel")
    p_ghp.add_argument("--text")
    p_ghp.add_argument("--nickname")

    p_ght = sub.add_parser("gh-toggle", help="新增 Boolean Toggle")
    p_ght.add_argument("--value", choices=["true", "false"], default="false")
    p_ght.add_argument("--nickname")

    p_ghset = sub.add_parser("gh-set", help="設定值（slider/panel/toggle 或輸入埠）")
    p_ghset.add_argument("id")
    p_ghset.add_argument("value", help="JSON（數字 / [x,y,z] / true）或純文字")
    p_ghset.add_argument("--input", help="輸入埠名稱或索引（一般元件）")

    p_ghc = sub.add_parser("gh-connect", help="連線 source → target")
    p_ghc.add_argument("source")
    p_ghc.add_argument("target")
    p_ghc.add_argument("--source-output")
    p_ghc.add_argument("--target-input")
    p_ghc.add_argument("--replace", action="store_true")

    p_ghd = sub.add_parser("gh-disconnect", help="移除連線")
    p_ghd.add_argument("target")
    p_ghd.add_argument("--target-input")
    p_ghd.add_argument("--source")
    p_ghd.add_argument("--source-output")

    sub.add_parser("gh-list", help="列出 GH 文件內容（物件 / 連線 / 錯誤）")
    p_ghsv = sub.add_parser("gh-solve", help="求解")
    p_ghsv.add_argument("--keep", action="store_true", help="不要 expire 全部（只算髒的）")

    p_gho2 = sub.add_parser("gh-output", help="讀取輸出（或 --input）資料")
    p_gho2.add_argument("id")
    p_gho2.add_argument("--output")
    p_gho2.add_argument("--input")
    p_gho2.add_argument("--max-items", type=int, default=10)

    p_ghb = sub.add_parser("gh-bake", help="烘焙輸出到 Rhino")
    p_ghb.add_argument("id")
    p_ghb.add_argument("--output")
    p_ghb.add_argument("--layer")
    p_ghb.add_argument("--name")
    p_ghb.add_argument("--color")

    p_ghdel = sub.add_parser("gh-delete", help="刪除 GH 物件")
    p_ghdel.add_argument("ids", nargs="+")

    p_ghsh = sub.add_parser("gh-shot", help="GH 畫布截圖")
    p_ghsh.add_argument("--out")
    p_ghsh.add_argument("--no-zoom", action="store_true")
    p_ghsh.add_argument("--max-edge", type=int, default=0)

    p_ghev = sub.add_parser("gh-eval", help="執行 IronPython（含 GH SDK）")
    p_ghev.add_argument("code", nargs="?")
    p_ghev.add_argument("--file")

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
        raise SystemExit("沒有讀到 Python 程式碼。請用 `eval \"code\"`、`--file path` 或從 stdin 輸入。")
    return data


def _window_shot(args: argparse.Namespace) -> int:
    from . import window_capture

    if args.list:
        _print_json(window_capture.find_windows())
        return 0

    if args.target == "grasshopper":
        wins = window_capture.find_windows(process_name="Rhino.exe", title_contains="Grasshopper")
        if not wins:
            raise window_capture.WindowCaptureError("找不到 Grasshopper 編輯器視窗（先執行 gh-editor）。")
        captured = window_capture.capture_window(hwnd=wins[0]["hwnd"], restore_if_minimized=args.restore)
    else:
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


def _shot_result(client: RhinoClient, method: str, params: Dict[str, Any], args: argparse.Namespace) -> int:
    """截圖：Rhino 端存檔 → （必要時）縮圖 → 複製到使用者指定路徑。失敗回傳 3。"""
    try:
        result = client.call(method, params, timeout=120)
    except RhinoCommandError as exc:
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
            result["path"] = image_utils.save_image(image, args.out or image_utils.timestamped_path("viewport"))
        except OSError as exc:
            sys.stderr.write(f"截圖檔案無法讀取：{path}（{exc}）\n")
            return 3
    _print_json(result)
    return 0


def dispatch(args: argparse.Namespace, client: RhinoClient) -> int:
    units = args.units
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
    elif command == "objects":
        _print_json(call("list_objects", _clean({
            "types": args.types, "layer": args.layer, "name_contains": args.name_contains,
            "selected_only": args.selected or None, "include_hidden": args.hidden or None,
            "limit": args.limit, "offset": args.offset}), timeout=120))
    elif command == "select":
        params: Dict[str, Any] = {"clear": True} if args.clear else {"ids": _parse_ids(args.ids)}
        if args.add:
            params["add"] = True
        _print_json(call("select_objects", params))
    elif command == "delete":
        _print_json(call("delete_objects", {"ids": _parse_ids(args.ids)}))
    elif command == "screenshot":
        return _shot_result(client, "screenshot", _clean({
            "width": args.width, "height": args.height, "viewport": args.viewport,
            "transparent": args.transparent or None}), args)
    elif command == "gh-shot":
        return _shot_result(client, "gh_canvas_screenshot", {"zoom_extents": not args.no_zoom}, args)
    elif command == "window-shot":
        return _window_shot(args)
    elif command == "view":
        _print_json(call("set_view", _clean({
            "units": units, "view": args.view, "display_mode": args.display_mode,
            "eye": _parse_point(args.eye), "target": _parse_point(args.target),
            "projection": args.projection, "viewport": args.viewport,
            "zoom_extents": args.zoom_extents or None, "selected_only": args.selected or None})))
    elif command == "box":
        _print_json(call("create_box", _clean({
            "units": units, "origin": _parse_point(args.origin), "width": args.width, "depth": args.depth,
            "height": args.height, "centered": args.centered or None, "name": args.name,
            "layer": args.layer, "color": _json_or_text(args.color)})))
    elif command == "sphere":
        _print_json(call("create_sphere", _clean({
            "units": units, "radius": args.radius, "center": _parse_point(args.center), "name": args.name,
            "layer": args.layer, "color": _json_or_text(args.color)})))
    elif command == "cylinder":
        _print_json(call("create_cylinder", _clean({
            "units": units, "radius": args.radius, "height": args.height, "base": _parse_point(args.base),
            "axis": _parse_point(args.axis), "cap": not args.no_cap, "name": args.name,
            "layer": args.layer, "color": _json_or_text(args.color)})))
    elif command == "circle":
        _print_json(call("create_circle", _clean({
            "units": units, "radius": args.radius, "center": _parse_point(args.center),
            "normal": _parse_point(args.normal), "name": args.name, "layer": args.layer,
            "color": _json_or_text(args.color)})))
    elif command == "polyline":
        _print_json(call("create_polyline", _clean({
            "units": units, "points": _parse_json_arg(args.points), "closed": args.closed or None,
            "name": args.name, "layer": args.layer, "color": _json_or_text(args.color)})))
    elif command == "curve":
        _print_json(call("create_curve", _clean({
            "units": units, "points": _parse_json_arg(args.points), "degree": args.degree, "mode": args.mode,
            "closed": args.closed or None, "name": args.name, "layer": args.layer,
            "color": _json_or_text(args.color)})))
    elif command == "extrude":
        _print_json(call("extrude", _clean({
            "units": units, "ids": _parse_ids(args.ids), "height": args.height,
            "direction": _parse_point(args.direction), "cap": not args.no_cap,
            "delete_input": args.delete_input or None, "name": args.name, "layer": args.layer,
            "color": _json_or_text(args.color)})))
    elif command == "boolean":
        _print_json(call("boolean", {"operation": args.operation, "a": _parse_ids(args.a),
                                     "b": _parse_ids(args.b), "delete_inputs": not args.keep_inputs}))
    elif command == "loft":
        _print_json(call("loft", _clean({
            "ids": _parse_ids(args.ids), "loft_type": args.loft_type, "closed": args.closed or None,
            "cap": args.cap or None, "delete_input": args.delete_input or None})))
    elif command == "transform":
        scale = None
        if args.scale:
            scale = _parse_json_arg(args.scale) if args.scale.startswith("[") else float(args.scale)
        _print_json(call("transform_objects", _clean({
            "units": units, "ids": _parse_ids(args.ids), "translate": _parse_point(args.translate),
            "rotate": _parse_json_arg(args.rotate), "scale": scale,
            "scale_origin": _parse_point(args.scale_origin), "mirror": _parse_json_arg(args.mirror),
            "copy": args.copy or None})))
    elif command == "layer":
        _print_json(call("set_layer", _clean({
            "layer": args.layer, "ids": _parse_ids(args.ids), "color": _json_or_text(args.color),
            "visible": None if args.visible is None else args.visible == "true",
            "locked": None if args.locked is None else args.locked == "true",
            "current": args.current or None})))
    elif command == "material":
        _print_json(call("set_material", _clean({
            "ids": _parse_ids(args.ids), "material": args.material, "color": _json_or_text(args.color),
            "transparency": args.transparency})))
    elif command == "eval":
        _print_json(call("eval_python", {"code": _read_code(args)}, timeout=120))
    elif command == "run":
        _print_json(call("run_command", _clean({
            "command": args.command_text, "echo": args.echo or None,
            "stop_on_error": not args.continue_on_error}), timeout=120))
    elif command == "cancel":
        _print_json(call("cancel_command", {"how": args.how}))
    elif command == "undo":
        _print_json(call("undo", {"times": args.times}))
    elif command == "redo":
        _print_json(call("redo", {"times": args.times}))
    elif command == "model":
        if args.action == "export":
            method = "export_model"
        else:
            method = {"new": "new_model", "open": "open_model", "save": "save_model",
                      "save_as": "save_model"}[args.action]
        _print_json(call(method, _clean({
            "path": args.path, "confirm": args.confirm or None, "overwrite": args.overwrite or None,
            "selected_only": (args.selected or None) if args.action == "export" else None}), timeout=180))
    # --- Grasshopper ---
    elif command == "gh-status":
        _print_json(call("gh_status", timeout=120))
    elif command == "gh-editor":
        _print_json(call("gh_open_editor", {"show": not args.hide}, timeout=120))
    elif command == "gh-new":
        _print_json(call("gh_new_document", {"show": args.show}, timeout=120))
    elif command == "gh-open":
        _print_json(call("gh_open_document", {"path": args.path, "show": args.show}, timeout=120))
    elif command == "gh-save":
        _print_json(call("gh_save_document", _clean({
            "path": args.path, "confirm": args.confirm or None, "overwrite": args.overwrite or None})))
    elif command == "gh-search":
        _print_json(call("gh_search_components", _clean({
            "query": args.query, "category": args.category, "subcategory": args.subcategory,
            "limit": args.limit, "with_ports": not args.no_ports}), timeout=120))
    elif command == "gh-add":
        _print_json(call("gh_add_component", _clean({
            "name": args.name, "guid": args.guid, "category": args.category,
            "subcategory": args.subcategory, "nickname": args.nickname, "x": args.x, "y": args.y}),
            timeout=120))
    elif command == "gh-slider":
        _print_json(call("gh_add_slider", _clean({
            "min": args.min, "max": args.max, "value": args.value, "integer": args.integer or None,
            "decimals": args.decimals, "nickname": args.nickname})))
    elif command == "gh-panel":
        _print_json(call("gh_add_panel", _clean({"text": args.text, "nickname": args.nickname})))
    elif command == "gh-toggle":
        _print_json(call("gh_add_toggle", _clean({"value": args.value == "true", "nickname": args.nickname})))
    elif command == "gh-set":
        _print_json(call("gh_set_input", _clean({
            "id": args.id, "value": _json_or_text(args.value), "input": _port_value(args.input)})))
    elif command == "gh-connect":
        _print_json(call("gh_connect", _clean({
            "source": args.source, "target": args.target, "source_output": _port_value(args.source_output),
            "target_input": _port_value(args.target_input), "replace": args.replace or None})))
    elif command == "gh-disconnect":
        _print_json(call("gh_disconnect", _clean({
            "target": args.target, "target_input": _port_value(args.target_input), "source": args.source,
            "source_output": _port_value(args.source_output)})))
    elif command == "gh-list":
        _print_json(call("gh_list_document", timeout=120))
    elif command == "gh-solve":
        _print_json(call("gh_solve", {"expire_all": not args.keep}, timeout=180))
    elif command == "gh-output":
        _print_json(call("gh_get_output", _clean({
            "id": args.id, "output": _port_value(args.output), "input": _port_value(args.input),
            "max_items": args.max_items})))
    elif command == "gh-bake":
        _print_json(call("gh_bake", _clean({
            "id": args.id, "output": _port_value(args.output), "layer": args.layer, "name": args.name,
            "color": _json_or_text(args.color)}), timeout=120))
    elif command == "gh-delete":
        _print_json(call("gh_delete_objects", {"ids": _parse_ids(args.ids)}))
    elif command == "gh-eval":
        _print_json(call("gh_eval_python", {"code": _read_code(args)}, timeout=120))
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

    # launch / window-shot 不需要（或不能先）連 Rhino 伺服器
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
        with RhinoClient(host=args.host, port=args.port, timeout=args.timeout) as client:
            return dispatch(args, client)
    except RhinoConnectionError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except RhinoCommandError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
