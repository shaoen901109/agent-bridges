# encoding: utf-8
"""Power Automate Desktop Agent Bridge 的命令列介面。

用途：除錯、備援（agent 沒有 MCP 時）、批次腳本。所有輸出都是 JSON。

    python -m pad_bridge flows
    python -m pad_bridge run 我的流程 --input Msg=你好 --input Count=3 --timeout 300
    python -m pad_bridge run 我的流程 --inputs-json "{\\"Msg\\": \\"你好\\"}" --no-wait
    python -m pad_bridge run-status <run_id>
    python -m pad_bridge runs --flow 我的流程 --limit 5
    python -m pad_bridge script 我的流程
    python -m pad_bridge create 新流程 --script-file flow.robin --spec-file vars.json
    python -m pad_bridge update 舊流程 --script-file flow.robin
    python -m pad_bridge search-actions 取代
    python -m pad_bridge template 取代文字
    python -m pad_bridge stop 我的流程
    python -m pad_bridge dialogs [--dismiss]

vars.json 格式：{"inputs": [{"name": "Msg", "type": "text", "default": "hi"}], "outputs": [{"name": "Result"}]}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from . import console, designer, runs
from .uia import PadUiError


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _parse_value(text: str) -> Any:
    """--input 的值：true/false → 布林，可轉數字就轉數字，其餘當文字。"""
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def _collect_inputs(args) -> Dict[str, Any]:
    inputs: Dict[str, Any] = {}
    if args.inputs_json:
        inputs.update(json.loads(args.inputs_json))
    for item in args.input or []:
        if "=" not in item:
            raise SystemExit(f"--input 格式應為 名稱=值，收到 {item!r}")
        key, value = item.split("=", 1)
        inputs[key] = value if args.raw_text else _parse_value(value)
    return inputs


def _read_script(args) -> Any:
    if args.script_file == "-":
        return sys.stdin.read()
    if args.script_file:
        return Path(args.script_file).read_text(encoding="utf-8-sig")
    return None


def _read_spec(args) -> Dict[str, Any]:
    if not args.spec_file:
        return {}
    return json.loads(Path(args.spec_file).read_text(encoding="utf-8-sig"))


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="pad-bridge", description="操作 Power Automate Desktop 桌面流程")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("flows", help="列出流程")

    r = sub.add_parser("run", help="執行流程")
    r.add_argument("flow")
    r.add_argument("--id", action="store_true", help="flow 參數是流程 ID 而不是名稱")
    r.add_argument("--input", action="append", help="名稱=值，可重複")
    r.add_argument("--inputs-json", help="整包輸入的 JSON")
    r.add_argument("--raw-text", action="store_true", help="--input 的值一律當文字")
    r.add_argument("--no-wait", action="store_true")
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--auto-confirm", action="store_true", help="替這次觸發自動按 PAD 的確認框")

    s = sub.add_parser("run-status", help="查詢某次執行")
    s.add_argument("run_id")
    s.add_argument("--tail", type=int, default=10)

    rs = sub.add_parser("runs", help="最近的執行紀錄")
    rs.add_argument("--flow")
    rs.add_argument("--limit", type=int, default=10)

    sc = sub.add_parser("script", help="讀出流程的 Robin 腳本")
    sc.add_argument("flow")

    for name in ("create", "update"):
        c = sub.add_parser(name, help="建立流程" if name == "create" else "修改流程")
        c.add_argument("flow")
        c.add_argument("--script-file", help="Robin 腳本檔（- 代表 stdin）")
        c.add_argument("--spec-file", help="變數規格 JSON 檔")
        if name == "create":
            c.add_argument("--power-fx", action="store_true")

    sa = sub.add_parser("search-actions", help="搜尋動作目錄")
    sa.add_argument("query")
    sa.add_argument("--limit", type=int, default=30)

    tp = sub.add_parser("template", help="取得某動作的 Robin 語法範本")
    tp.add_argument("query")
    tp.add_argument("--action-id")

    st = sub.add_parser("stop", help="停止流程")
    st.add_argument("flow")

    d = sub.add_parser("dialogs", help="列出（或關閉）console 對話框")
    d.add_argument("--dismiss", action="store_true")

    args = p.parse_args(argv)
    try:
        if args.cmd == "flows":
            _print_json(console.list_flows())
        elif args.cmd == "run":
            kw = {"flow_id": args.flow} if args.id else {"flow_name": args.flow}
            result = console.run_flow(
                inputs=_collect_inputs(args) or None,
                wait=not args.no_wait,
                timeout=args.timeout,
                auto_confirm=True if args.auto_confirm else None,
                **kw,
            )
            _print_json(result)
            return 0 if result.get("status") in ("Succeeded", "still_running") or args.no_wait else 1
        elif args.cmd == "run-status":
            run_dir = runs.find_run_dir(args.run_id)
            if not run_dir:
                raise SystemExit(f"找不到執行紀錄 {args.run_id}")
            _print_json(runs.summarize_run(run_dir, action_tail=args.tail))
        elif args.cmd == "runs":
            _print_json(runs.list_runs(flow_name=args.flow, limit=args.limit))
        elif args.cmd == "script":
            info = designer.get_flow_script(args.flow)
            _print_json(info)
        elif args.cmd == "create":
            spec = _read_spec(args)
            _print_json(designer.create_flow(args.flow, _read_script(args) or "", spec.get("inputs"), spec.get("outputs"), args.power_fx))
        elif args.cmd == "update":
            spec = _read_spec(args)
            _print_json(designer.update_flow(args.flow, _read_script(args), spec.get("inputs"), spec.get("outputs")))
        elif args.cmd == "search-actions":
            _print_json(designer.search_actions(args.query, args.limit))
        elif args.cmd == "template":
            _print_json(designer.action_template(args.query, args.action_id))
        elif args.cmd == "stop":
            _print_json(console.stop_flow(args.flow))
        elif args.cmd == "dialogs":
            _print_json(console.dismiss_dialogs() if args.dismiss else console.get_dialogs())
    except PadUiError as exc:
        _print_json({"error": "PAD_UI_ERROR", "message": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
