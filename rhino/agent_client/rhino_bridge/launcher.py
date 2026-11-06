# encoding: utf-8
"""啟動 Rhino 並等橋接器就緒（供 rhino_launch / `launch` 子指令使用）。

規則：只會「啟動」Rhino，永遠不會強制結束它。若 Rhino 被授權 / 登入 / 更新對話框卡住，
只回報視窗標題，請使用者自行處理。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from .client import DEFAULT_HOST, DEFAULT_PORT, RhinoBridgeError, RhinoClient

DEFAULT_RHINO_EXE = r"C:\Program Files\Rhino 7\System\Rhino.exe"
_DETACHED = 0x00000008  # DETACHED_PROCESS
_NEW_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP


def find_rhino_exe() -> Optional[str]:
    env = os.environ.get("RHINO_EXE")
    if env and os.path.isfile(env):
        return env
    for p in (DEFAULT_RHINO_EXE, r"C:\Program Files\Rhino 7\System\Rhino.exe"):
        if os.path.isfile(p):
            return p
    return None


def rhino_windows() -> List[str]:
    """目前 Rhino.exe 的可見視窗標題（判斷是否卡在對話框用）。"""
    try:
        from . import window_capture

        return [w["title"] for w in window_capture.find_windows(process_name="Rhino.exe") if w["title"]]
    except Exception:  # pragma: no cover - 非 Windows 或缺 Pillow
        return []


def rhino_process_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Rhino.exe", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return "Rhino.exe" in out
    except Exception:
        return False


def _ping(host: str, port: int) -> Optional[Dict[str, Any]]:
    try:
        with RhinoClient(host=host, port=port, timeout=3.0) as c:
            return c.call("ping")
    except RhinoBridgeError:
        return None


def launch(
    path: Optional[str] = None,
    wait_seconds: float = 90.0,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> Dict[str, Any]:
    """確保 Rhino 在執行且橋接器可連線。回傳狀態 dict（ready=True/False）。"""
    if path and not os.path.isfile(path):
        return {"ready": False, "error": f"找不到檔案：{path}"}

    pong = _ping(host, port)
    if pong is not None:
        note = None
        if path:
            note = "Rhino 已在執行，沒有重新開啟檔案（請用 rhino_model_file open 並取得使用者同意）。"
        return {"ready": True, "already_running": True, "ping": pong, "note": note}

    started = False
    exe = None
    if not rhino_process_running():
        exe = find_rhino_exe()
        if exe is None:
            return {"ready": False, "error": "找不到 Rhino.exe（預設 " + DEFAULT_RHINO_EXE + "；可設環境變數 RHINO_EXE）。"}
        args = [exe, "/nosplash"]
        if path:
            args.append(os.path.abspath(path))
        # 外掛本身會在啟動時自動開伺服器；/runscript 當保險（已啟動時只會回報「已在執行」）
        args.append("/runscript=-_RhinoBridgeStart")
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
        time.sleep(1.5)

    return {
        "ready": False,
        "started": started,
        "error": f"等了 {wait_seconds:.0f} 秒橋接器仍未就緒。",
        "rhino_windows": rhino_windows(),
        "hint": ("Rhino 可能卡在授權 / 登入 / 更新對話框，或外掛尚未安裝（執行 install.ps1）。"
                 "請使用者自行處理對話框；不要強制結束 Rhino。"),
    }
