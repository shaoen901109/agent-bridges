# encoding: utf-8
"""UI Automation 共用輔助：找視窗、找元件、點擊、送鍵、剪貼簿。

PAD 的 console 與設計器都是 WPF，大部分元件都有 AutomationId，可以用
Invoke / Value pattern 直接操作，不必動滑鼠。少數例外（下拉式選單、
需要前景焦點的 Ctrl+V 貼上）才會用真實滑鼠與鍵盤，此時會搶前景視窗。
"""

from __future__ import annotations

import ctypes
import functools
import threading
import time
from typing import Callable, Iterable, List, Optional

# 先設成 per-monitor DPI aware，UIA 回傳的座標才會是實體像素，多螢幕縮放下點擊才準
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:  # pragma: no cover - 舊版 Windows
    pass

import uiautomation as auto  # noqa: E402

auto.SetGlobalSearchTimeout(2)

_LOCK = threading.RLock()


class PadUiError(RuntimeError):
    """UI 找不到預期元件或狀態不符。"""


def ui_task(fn: Callable) -> Callable:
    """包住所有會碰 UIA 的函式：同一時間只跑一個，並在當前執行緒初始化 COM。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            with auto.UIAutomationInitializerInThread(debug=False):
                return fn(*args, **kwargs)

    return wrapper


# --- 視窗 -------------------------------------------------------------------


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def pids_by_name(process_names: Iterable[str]) -> set:
    """依執行檔名稱（不分大小寫、可省略 .exe）列出 PID。"""
    wanted = {n.lower().removesuffix(".exe") for n in process_names}
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    found = set()
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower().removesuffix(".exe") in wanted:
                found.add(entry.th32ProcessID)
            ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    finally:
        k32.CloseHandle(ctypes.c_void_p(snap))
    return found


def top_windows(process_names: Iterable[str]) -> List[auto.Control]:
    """回傳屬於指定行程名稱（不含 .exe）的所有頂層視窗。"""
    pids = pids_by_name(process_names)
    if not pids:
        return []
    return [c for c in auto.GetRootControl().GetChildren() if c.ProcessId in pids]


def find(root: auto.Control, max_depth: int = 40, **props) -> Optional[auto.Control]:
    """深度優先找第一個符合所有屬性的後代（AutomationId / Name / ControlTypeName / ClassName）。"""
    for c in find_all(root, max_depth=max_depth, limit=1, **props):
        return c
    return None


def find_all(root: auto.Control, max_depth: int = 40, limit: int = 0, **props) -> List[auto.Control]:
    out: List[auto.Control] = []
    for c, _depth in auto.WalkControl(root, includeTop=False, maxDepth=max_depth):
        if all(getattr(c, k) == v for k, v in props.items()):
            out.append(c)
            if limit and len(out) >= limit:
                break
    return out


def by_id(root: auto.Control, automation_id: str, max_depth: int = 40) -> Optional[auto.Control]:
    return find(root, max_depth=max_depth, AutomationId=automation_id)


def wait_for(fn: Callable[[], object], timeout: float, interval: float = 0.5):
    """反覆呼叫 fn 直到回傳真值或逾時；逾時回傳 None。"""
    end = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= end:
            return None
        time.sleep(interval)


# --- 操作 -------------------------------------------------------------------


_PATTERN_IDS = {
    "invoke": auto.PatternId.InvokePattern,
    "value": auto.PatternId.ValuePattern,
    "toggle": auto.PatternId.TogglePattern,
    "expand": auto.PatternId.ExpandCollapsePattern,
    "text": auto.PatternId.TextPattern,
    "window": auto.PatternId.WindowPattern,
}


def pattern(ctrl: auto.Control, name: str):
    """通用取 pattern。uiautomation 的各控制項類別只帶部分 GetXxxPattern 方法，
    PAD 的 WPF 元件常常不照型別慣例，所以一律走 GetPattern；不支援時回 None。"""
    try:
        return ctrl.GetPattern(_PATTERN_IDS[name])
    except Exception:
        return None


def invoke(ctrl: auto.Control) -> None:
    p = pattern(ctrl, "invoke")
    if not p:
        raise PadUiError(f"元件 {ctrl.ControlTypeName} {ctrl.Name!r} 不支援 Invoke")
    p.Invoke()


def set_value(ctrl: auto.Control, value: str) -> None:
    p = pattern(ctrl, "value")
    if not p:
        raise PadUiError(f"元件 {ctrl.ControlTypeName} {ctrl.Name!r} 不能輸入文字")
    p.SetValue(value)


def get_value(ctrl: auto.Control) -> str:
    p = pattern(ctrl, "value")
    try:
        return p.Value if p else ""
    except Exception:
        return ""


def get_text(ctrl: auto.Control) -> str:
    """讀 Document / RichText 這類只開 TextPattern 的元件。"""
    p = pattern(ctrl, "text")
    try:
        return p.DocumentRange.GetText(-1).strip() if p else (ctrl.Name or "")
    except Exception:
        return ctrl.Name or ""


def bring_to_front(window: auto.Control) -> None:
    wp = pattern(window, "window")
    try:
        if wp and wp.WindowVisualState == auto.WindowVisualState.Minimized:
            wp.SetWindowVisualState(auto.WindowVisualState.Normal)
    except Exception:
        pass
    window.SetActive()
    window.SetTopmost(True)
    window.SetTopmost(False)
    time.sleep(0.4)


def real_click(ctrl: auto.Control) -> None:
    """真實滑鼠點擊（會移動游標）。只用在 Invoke 打不開的 WPF 下拉選單。"""
    rect = ctrl.BoundingRectangle
    auto.Click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2, waitTime=0.3)


def send_keys(keys: str) -> None:
    auto.SendKeys(keys, interval=0.05, waitTime=0.3)


def read_clipboard() -> str:
    try:
        return auto.GetClipboardText() or ""
    except Exception:
        return ""


def write_clipboard(text: str) -> None:
    auto.SetClipboardText(text)


def describe(root: auto.Control, max_items: int = 60) -> List[str]:
    """把元件樹攤平成可讀文字，回報未知對話框時使用。"""
    lines: List[str] = []
    for c, _ in auto.WalkControl(root, includeTop=False, maxDepth=20):
        name = (c.Name or "").strip()
        value = get_value(c) if c.ControlTypeName == "EditControl" else ""
        if name or value:
            lines.append(f"[{c.ControlTypeName}] {name}" + (f" = {value}" if value else ""))
        if len(lines) >= max_items:
            break
    return lines
