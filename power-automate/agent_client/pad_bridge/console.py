# encoding: utf-8
"""操作 Power Automate Desktop 的 console：列流程、用 run URL 觸發、等結果、停止。

觸發方式是官方的 run URL（ms-powerautomate:/console/flow/run?...），
執行結果從本機執行紀錄讀取（見 runs.py），console 跳出的對話框用 UIA 處理。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import runs
from .uia import (
    PadUiError,
    by_id,
    describe,
    find,
    find_all,
    get_text,
    get_value,
    invoke,
    top_windows,
    ui_task,
    wait_for,
)

CONSOLE_PROCESS = "PAD.Console.Host"
URL_MAX = 2000  # ShellExecute 對協定 URL 的長度上限約 2048

# 使用者自己決定要不要讓 agent 自動按「外部來源正在嘗試執行以下流程」的「繼續」。
# 預設關閉：對話框會留著等使用者按，或請使用者在 PAD 設定裡關掉這個確認。
AUTO_CONFIRM = os.environ.get("PAD_BRIDGE_AUTO_CONFIRM", "").strip().lower() in ("1", "true", "yes")

_CLOSE_NAMES = ("關閉", "确定", "確定", "Close", "OK")


# --- URL --------------------------------------------------------------------


def _escape_for_quoted_arg(text: str) -> str:
    """PAD 的協定處理是 `PAD.Console.Host.exe "%1"`，URL 會被塞進一對雙引號。

    依 Windows 命令列規則跳脫：雙引號前的反斜線要加倍，雙引號本身變成 \\"。
    """
    out, backslashes = [], 0
    for ch in text:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            out.append("\\" * (backslashes * 2) + '\\"')
        else:
            out.append("\\" * backslashes + ch)
        backslashes = 0
    out.append("\\" * (backslashes * 2))
    return "".join(out)


def build_run_url(
    flow_name: Optional[str] = None,
    flow_id: Optional[str] = None,
    inputs: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
) -> str:
    if not flow_name and not flow_id:
        raise ValueError("flow_name 或 flow_id 至少要給一個")
    for name, value in (("flow_name", flow_name), ("flow_id", flow_id)):
        if value and re.search(r'[&#"]', value):
            raise ValueError(f"{name} 含有 & # \" 字元，run URL 無法表達，請改用另一種識別方式")
    parts = [f"workflowId={flow_id}" if flow_id else f"workflowName={flow_name}"]
    if run_id:
        parts.append(f"runId={run_id}")
    if inputs:
        for key, value in inputs.items():
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"輸入 {key!r} 的型別 {type(value).__name__} 不支援；run URL 只接受文字、數字、布林")
        text = json.dumps(inputs, ensure_ascii=False, separators=(",", ":"))
        # PAD 不會對 URL 做解碼，& 與 # 會截斷參數；JSON 允許 \uXXXX，用它繞過
        text = text.replace("&", "\\u0026").replace("#", "\\u0023")
        parts.append("inputArguments=" + text)
    url = "ms-powerautomate:/console/flow/run?" + "&".join(parts)
    return _escape_for_quoted_arg(url)


def _launch(url: str) -> None:
    os.startfile(url)  # noqa: S606 - 走 Windows 協定處理程式


# --- 視窗與對話框 -----------------------------------------------------------


def console_window():
    for w in top_windows([CONSOLE_PROCESS]):
        if w.Name and w.ControlTypeName == "WindowControl":
            return w
    return None


def ensure_console(timeout: float = 60.0):
    """確保 console 開著；沒開就用協定喚起。"""
    win = console_window()
    if win and by_id(win, "MyFlowsListGrid"):
        return win
    if not win:
        _launch("ms-powerautomate:")
    win = wait_for(lambda: (lambda w: w if w and by_id(w, "MyFlowsListGrid") else None)(console_window()), timeout)
    if not win:
        raise PadUiError("Power Automate console 沒有開啟或尚未登入（找不到流程清單）。請使用者開啟 Power Automate 並登入。")
    return win


def _dialogs(win) -> List[Any]:
    out = []
    for w in find_all(win, max_depth=8, ControlTypeName="WindowControl"):
        # 滑鼠停在清單上時的 tooltip 也是 Window，排除
        if not w.Name and any(c.ControlTypeName == "ToolTipControl" for c in w.GetChildren()):
            continue
        out.append(w)
    return out


def _button_in(dialog, names=_CLOSE_NAMES):
    """PAD 對話框的按鈕多半是 DataItem(名稱) 包著 Button(id=Button)。"""
    for name in names:
        holder = find(dialog, Name=name, ControlTypeName="DataItemControl")
        if holder:
            btn = by_id(holder, "Button") or find(holder, ControlTypeName="ButtonControl")
            if btn:
                return btn
        btn = find(dialog, Name=name, ControlTypeName="ButtonControl")
        if btn:
            return btn
    return None


def classify_dialog(dialog) -> Dict[str, Any]:
    title = dialog.Name or ""
    ok = by_id(dialog, "OKRunFlowFromProtocolHandlerButton")
    if ok:
        msg = by_id(dialog, "RunFlowFromProtocolHandlerDialogMessage")
        text = (msg.Name if msg else "") or ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return {"kind": "confirm_run", "title": title, "flow_name": lines[1] if len(lines) > 1 else None, "text": text}
    outputs_list = by_id(dialog, "OutputsList")
    if outputs_list:
        names = find_all(outputs_list, AutomationId="OutputNameLabel")
        values = find_all(outputs_list, AutomationId="VariableDefaultValueTextBox")
        outputs = {n.Name: get_value(v) for n, v in zip(names, values)}
        return {"kind": "outputs", "title": title, "outputs": outputs}
    rich = by_id(dialog, "RichText")
    if rich:
        detail = by_id(dialog, "DetailsExpander")
        return {
            "kind": "error",
            "title": title,
            "text": get_text(rich),
            "details": [ln for ln in describe(detail, 10)] if detail else [],
        }
    return {"kind": "unknown", "title": title, "content": describe(dialog, 40)}


def _close_dialog(dialog) -> bool:
    btn = _button_in(dialog)
    if btn:
        invoke(btn)
        return True
    close = by_id(dialog, "PART_CloseButton")
    if close:
        invoke(close)
        return True
    return False


@ui_task
def get_dialogs() -> List[Dict[str, Any]]:
    win = console_window()
    if not win:
        return []
    return [classify_dialog(d) for d in _dialogs(win)]


@ui_task
def dismiss_dialogs(kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """關掉 console 上的資訊性對話框（錯誤、輸出）。不會替使用者按「繼續執行」。"""
    kinds = kinds or ["error", "outputs"]
    win = console_window()
    closed = []
    if not win:
        return closed
    for d in _dialogs(win):
        info = classify_dialog(d)
        if info["kind"] in kinds and _close_dialog(d):
            closed.append(info)
    return closed


# --- 流程清單 ---------------------------------------------------------------


def _flow_rows(win) -> List[Any]:
    grid = by_id(win, "MyFlowsListGrid")
    if not grid:
        return []
    return [r for r in grid.GetChildren() if r.ControlTypeName == "DataItemControl"]


def _row_info(row) -> Dict[str, Any]:
    status = by_id(row, "ProcessStateStatusLabel")
    name = row.Name
    skip = {name, status.Name if status else None, "切換選取項目", "Toggle selection"}
    # 各欄是列底下的 Custom 格子（名稱、修改時間、狀態）；名稱格內還藏著重新命名用的文字，不能往下找
    cells = [(c.Name or "").strip() for c in row.GetChildren() if c.ControlTypeName == "CustomControl"]
    modified = next((t for t in cells if t and t not in skip), None)
    return {"name": name, "status": status.Name if status else None, "modified": modified}


@ui_task
def list_flows() -> Dict[str, Any]:
    win = ensure_console()
    ids = runs.known_flow_ids()
    flows = []
    for row in _flow_rows(win):
        info = _row_info(row)
        info["id"] = ids.get(info["name"])
        flows.append(info)
    account = by_id(win, "AccountInfoButton")
    account_text = by_id(account, "ButtonText") if account else None
    return {"account": account_text.Name if account_text else None, "flows": flows}


def _find_row(win, flow_name: str):
    for row in _flow_rows(win):
        if row.Name == flow_name:
            return row
    return None


@ui_task
def stop_flow(flow_name: str) -> Dict[str, Any]:
    win = ensure_console()
    row = _find_row(win, flow_name)
    if not row:
        raise PadUiError(f"找不到流程「{flow_name}」")
    btn = by_id(row, "StopFlowButton")
    if not btn or not btn.IsEnabled:
        return {"stopped": False, "reason": "流程沒有在執行"}
    invoke(btn)
    return {"stopped": True}


# --- 執行 -------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@ui_task
def _scan_after_trigger(flow_name: Optional[str], confirm: bool) -> List[Dict[str, Any]]:
    """處理觸發後 console 上出現的對話框，回傳看到的事件。"""
    win = console_window()
    events = []
    if not win:
        return events
    for d in _dialogs(win):
        info = classify_dialog(d)
        if info["kind"] == "confirm_run":
            # 只替「自己剛剛要求的那個流程」按繼續，其他來源的請求一律不碰
            if confirm and flow_name and info.get("flow_name") == flow_name:
                invoke(by_id(d, "OKRunFlowFromProtocolHandlerButton"))
                info["action"] = "confirmed"
            else:
                info["action"] = "waiting_for_user"
        elif info["kind"] in ("outputs", "error"):
            _close_dialog(d)
            info["action"] = "closed"
        events.append(info)
    return events


@ui_task
def _flow_row_status(flow_name: str) -> Optional[str]:
    win = console_window()
    row = _find_row(win, flow_name) if win else None
    if not row:
        return None
    return _row_info(row)["status"]


def run_flow(
    flow_name: Optional[str] = None,
    flow_id: Optional[str] = None,
    inputs: Optional[Dict[str, Any]] = None,
    wait: bool = True,
    timeout: float = 600.0,
    auto_confirm: Optional[bool] = None,
) -> Dict[str, Any]:
    """觸發流程並（預設）等到結束，回傳狀態、輸出變數與失敗動作。"""
    confirm = AUTO_CONFIRM if auto_confirm is None else auto_confirm
    ui_task(ensure_console)()
    if flow_name and not flow_id:
        status = _flow_row_status(flow_name)
        if status is None:
            known = [f["name"] for f in list_flows()["flows"]]
            return {"status": "not_found", "message": f"console 裡沒有名為「{flow_name}」的流程", "flows": known}
    dismiss_dialogs()  # 清掉上一輪留下的錯誤／輸出對話框，避免誤判
    run_id = str(uuid.uuid4())
    url = build_run_url(flow_name, flow_id, inputs, run_id)
    if len(url) > URL_MAX:
        return {"status": "error", "message": f"run URL 長度 {len(url)} 超過上限，請把大量資料改用檔案路徑傳入"}
    since = _utcnow() - timedelta(seconds=2)
    _launch(url)

    events: List[Dict[str, Any]] = []
    seen_outputs: Dict[str, Any] = {}
    deadline = time.monotonic() + timeout
    run_dir = None
    started_reported = False
    while time.monotonic() < deadline:
        time.sleep(1.0)
        for ev in _scan_after_trigger(flow_name, confirm):
            if ev["kind"] == "outputs":
                seen_outputs.update(ev.get("outputs") or {})
            key = (ev["kind"], ev.get("action"), ev.get("text"))
            if key not in {(e["kind"], e.get("action"), e.get("text")) for e in events}:
                events.append(ev)
            if ev["kind"] == "error" and run_dir is None:
                return {"status": "error", "message": ev.get("text"), "events": events, "url": url}
        run_dir = run_dir or runs.find_run_dir(run_id) or runs.newest_run_since(since, flow_name, flow_id)
        if run_dir is None:
            continue
        summary = runs.summarize_run(run_dir)
        if summary["finished"]:
            if seen_outputs and not summary["outputs"]:
                summary["outputs"] = seen_outputs
            summary["events"] = [e for e in events if e["kind"] != "outputs"]
            return summary
        if not wait and not started_reported:
            summary["events"] = events
            summary["message"] = "流程已開始執行；用 pad_get_run 查結果"
            return summary
        started_reported = True

    waiting_confirm = any(e.get("action") == "waiting_for_user" for e in events)
    result: Dict[str, Any] = {"status": "timeout", "events": events, "url": url}
    if run_dir is not None:
        result.update(runs.summarize_run(run_dir))
        result["status"] = "still_running"
        result["message"] = "等待逾時，流程仍在執行；可用 pad_get_run 繼續追蹤或 pad_stop_flow 停止"
    elif waiting_confirm:
        result["status"] = "awaiting_confirmation"
        result["message"] = (
            "PAD 跳出「外部來源正在嘗試執行以下流程」確認框，等待使用者按「繼續」。"
            "若要免確認：在 PAD console 的設定關閉「從外部叫用流程時顯示確認對話方塊」，"
            "或設定環境變數 PAD_BRIDGE_AUTO_CONFIRM=1 讓 bridge 只對自己發起的流程自動確認。"
        )
    else:
        result["message"] = "逾時且沒有看到執行紀錄；用 pad_get_dialogs 看 console 是否卡在其他對話框"
    return result
