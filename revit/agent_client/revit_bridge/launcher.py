# encoding: utf-8
"""啟動 Revit 並等橋接器就緒（供 revit_launch / `launch` 子指令使用）。

規則：只會「啟動」Revit，永遠不會強制結束它。若 Revit 被授權 / 登入 / 更新 / 載入外掛確認對話框卡住，
只回報視窗標題，請使用者自行處理。Revit 啟動很慢（常要一兩分鐘），預設等 180 秒。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from .client import DEFAULT_HOST, DEFAULT_PORT, RevitBridgeError, RevitClient

DEFAULT_REVIT_EXE = r"C:\Program Files\Autodesk\Revit 2024\Revit.exe"
_DETACHED = 0x00000008  # DETACHED_PROCESS
_NEW_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP


def find_revit_exe() -> Optional[str]:
    env = os.environ.get("REVIT_EXE")
    if env and os.path.isfile(env):
        return env
    if os.path.isfile(DEFAULT_REVIT_EXE):
        return DEFAULT_REVIT_EXE
    return None


def revit_windows() -> List[str]:
    """目前 Revit.exe 的可見視窗標題（判斷是否卡在對話框用）。"""
    try:
        from . import window_capture

        return [w["title"] for w in window_capture.find_windows(process_name="Revit.exe") if w["title"]]
    except Exception:  # pragma: no cover - 非 Windows 或缺 Pillow
        return []


def revit_process_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Revit.exe", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return "Revit.exe" in out
    except Exception:
        return False


def _ping(host: str, port: int) -> Optional[Dict[str, Any]]:
    try:
        with RevitClient(host=host, port=port, timeout=3.0) as c:
            return c.call("ping")
    except RevitBridgeError:
        return None


def launch(
    path: Optional[str] = None,
    wait_seconds: float = 180.0,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> Dict[str, Any]:
    """確保 Revit 在執行且橋接器可連線。回傳狀態 dict（ready=True/False）。"""
    if path and not os.path.isfile(path):
        return {"ready": False, "error": f"找不到檔案：{path}"}

    pong = _ping(host, port)
    if pong is not None:
        note = None
        if path:
            note = "Revit 已在執行，沒有重新開啟檔案（請用 revit_model_file open 並取得使用者同意）。"
        return {"ready": True, "already_running": True, "ping": pong, "note": note}

    started = False
    if not revit_process_running():
        exe = find_revit_exe()
        if exe is None:
            return {"ready": False, "error": "找不到 Revit.exe（預設 " + DEFAULT_REVIT_EXE + "；可設環境變數 REVIT_EXE）。"}
        args = [exe]
        if path:
            args.append(os.path.abspath(path))
        flags = (_DETACHED | _NEW_GROUP) if sys.platform == "win32" else 0
        subprocess.Popen(args, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = True

    deadline = time.time() + float(wait_seconds)
    while time.time() < deadline:
        pong = _ping(host, port)
        if pong is not None:
            return {"ready": True, "already_running": not started, "started": started,
                    "waited_seconds": round(wait_seconds - (deadline - time.time()), 1), "ping": pong}
        time.sleep(2.0)

    return {
        "ready": False,
        "started": started,
        "error": f"等了 {wait_seconds:.0f} 秒橋接器仍未就緒。",
        "revit_windows": revit_windows(),
        "hint": ("Revit 可能還在啟動、或卡在授權 / 登入 / 更新對話框，或跳出「是否載入此外掛」的安全性對話框"
                 "（請使用者選「永遠載入」），或外掛尚未安裝（執行 install.ps1）。"
                 "請使用者自行處理對話框；不要強制結束 Revit。"),
    }
