# encoding: utf-8
"""與 Revit 內 Agent Bridge 伺服器溝通的共用 client。

協定：換行分隔的 JSON（newline-delimited JSON），類 JSON-RPC。

    送出: {"id": 1, "method": "ping", "params": {}, "timeout": 29}\\n
    收到: {"id": 1, "ok": true,  "result": {...}}\\n
       或: {"id": 1, "ok": false, "error": {"message": "...", "class": "...", "backtrace": [...]}}\\n

MCP server 與 CLI 都只透過這個模組操作，不自己拼協定。
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Any, Dict, Optional

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9879
DEFAULT_TIMEOUT = 30.0

# 讀取用的區塊大小
_READ_CHUNK = 65536


class RevitBridgeError(Exception):
    """所有橋接器錯誤的基底類別。"""


class RevitConnectionError(RevitBridgeError):
    """無法連線 / 連線中斷 / 逾時。訊息會包含給使用者的排除建議。"""


class RevitTimeoutError(RevitConnectionError):
    """請求已送出但等不到回應。指令可能仍在 Revit 內執行，呼叫端不可自動重送。"""


class RevitCommandError(RevitBridgeError):
    """Revit 端回報指令執行失敗。"""

    def __init__(
        self,
        message: str,
        error_class: Optional[str] = None,
        backtrace: Optional[list] = None,
        method: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_class = error_class
        self.backtrace = backtrace or []
        self.method = method

    def __str__(self) -> str:  # pragma: no cover - 字串組裝
        parts = []
        if self.method:
            parts.append(f"指令 {self.method} 執行失敗")
        if self.error_class:
            parts.append(f"[{self.error_class}]")
        parts.append(self.message)
        text = " ".join(parts)
        if self.backtrace:
            text += "\nRevit 端 C# 堆疊（前 8 行）：\n  " + "\n  ".join(self.backtrace[:8])
        return text


def _not_connected_hint(host: str, port: int, detail: str) -> str:
    return (
        f"REVIT_NOT_AVAILABLE：無法連線到 Revit Agent Bridge（{host}:{port}）：{detail}\n"
        "請依序確認：\n"
        "  1. Revit 2024 是否已開啟？（可呼叫 revit_launch 或 `python -m revit_bridge launch` 啟動；Revit 啟動要等一兩分鐘）\n"
        "  2. 外掛 RevitAgentBridge 是否已安裝？（執行 install.ps1；Revit 啟動時若跳出「是否載入此外掛」的安全性對話框，請使用者選「永遠載入」）\n"
        "  3. 伺服器是否被停止？請使用者在 Revit 功能區「Agent Bridge」分頁按「啟動伺服器」，按「狀態」查看。\n"
        f"  4. 連接埠是否為 {port}？（環境變數 REVIT_BRIDGE_PORT 可覆寫）\n"
        "若 Revit 卡在對話框（授權 / 登入 / 更新 / 載入外掛確認），請使用者自行處理；不要強制結束 Revit。"
    )


class RevitClient:
    """一條 TCP 連線上的同步 request/response client。

    用法::

        with RevitClient() as client:
            info = client.call("get_info")
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._buffer = bytearray()
        self._next_id = 0
        self._lock = threading.Lock()

    # -- 連線管理 ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except socket.timeout as exc:
            raise RevitConnectionError(
                _not_connected_hint(self.host, self.port, f"連線逾時（{self.timeout} 秒）")
            ) from exc
        except ConnectionRefusedError as exc:
            raise RevitConnectionError(
                _not_connected_hint(self.host, self.port, "連線被拒絕（沒有程式在監聽這個連接埠）")
            ) from exc
        except OSError as exc:
            raise RevitConnectionError(
                _not_connected_hint(self.host, self.port, f"{type(exc).__name__}: {exc}")
            ) from exc

        sock.settimeout(self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:  # pragma: no cover - 平台差異
            pass
        self._sock = sock
        self._buffer = bytearray()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._buffer = bytearray()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "RevitClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- 主要 API ---------------------------------------------------------

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """送出一個指令並回傳 result；失敗時丟出 RevitCommandError。"""
        with self._lock:
            self.connect()
            assert self._sock is not None
            self._next_id += 1
            request_id = self._next_id

            effective_timeout = self.timeout if timeout is None else float(timeout)
            payload = {"id": request_id, "method": method}
            if params:
                payload["params"] = params
            # 讓 Revit 端的 API 執行緒逾時比 client 早一點，才能回傳乾淨的錯誤而不是斷線
            payload["timeout"] = max(1.0, effective_timeout - 1.0)

            line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"

            self._sock.settimeout(effective_timeout)

            try:
                self._sock.sendall(line)
            except OSError as exc:
                self.close()
                raise RevitConnectionError(
                    _not_connected_hint(self.host, self.port, f"送出請求時中斷：{exc}")
                ) from exc

            response = self._read_response(effective_timeout, method)

        if response.get("ok"):
            return response.get("result")

        error = response.get("error") or {}
        raise RevitCommandError(
            message=str(error.get("message", "（Revit 端未提供錯誤訊息）")),
            error_class=error.get("class"),
            backtrace=error.get("backtrace"),
            method=method,
        )

    def call_raw(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """跟 call() 相同，但即使失敗也回傳完整 response dict（不丟例外）。"""
        try:
            result = self.call(method, params, timeout)
            return {"ok": True, "result": result}
        except RevitCommandError as exc:
            return {
                "ok": False,
                "error": {
                    "message": exc.message,
                    "class": exc.error_class,
                    "backtrace": exc.backtrace,
                },
            }

    # -- 內部 -------------------------------------------------------------

    def _read_response(self, timeout: float, method: str) -> Dict[str, Any]:
        """讀到一整行 JSON 為止。可處理分段到達的大訊息。"""
        while True:
            newline_index = self._buffer.find(b"\n")
            if newline_index >= 0:
                raw = bytes(self._buffer[:newline_index])
                del self._buffer[: newline_index + 1]
                if not raw.strip():
                    continue
                try:
                    parsed = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    raise RevitConnectionError(
                        f"Revit 回傳的資料不是合法 JSON：{exc}\n前 200 字元："
                        f"{raw[:200]!r}"
                    ) from exc
                if not isinstance(parsed, dict):
                    raise RevitConnectionError(
                        f"Revit 回傳的 JSON 不是物件：{parsed!r}"
                    )
                return parsed

            assert self._sock is not None
            try:
                chunk = self._sock.recv(_READ_CHUNK)
            except socket.timeout as exc:
                self.close()
                raise RevitTimeoutError(
                    f"等待 Revit 回應 `{method}` 超過 {timeout} 秒（原指令可能仍在 Revit 內執行，"
                    "不會自動重送；請先用 revit_get_info／list_elements 確認結果再決定要不要重做）。\n"
                    "可能原因：Revit 正忙於運算、停在對話框等待使用者（此時 ExternalEvent 不會執行）、或指令本身太慢。\n"
                    "Revit 可能停在對話框，請使用者處理（可用 revit_window_screenshot 查看）；必要時加長 timeout。"
                ) from exc
            except OSError as exc:
                self.close()
                raise RevitConnectionError(
                    _not_connected_hint(self.host, self.port, f"讀取回應時連線中斷：{exc}")
                ) from exc

            if not chunk:
                self.close()
                raise RevitConnectionError(
                    _not_connected_hint(
                        self.host,
                        self.port,
                        "連線被 Revit 端關閉（可能是伺服器被停止或 Revit 已關閉）",
                    )
                )
            self._buffer.extend(chunk)


def call(
    method: str,
    params: Optional[Dict[str, Any]] = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """一次性呼叫：建立連線 → 送指令 → 關閉。適合 CLI 這類短命流程。"""
    with RevitClient(host=host, port=port, timeout=timeout) as client:
        return client.call(method, params)
