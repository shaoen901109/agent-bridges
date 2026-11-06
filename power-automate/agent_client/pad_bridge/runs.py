# encoding: utf-8
"""讀 PAD 寫在本機的執行紀錄（純檔案，不碰 UI）。

每次執行都會在
    %LOCALAPPDATA%\\Microsoft\\Power Automate Desktop\\Console\\Scripts\\<flowId>\\Runs\\<runId>\\
留下 RunDefinition.json（狀態、輸出變數、起訖時間）與 Actions.log（每個動作一行 JSON）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

PAD_ROOT = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Power Automate Desktop"
SCRIPTS_DIR = PAD_ROOT / "Console" / "Scripts"
WORKSPACE_DIR = PAD_ROOT / "Console" / "Workspace"
DESIGNER_DATA_DIR = PAD_ROOT / "Designer" / "Data"


def parse_time(text: Optional[str]) -> Optional[datetime]:
    """PAD 的時間是 7 位小數的 ISO 字串（…Z），Python 只吃 6 位。"""
    if not text:
        return None
    t = text.rstrip("Z")
    if "." in t:
        head, frac = t.split(".", 1)
        t = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None  # 執行中檔案可能寫到一半


def iter_run_dirs(flow_id: Optional[str] = None) -> Iterator[Path]:
    if not SCRIPTS_DIR.is_dir():
        return
    flows = [SCRIPTS_DIR / flow_id] if flow_id else [p for p in SCRIPTS_DIR.iterdir() if p.is_dir()]
    for flow_dir in flows:
        runs = flow_dir / "Runs"
        if runs.is_dir():
            yield from (p for p in runs.iterdir() if p.is_dir())


def find_run_dir(run_id: str) -> Optional[Path]:
    for p in SCRIPTS_DIR.glob(f"*/Runs/{run_id}"):
        if p.is_dir():
            return p
    return None


def read_actions(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / "Actions.log"
    out: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _brief_action(a: Dict[str, Any]) -> Dict[str, Any]:
    item = {
        "index": a.get("index"),
        "name": a.get("name"),
        "action": f"{a.get('systemModuleName')}.{a.get('systemActionName')}",
        "function": a.get("functionName"),
        "status": a.get("status"),
    }
    err = a.get("error")
    if isinstance(err, dict):
        # message 後半段是 .NET 堆疊，對 agent 沒用
        message = (err.get("message") or "").split("\r\n \r\n")[0].split("\r\n")[0].strip()
        item["error"] = {"code": err.get("code"), "message": message}
    elif err:
        item["error"] = err
    return item


def summarize_run(run_dir: Path, action_tail: int = 5) -> Dict[str, Any]:
    """整理成 agent 好讀的摘要：狀態、輸出、耗時、失敗動作、最後幾個動作。"""
    rd = _read_json(run_dir / "RunDefinition.json") or {}
    actions = read_actions(run_dir)
    start, end = parse_time(rd.get("startTime")), parse_time(rd.get("endTime"))
    wf = rd.get("workflow") or {}
    failed = [_brief_action(a) for a in actions if a.get("status") not in ("Succeeded", None)]
    return {
        "run_id": run_dir.name,
        "flow_name": wf.get("name"),
        "flow_id": wf.get("workflowId") or run_dir.parent.parent.name,
        "status": rd.get("status") or "Running",
        "finished": bool(rd.get("endTime")),
        "start_time": rd.get("startTime"),
        "end_time": rd.get("endTime"),
        "duration_s": round((end - start).total_seconds(), 2) if start and end else None,
        "outputs": rd.get("outputs") or {},
        "actions_executed": (rd.get("executionStatistics") or {}).get("actionsExecutedCount", len(actions)),
        "not_succeeded_actions": failed[:20],
        "last_actions": [_brief_action(a) for a in actions[-action_tail:]] if action_tail else [],
        "log_dir": str(run_dir),
    }


def list_runs(flow_id: Optional[str] = None, flow_name: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    items = []
    for run_dir in iter_run_dirs(flow_id):
        rd = _read_json(run_dir / "RunDefinition.json")
        if rd is None:
            continue
        if flow_name and (rd.get("workflow") or {}).get("name") != flow_name:
            continue
        items.append((parse_time(rd.get("startTime")) or datetime.min.replace(tzinfo=timezone.utc), run_dir))
    items.sort(key=lambda x: x[0], reverse=True)
    return [summarize_run(d, action_tail=0) for _, d in items[:limit]]


def newest_run_since(since: datetime, flow_name: Optional[str], flow_id: Optional[str]) -> Optional[Path]:
    """PAD 在前一個 URL 請求失敗後，有時會沿用舊的 runId；用「觸發後最新一筆」當備援。"""
    best = None
    for run_dir in iter_run_dirs():
        rd = _read_json(run_dir / "RunDefinition.json")
        if not rd:
            continue
        wf = rd.get("workflow") or {}
        if flow_id and wf.get("workflowId") != flow_id:
            continue
        if flow_name and not flow_id and wf.get("name") != flow_name:
            continue
        st = parse_time(rd.get("startTime"))
        if st and st >= since and (best is None or st > best[0]):
            best = (st, run_dir)
    return best[1] if best else None


def known_flow_ids() -> Dict[str, str]:
    """從執行紀錄整理出 流程名稱 → 流程 ID（只有跑過的流程才查得到）。"""
    mapping: Dict[str, tuple] = {}
    for run_dir in iter_run_dirs():
        rd = _read_json(run_dir / "RunDefinition.json")
        if not rd:
            continue
        wf = rd.get("workflow") or {}
        name, fid = wf.get("name"), wf.get("workflowId")
        st = parse_time(rd.get("startTime"))
        if name and fid and st and (name not in mapping or st > mapping[name][0]):
            mapping[name] = (st, fid)
    return {k: v[1] for k, v in mapping.items()}
