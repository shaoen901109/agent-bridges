# encoding: utf-8
"""與 Blender 內 blender_bridge.py 伺服器溝通的共用 client。

協定：一條 TCP 連線 = 一個請求。送一行 JSON（\\n 結尾），收一行 JSON，伺服器關閉連線。

    送出: {"id": 1, "cmd": "ping", "params": {}}\\n
    收到: {"id": 1, "ok": true, ...}\\n
       或: {"id": 1, "ok": false, "error": "..."}\\n

MCP server 與 CLI 都只透過這個模組操作，不自己拼協定。

環境變數：
    BLENDER_BRIDGE_HOST     預設 127.0.0.1
    BLENDER_BRIDGE_PORT     預設 9877（Blender 端 startup 腳本讀同一個變數）
    BLENDER_BRIDGE_TIMEOUT  預設 120（秒；exec/render 可另外指定）
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Dict, Optional

DEFAULT_HOST = os.environ.get("BLENDER_BRIDGE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("BLENDER_BRIDGE_PORT", "9877"))
DEFAULT_TIMEOUT = float(os.environ.get("BLENDER_BRIDGE_TIMEOUT", "120"))
CONNECT_TIMEOUT = 5.0

_READ_CHUNK = 65536


class BlenderBridgeError(Exception):
    """所有橋接器錯誤的基底類別。"""


class BlenderNotAvailable(BlenderBridgeError):
    """無法連線 / 連線中斷 / 逾時。訊息包含給使用者的排除建議。"""


class BlenderCommandError(BlenderBridgeError):
    """Blender 端回報指令執行失敗（ok=false）。"""

    def __init__(self, message: str, cmd: Optional[str] = None,
                 response: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.cmd = cmd
        self.response = response or {}

    @property
    def refused(self) -> bool:
        """安全旗標（confirm/overwrite）不足而被拒絕，而非真正的錯誤。"""
        return bool(self.response.get("refused"))

    def __str__(self) -> str:
        head = f"指令 {self.cmd} 執行失敗：" if self.cmd else ""
        text = head + self.message
        extra = []
        for key in ("stdout", "stderr"):
            value = self.response.get(key)
            if value:
                extra.append(f"{key}:\n{value.rstrip()}")
        if extra:
            text += "\n" + "\n".join(extra)
        return text


def not_available_hint(host: str, port: int, detail: str) -> str:
    return (
        f"無法連線到 Blender Agent Bridge（{host}:{port}）：{detail}\n"
        "Blender 必須已由使用者開啟，且 startup 腳本 blender_bridge.py 已安裝到\n"
        "  %APPDATA%\\Blender Foundation\\Blender\\3.4\\scripts\\startup\\\n"
        "（安裝或更新腳本後要重新啟動 Blender 才會生效；Blender 啟動時會自動開伺服器，\n"
        " 不需要也無法從外部啟動）。\n"
        f"若 Blender 用了別的埠，請設定環境變數 BLENDER_BRIDGE_PORT（目前 {port}）。\n"
        "請把上述步驟告訴使用者，由使用者操作；不要自行啟動或關閉 Blender。"
    )


class BlenderClient:
    """每次呼叫建立一條短連線的同步 client（伺服器每個請求都會關閉連線）。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._next_id = 0

    # 讓 `with BlenderClient() as c:` 也能用（與 SketchUp client 介面一致）
    def __enter__(self) -> "BlenderClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def close(self) -> None:
        return None

    # -- 主要 API ---------------------------------------------------------

    def send(self, request: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """送出原始請求 dict，回傳原始回應 dict（不檢查 ok）。"""
        effective = self.timeout if timeout is None else float(timeout)
        payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        except socket.timeout as exc:
            raise BlenderNotAvailable(
                not_available_hint(self.host, self.port, f"連線逾時（{CONNECT_TIMEOUT} 秒）")) from exc
        except ConnectionRefusedError as exc:
            raise BlenderNotAvailable(
                not_available_hint(self.host, self.port, "連線被拒絕（沒有程式在監聽這個連接埠）")) from exc
        except OSError as exc:
            raise BlenderNotAvailable(
                not_available_hint(self.host, self.port, f"{type(exc).__name__}: {exc}")) from exc

        try:
            sock.settimeout(effective)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            try:
                sock.sendall(payload)
            except OSError as exc:
                raise BlenderNotAvailable(
                    not_available_hint(self.host, self.port, f"送出請求時中斷：{exc}")) from exc
            raw = self._read_line(sock, effective, request.get("cmd") or request.get("method"))
        finally:
            try:
                sock.close()
            except OSError:
                pass

        if not raw.strip():
            raise BlenderNotAvailable(
                not_available_hint(self.host, self.port, "Blender 回傳空回應（連線被關閉）"))
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise BlenderNotAvailable(
                f"Blender 回傳的資料不是合法 JSON：{exc}\n前 200 字元：{raw[:200]!r}") from exc
        if not isinstance(parsed, dict):
            raise BlenderNotAvailable(f"Blender 回傳的 JSON 不是物件：{parsed!r}")
        return parsed

    def call(self, cmd: str, params: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """送出一個指令；ok=false 時丟出 BlenderCommandError，否則回傳整個回應 dict。"""
        self._next_id += 1
        request: Dict[str, Any] = {"id": self._next_id, "cmd": cmd}
        if params:
            request["params"] = {k: v for k, v in params.items() if v is not None}
        response = self.send(request, timeout)
        if response.get("ok"):
            return response
        raise BlenderCommandError(
            message=str(response.get("error", "（Blender 端未提供錯誤訊息）")),
            cmd=cmd, response=response)

    def call_raw(self, cmd: str, params: Optional[Dict[str, Any]] = None,
                 timeout: Optional[float] = None) -> Dict[str, Any]:
        """跟 call() 相同，但 ok=false 也回傳完整回應而不丟例外（連不上仍會丟）。"""
        try:
            return self.call(cmd, params, timeout)
        except BlenderCommandError as exc:
            return exc.response or {"ok": False, "error": exc.message}

    # -- 內部 -------------------------------------------------------------

    def _read_line(self, sock: socket.socket, timeout: float, cmd: Any) -> bytes:
        chunks = []
        while True:
            try:
                data = sock.recv(_READ_CHUNK)
            except socket.timeout as exc:
                raise BlenderNotAvailable(
                    f"等待 Blender 回應 `{cmd}` 超過 {timeout} 秒。\n"
                    "可能原因：Blender 正忙（算圖、modal operator）、跳出對話框等待使用者、或指令本身太慢。\n"
                    "請確認 Blender 視窗沒有卡在對話框，必要時加長 timeout。") from exc
            except OSError as exc:
                raise BlenderNotAvailable(
                    not_available_hint(self.host, self.port, f"讀取回應時連線中斷：{exc}")) from exc
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
        raw = b"".join(chunks)
        nl = raw.find(b"\n")
        return raw[:nl] if nl >= 0 else raw


def call(cmd: str, params: Optional[Dict[str, Any]] = None, host: str = DEFAULT_HOST,
         port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """一次性呼叫：適合 CLI 這類短命流程。"""
    return BlenderClient(host=host, port=port, timeout=timeout).call(cmd, params)
