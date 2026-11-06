# encoding: utf-8
"""操作 PAD 流程設計工具：建立流程、讀出／替換 Robin 腳本、新增輸入／輸出變數。

PAD 的設計器支援把動作「以文字」複製貼上（Robin 語言），所以：
  讀腳本 = 全選 → 複製 → 讀剪貼簿
  寫腳本 = 全選 → 刪除 → 把 Robin 文字放進剪貼簿 → 貼上
這些步驟需要真實鍵盤與前景焦點，執行期間會搶走使用者的視窗；結束後會還原剪貼簿。

限制：複製／貼上只涵蓋目前開啟的子流程（預設 Main）；輸入／輸出變數不在
Robin 文字裡，要另外用 add_variable 建立。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from . import runs
from .console import _button_in, _find_row, ensure_console
from .uia import (
    PadUiError,
    auto,
    bring_to_front,
    by_id,
    describe,
    find,
    find_all,
    invoke,
    pattern,
    read_clipboard,
    real_click,
    send_keys,
    set_value,
    top_windows,
    ui_task,
    wait_for,
    write_clipboard,
)

DESIGNER_PROCESS = "PAD.Designer"
TITLE_PREFIX = "Power Automate | "

# 資料類型下拉選單的項目名稱（繁中／英文介面）
_TYPE_NAMES = {
    "text": ("文字", "Text"),
    "number": ("數字", "Number"),
    "boolean": ("布林值", "Boolean", "布林"),
}


# --- 視窗 -------------------------------------------------------------------


def designer_window(flow_name: str):
    title = TITLE_PREFIX + flow_name
    for w in top_windows([DESIGNER_PROCESS]):
        if w.Name in (title, title + " *", title + "*"):
            return w
    return None


def _open_designer(flow_name: str, timeout: float = 60.0):
    win = designer_window(flow_name)
    if win:
        return win
    console = ensure_console()
    row = _find_row(console, flow_name)
    if not row:
        raise PadUiError(f"找不到流程「{flow_name}」")
    edit = by_id(row, "EditFlowButton")
    if not edit or not edit.IsEnabled:
        raise PadUiError(f"流程「{flow_name}」目前無法編輯（可能正在執行）")
    invoke(edit)
    win = wait_for(lambda: designer_window(flow_name), timeout, 1.0)
    if not win:
        raise PadUiError("等待設計工具開啟逾時")
    if not wait_for(lambda: by_id(win, "ProgramItemsListBoxActions"), 30, 1.0):
        raise PadUiError("設計工具開啟了，但找不到動作編輯區")
    return win


def _canvas(win):
    canvas = by_id(win, "ProgramItemsListBoxActions")
    if not canvas:
        raise PadUiError("找不到設計工具的動作編輯區")
    return canvas


def _actions(win) -> List[Any]:
    return [c for c in _canvas(win).GetChildren() if c.ControlTypeName == "ListItemControl"]


def _focus_canvas(win) -> None:
    bring_to_front(win)
    items = _actions(win)
    if items:
        real_click(items[0])
    else:
        real_click(_canvas(win))
    time.sleep(0.3)


def _designer_dialogs(win) -> List[Any]:
    return [d for d in find_all(win, max_depth=8, ControlTypeName="WindowControl")]


def _subflows(win) -> List[str]:
    tabs = by_id(win, "SubflowTabControl")
    names = [t.Name for t in find_all(tabs, max_depth=2, ControlTypeName="TabItemControl")] if tabs else []
    dropdown = by_id(win, "SubFlowDropDownButton")
    return {"open_tabs": names, "summary": dropdown.Name if dropdown else None}


# --- 讀寫腳本 ---------------------------------------------------------------


def _copy_script(win) -> str:
    if not _actions(win):
        return ""
    _focus_canvas(win)
    write_clipboard("")
    send_keys("{Ctrl}a")
    send_keys("{Ctrl}c")
    return wait_for(read_clipboard, 5, 0.3) or ""


def _paste_script(win, script: str) -> int:
    _focus_canvas(win)
    if _actions(win):
        send_keys("{Ctrl}a")
        send_keys("{Delete}")
        time.sleep(0.5)
        if _actions(win):
            raise PadUiError("無法清空原本的動作")
    if not script.strip():
        return 0
    write_clipboard(script.replace("\r\n", "\n").replace("\n", "\r\n"))
    real_click(_canvas(win))
    send_keys("{Ctrl}v")
    count = wait_for(lambda: len(_actions(win)), 10, 0.5) or 0
    if count == 0:
        dialogs = [describe(d, 20) for d in _designer_dialogs(win)]
        raise PadUiError(f"貼上後沒有任何動作，腳本可能有語法錯誤。對話框：{dialogs}")
    return count


def _errors(win) -> List[Dict[str, Any]]:
    """讀設計工具底部的「錯誤清單」。每列的格子依序是：類型、訊息、子流程、行號。"""
    grid = by_id(win, "ErrorsGrid")
    out = []
    if not grid:
        return out
    for row in grid.GetChildren():
        if row.ControlTypeName != "DataItemControl":
            continue
        cells = [c.Name or "" for c in row.GetChildren() if c.ControlTypeName == "CustomControl"]
        if len(cells) < 4:
            continue
        line = cells[3].strip()
        out.append({
            "type": "error" if cells[0] in ("錯誤", "Error") else "warning",
            "message": cells[1],
            "subflow": cells[2],
            "line": int(line) if line.isdigit() else line,
        })
    return out


class ScriptError(PadUiError):
    """貼上的 Robin 腳本有設計階段錯誤（未知動作、參數錯、變數不存在…）。"""

    def __init__(self, errors: List[Dict[str, Any]], note: str):
        self.errors = errors
        lines = "\n".join(f"  第 {e['line']} 行 [{e['subflow']}] {e['message']}" for e in errors)
        super().__init__(f"腳本有 {len(errors)} 個錯誤（{note}）：\n{lines}")


def _check_errors(win, wait: float = 2.0) -> List[Dict[str, Any]]:
    time.sleep(wait)  # 錯誤清單在貼上後非同步更新
    return _errors(win)


def _variables(win) -> Dict[str, List[str]]:
    out = {}
    for key, expander_id in (("inputs", "InputArgumentsExpander"), ("outputs", "OutputArgumentsExpander"), ("flow", "FlowVariablesExpander")):
        exp = by_id(win, expander_id)
        if not exp:
            out[key] = []
            continue
        try:
            pattern(exp, "expand").Expand()
            time.sleep(0.2)
        except Exception:
            pass
        out[key] = [c.Name for c in find_all(exp, max_depth=6, AutomationId="VariablesListItem")]
    return out


# --- 變數 -------------------------------------------------------------------


def _set_default(win, dialog, value: Any) -> None:
    """預設值欄位隨資料類型變形：文字是 Edit、數字是 Spinner(內含 Edit)、布林是下拉選單。"""
    if isinstance(value, bool):
        # 布林是下拉框，項目名稱為 "[1, 是]" / "[0, 否]"，用數字前綴判斷以免受語系影響
        combo = by_id(dialog, "VariableDefaultValueComboBox")
        if not combo:
            raise PadUiError("找不到布林預設值的下拉框")
        pattern(combo, "expand").Expand()
        prefix = "[1," if value else "[0,"
        item = wait_for(lambda: next((c for c in combo.GetChildren() if (c.Name or "").startswith(prefix)), None), 5, 0.3)
        if not item:
            raise PadUiError("布林預設值下拉框裡找不到選項")
        item.GetPattern(auto.PatternId.SelectionItemPattern).Select()
        try:
            pattern(combo, "expand").Collapse()
        except Exception:
            pass
        return
    text = str(value)
    for holder in find_all(dialog, max_depth=6, AutomationId="VariableDefaultValueTextBox"):
        for ctrl in [holder] + find_all(holder, max_depth=3, ControlTypeName="EditControl"):
            vp = pattern(ctrl, "value")
            if vp and not vp.IsReadOnly:
                vp.SetValue(text)
                return
        if pattern(holder, "expand"):
            real_click(holder)

            def _item():
                for menu in find_all(win, max_depth=6, ControlTypeName="MenuControl"):
                    for item in menu.GetChildren():
                        if item.Name.lower() == text.lower():
                            return item
                return None

            item = wait_for(_item, 5, 0.3)
            if item:
                invoke(item)
                return
    raise PadUiError(f"無法設定預設值 {text!r}；相關欄位：{[c for c in describe(dialog, 80) if '預設' in c]}")


def _add_variable(win, kind: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    name = spec["name"]
    existing = _variables(win)["inputs" if kind == "input" else "outputs"]
    if name in existing:
        return {"name": name, "kind": kind, "created": False, "reason": "已存在"}

    menu_id = "VariablesPanelInputOutputVariablesInputMenuItem" if kind == "input" else "VariablesPanelInputOutputVariablesOutputMenuItem"
    add_btn = by_id(win, "VariablesPanelInputOutputVariablesAddArgumentButton")
    if not add_btn:
        raise PadUiError("找不到變數窗格的「新增變數」按鈕（變數窗格可能被關閉）")
    bring_to_front(win)
    # 這是 WPF 下拉選單，Invoke/Expand 不會開，只能真的點
    real_click(add_btn)
    item = wait_for(lambda: by_id(win, menu_id), 5, 0.3)
    if not item:
        raise PadUiError("新增變數選單沒有出現")
    invoke(item)
    dialog = wait_for(lambda: next((d for d in _designer_dialogs(win) if by_id(d, "VariableNameTextBox")), None), 10, 0.3)
    if not dialog:
        raise PadUiError("新增變數對話框沒有出現")

    set_value(by_id(dialog, "VariableNameTextBox"), name)
    set_value(by_id(dialog, "VariableFriendlyNameTextBox"), spec.get("external_name") or name)
    if spec.get("description"):
        set_value(by_id(dialog, "VariableDescriptionTextBox"), str(spec["description"]))

    data_type = (spec.get("type") or "text").lower()
    if data_type not in _TYPE_NAMES:
        raise PadUiError(f"不支援的資料類型 {data_type!r}；只支援 text / number / boolean")
    if data_type != "text":
        dropdown = by_id(dialog, "VariableDefaultDataTypeDropDown")
        real_click(dropdown)
        # 選單是設計器視窗內的 MenuControl，項目為 MenuItemControl
        def _option():
            for menu in find_all(win, max_depth=6, ControlTypeName="MenuControl"):
                for item in menu.GetChildren():
                    if item.Name in _TYPE_NAMES[data_type]:
                        return item
            return None

        option = wait_for(_option, 5, 0.3)
        if not option:
            raise PadUiError(f"資料類型選單裡找不到 {data_type}")
        try:
            invoke(option)
        except Exception:
            real_click(option)
        time.sleep(0.5)
        chosen = find(by_id(dialog, "VariableDefaultDataTypeDropDown"), ControlTypeName="TextControl")
        if not chosen or chosen.Name not in _TYPE_NAMES[data_type]:
            raise PadUiError(f"資料類型沒有切換成 {data_type}（目前：{chosen.Name if chosen else '?'}）")

    if kind == "input" and spec.get("default") is not None:
        _set_default(win, dialog, spec["default"])
    if kind == "input" and spec.get("optional"):
        toggle = by_id(dialog, "VariableIsOptionalToggleButton")
        if toggle and pattern(toggle, "toggle").ToggleState == 0:
            pattern(toggle, "toggle").Toggle()
    if spec.get("sensitive"):
        toggle = by_id(dialog, "VariableSensitivityToggleButton")
        if toggle and pattern(toggle, "toggle").ToggleState == 0:
            pattern(toggle, "toggle").Toggle()

    invoke(by_id(dialog, "ArgumentVariableCreateButton"))
    time.sleep(1.0)
    if dialog.Exists(0.5):
        problem = describe(dialog, 30)
        cancel = by_id(dialog, "ArgumentVariableCancelButton")
        if cancel:
            invoke(cancel)
        raise PadUiError(f"建立變數 {name} 失敗，對話框內容：{problem}")
    return {"name": name, "kind": kind, "type": data_type, "created": True}


# --- 儲存與關閉 -------------------------------------------------------------


def _save(win) -> None:
    holder = by_id(win, "SaveFlowButton")
    btn = by_id(holder, "Button") if holder else None
    if not btn:
        raise PadUiError("找不到「儲存」按鈕")
    invoke(btn)
    time.sleep(3.0)


def _close(win, flow_name: str, save_if_asked: bool = True) -> None:
    try:
        pattern(win, "window").Close()
    except Exception:
        by_id(win, "PART_CloseButton") and invoke(by_id(win, "PART_CloseButton"))
    for _ in range(20):
        time.sleep(0.5)
        if not designer_window(flow_name):
            return
        for d in _designer_dialogs(win):
            btn = _button_in(d, ("儲存", "Save") if save_if_asked else ("不要儲存", "Don't save"))
            if btn:
                invoke(btn)
    if designer_window(flow_name):
        raise PadUiError("設計工具沒有關閉，可能卡在對話框")


def _newest_flow_id_since(t0: float) -> Optional[str]:
    best = None
    if runs.DESIGNER_DATA_DIR.is_dir():
        for p in runs.DESIGNER_DATA_DIR.glob("*.settings"):
            m = p.stat().st_mtime
            if m >= t0 and (best is None or m > best[0]):
                best = (m, p.stem)
    return best[1] if best else None


# --- 對外 API ---------------------------------------------------------------


@ui_task
def get_flow_script(flow_name: str, close_after: bool = True) -> Dict[str, Any]:
    """讀出流程的 Robin 腳本與變數清單。會暫時開啟設計工具。"""
    was_open = designer_window(flow_name) is not None
    win = _open_designer(flow_name)
    saved_clip = read_clipboard()
    try:
        script = _copy_script(win)
        info = {
            "flow_name": flow_name,
            "script": script,
            "action_count": len(_actions(win)),
            "variables": _variables(win),
            "subflows": _subflows(win),
        }
    finally:
        write_clipboard(saved_clip)
        if close_after and not was_open:
            _close(win, flow_name, save_if_asked=False)
    return info


def _apply(win, flow_name: str, script: Optional[str], inputs, outputs, creating: bool) -> Dict[str, Any]:
    """建變數 → 貼腳本 → 檢查錯誤清單 → 儲存。

    腳本有錯時：修改既有流程 → 不儲存直接關閉，流程維持原狀；
    建立新流程 → 清掉動作後儲存（保留流程與變數），讓 agent 修正後用 update_flow 補上。
    """
    saved_clip = read_clipboard()
    created = []
    try:
        # 先建變數，腳本裡用到的 %變數% 在貼上時才認得
        for spec in inputs or []:
            created.append(_add_variable(win, "input", spec))
        for spec in outputs or []:
            created.append(_add_variable(win, "output", spec))
        count = _paste_script(win, script) if script else len(_actions(win))
        problems = _check_errors(win) if script else []
        errors = [e for e in problems if e["type"] == "error"]
        if errors:
            if creating:
                _paste_script(win, "")
                _save(win)
                _close(win, flow_name, save_if_asked=True)
                raise ScriptError(errors, "流程已建立並保留變數，但動作沒有寫入；修正後用 update_flow 補上")
            _close(win, flow_name, save_if_asked=False)
            raise ScriptError(errors, "沒有儲存，流程維持原狀")
        _save(win)
        variables = _variables(win)
    finally:
        write_clipboard(saved_clip)
    return {
        "flow_name": flow_name,
        "action_count": count,
        "warnings": [e for e in problems if e["type"] != "error"],
        "variables_created": created,
        "variables": variables,
        "saved": True,
    }


@ui_task
def update_flow(
    flow_name: str,
    script: Optional[str] = None,
    inputs: Optional[List[Dict[str, Any]]] = None,
    outputs: Optional[List[Dict[str, Any]]] = None,
    close_after: bool = True,
) -> Dict[str, Any]:
    """替換流程的 Main 腳本（給 script 時）並補上缺少的輸入／輸出變數，然後儲存。"""
    win = _open_designer(flow_name)
    result = _apply(win, flow_name, script, inputs, outputs, creating=False)
    if close_after:
        _close(win, flow_name, save_if_asked=True)
    return result


@ui_task
def create_flow(
    flow_name: str,
    script: str = "",
    inputs: Optional[List[Dict[str, Any]]] = None,
    outputs: Optional[List[Dict[str, Any]]] = None,
    power_fx: bool = False,
) -> Dict[str, Any]:
    """建立新流程：開新流程對話框 → 命名 → 建變數 → 貼腳本 → 儲存 → 關閉。"""
    console = ensure_console()
    if _find_row(console, flow_name):
        raise PadUiError(f"已經有名為「{flow_name}」的流程；要修改請用 update_flow")
    t0 = time.time() - 1
    btn = by_id(console, "CreateNewFlowButton")
    if not btn:
        raise PadUiError("找不到 console 的「新流程」按鈕")
    invoke(btn)
    name_box = wait_for(lambda: by_id(console, "NewFlowNameTextBox"), 15, 0.5)
    if not name_box:
        raise PadUiError("建立流程對話框沒有出現")
    set_value(name_box, flow_name)
    toggle = by_id(console, "PowerFxEnabledToggleButton")
    if toggle:
        tp = pattern(toggle, "toggle")
        if bool(tp.ToggleState) != power_fx:
            tp.Toggle()
    invoke(by_id(console, "OKNewFlowButton"))
    win = wait_for(lambda: designer_window(flow_name), 90, 1.0)
    if not win:
        raise PadUiError("等待新流程的設計工具開啟逾時")
    wait_for(lambda: by_id(win, "ProgramItemsListBoxActions"), 30, 1.0)

    result = _apply(win, flow_name, script, inputs, outputs, creating=True)
    _close(win, flow_name, save_if_asked=True)
    result["flow_id"] = _newest_flow_id_since(t0)
    result["power_fx"] = power_fx
    return result


# --- 動作目錄與語法範本 -----------------------------------------------------

SCRATCH_FLOW = "_pad_bridge_scratch"


def _scratch_designer():
    """開啟專用的暫存流程當畫布；不存在就建立（空流程）。畫布內容永遠不儲存。"""
    win = designer_window(SCRATCH_FLOW)
    if win:
        return win
    console = ensure_console()
    if not _find_row(console, SCRATCH_FLOW):
        create_flow(SCRATCH_FLOW, "")
    return _open_designer(SCRATCH_FLOW)


def _search_tree(win, query: str) -> List[Any]:
    box = by_id(win, "SearchActionsSearchBox")
    if not box:
        raise PadUiError("找不到動作面板的搜尋框（動作面板可能被關閉）")
    set_value(box, query)
    time.sleep(1.2)
    tree = by_id(win, "ActionsTreeView")
    leaves = []
    for c, _ in auto.WalkControl(tree, maxDepth=12):
        # 葉節點的 AutomationId 是「模組.動作群組」；模組與子分類節點底下還有 TreeItem，排除
        if c.ControlTypeName == "TreeItemControl" and "." in (c.AutomationId or ""):
            if not any(ch.ControlTypeName == "TreeItemControl" for ch in c.GetChildren()):
                leaves.append(c)
    return leaves


@ui_task
def search_actions(query: str, limit: int = 30) -> Dict[str, Any]:
    """在設計工具的動作面板搜尋，回傳符合的動作（Robin ID 與顯示名稱）。"""
    win = _scratch_designer()
    try:
        leaves = _search_tree(win, query)
        items = [{"action_id": c.AutomationId, "name": c.Name} for c in leaves[:limit]]
    finally:
        set_value(by_id(win, "SearchActionsSearchBox"), "")
        _close(win, SCRATCH_FLOW, save_if_asked=False)
    return {"query": query, "count": len(leaves), "actions": items}


@ui_task
def action_template(query: str, action_id: Optional[str] = None) -> Dict[str, Any]:
    """把一個動作以預設參數加進暫存流程，複製出 PAD 自己產生的 Robin 文字。

    query 是動作面板的搜尋字（顯示名稱，如「執行 PowerShell」）；有多個結果時用 action_id 指定。
    """
    win = _scratch_designer()
    saved_clip = read_clipboard()
    try:
        _paste_script(win, "")
        leaves = _search_tree(win, query)
        if action_id:
            leaves = [c for c in leaves if c.AutomationId == action_id]
        if not leaves:
            raise PadUiError(f"動作面板搜尋「{query}」沒有符合的動作" + (f"（action_id={action_id}）" if action_id else ""))
        leaf = leaves[0]
        # 元件在設計工具關閉後就失效，先把要回傳的資訊取出來
        found = {"action_id": leaf.AutomationId, "name": leaf.Name}
        bring_to_front(win)
        leaf.DoubleClick(simulateMove=False)
        dialog = wait_for(lambda: next((d for d in _designer_dialogs(win) if by_id(d, "ActionDialogButtonSaveButton")), None), 10, 0.3)
        if dialog:
            invoke(by_id(dialog, "ActionDialogButtonSaveButton"))
            time.sleep(1.0)
            if dialog.Exists(0.3) and by_id(dialog, "ActionDialogButtonSaveButton"):
                problem = [ln for ln in describe(dialog, 120) if ln.strip()]
                invoke(by_id(dialog, "ActionDialogButtonCloseButton"))
                raise PadUiError(f"動作「{leaf.Name}」需要填必要參數才能產生範本；對話框內容：{problem[:40]}")
        if not _actions(win):
            raise PadUiError(f"動作「{leaf.Name}」沒有加進流程")
        robin = _copy_script(win)
        others = [{"action_id": c.AutomationId, "name": c.Name} for c in leaves[1:10]]
    finally:
        write_clipboard(saved_clip)
        box = by_id(win, "SearchActionsSearchBox")
        if box:
            set_value(box, "")
        _close(win, SCRATCH_FLOW, save_if_asked=False)
    return {**found, "robin": robin.strip(), "other_matches": others}
