# encoding: utf-8
"""client.py 的單元測試：用假的 TCP server 模擬 SketchUp 端協定。

執行：
    <venv>\\Scripts\\python.exe -m pytest tests -v
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from sketchup_bridge.cli import _parse_ids, _parse_json_arg, _parse_point
from sketchup_bridge.client import (
    SketchUpClient,
    SketchUpCommandError,
    SketchUpConnectionError,
)


class FakeServer:
    """一個只會照著 handler 回應的極簡 newline-JSON server。

    handler(request_dict) -> bytes（已含換行）或 list[bytes]（分段送出）或 None（不回應）
    """

    def __init__(self, handler, chunk_delay: float = 0.0):
        self.handler = handler
        self.chunk_delay = chunk_delay
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.requests = []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                self.sock.settimeout(0.2)
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        buffer = b""
        conn.settimeout(5.0)
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    return
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    request = json.loads(line.decode("utf-8"))
                    self.requests.append(request)
                    reply = self.handler(request)
                    if reply is None:
                        continue
                    if isinstance(reply, bytes):
                        reply = [reply]
                    for chunk in reply:
                        if chunk == b"__CLOSE__":
                            conn.close()
                            return
                        conn.sendall(chunk)
                        if self.chunk_delay:
                            time.sleep(self.chunk_delay)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def ok_line(request, result):
    return json.dumps({"id": request["id"], "ok": True, "result": result}).encode() + b"\n"


# --- 基本往返 -------------------------------------------------------------


def test_ping_roundtrip():
    server = FakeServer(lambda r: ok_line(r, {"pong": True}))
    try:
        with SketchUpClient(port=server.port, timeout=5) as client:
            assert client.call("ping") == {"pong": True}
        assert server.requests[0]["method"] == "ping"
        assert server.requests[0]["id"] == 1
    finally:
        server.close()


def test_params_are_sent():
    server = FakeServer(lambda r: ok_line(r, r.get("params")))
    try:
        with SketchUpClient(port=server.port, timeout=5) as client:
            result = client.call("create_box", {"width": 1000, "units": "mm"})
        assert result == {"width": 1000, "units": "mm"}
    finally:
        server.close()


def test_multiple_requests_one_connection():
    server = FakeServer(lambda r: ok_line(r, {"n": r["id"]}))
    try:
        with SketchUpClient(port=server.port, timeout=5) as client:
            assert client.call("ping") == {"n": 1}
            assert client.call("ping") == {"n": 2}
            assert client.call("ping") == {"n": 3}
        assert len(server.requests) == 3
    finally:
        server.close()


def test_unicode_roundtrip():
    server = FakeServer(lambda r: ok_line(r, {"名稱": "牆面 A", "note": r["params"]["text"]}))
    try:
        with SketchUpClient(port=server.port, timeout=5) as client:
            result = client.call("echo", {"text": "中文測試 ✓"})
        assert result["名稱"] == "牆面 A"
        assert result["note"] == "中文測試 ✓"
    finally:
        server.close()


# --- 分段訊息 -------------------------------------------------------------


def test_response_split_into_chunks():
    """回應被切成很多段送達時仍要能組回來。"""

    def handler(request):
        payload = ok_line(request, {"data": "x" * 5000})
        size = 97
        return [payload[i : i + size] for i in range(0, len(payload), size)]

    server = FakeServer(handler, chunk_delay=0.001)
    try:
        with SketchUpClient(port=server.port, timeout=10) as client:
            result = client.call("big")
        assert result["data"] == "x" * 5000
    finally:
        server.close()


def test_large_message():
    """模擬 base64 截圖等級的大回應（約 3 MB）。"""
    blob = "A" * (3 * 1024 * 1024)
    server = FakeServer(lambda r: ok_line(r, {"image_base64": blob}))
    try:
        with SketchUpClient(port=server.port, timeout=30) as client:
            result = client.call("screenshot")
        assert len(result["image_base64"]) == len(blob)
    finally:
        server.close()


def test_two_responses_arriving_together():
    """兩行回應黏在同一個 TCP 封包時，第二行必須留在緩衝區給下一次 call。"""

    def handler(request):
        if request["id"] == 1:
            # 一次把兩行都送出（第二行是給 id=2 的預先回應）
            return (
                json.dumps({"id": 1, "ok": True, "result": "first"}).encode()
                + b"\n"
                + json.dumps({"id": 2, "ok": True, "result": "second"}).encode()
                + b"\n"
            )
        return None

    server = FakeServer(handler)
    try:
        with SketchUpClient(port=server.port, timeout=5) as client:
            assert client.call("a") == "first"
            assert client.call("b") == "second"
    finally:
        server.close()


def test_blank_lines_are_skipped():
    def handler(request):
        return b"\n\n" + ok_line(request, "ok")

    server = FakeServer(handler)
    try:
        with SketchUpClient(port=server.port, timeout=5) as client:
            assert client.call("ping") == "ok"
    finally:
        server.close()


# --- 錯誤路徑 -------------------------------------------------------------


def test_command_error_raises():
    def handler(request):
        return json.dumps(
            {
                "id": request["id"],
                "ok": False,
                "error": {
                    "class": "ArgumentError",
                    "message": "必須提供 width",
                    "backtrace": ["commands.rb:120", "server.rb:200"],
                },
            }
        ).encode() + b"\n"

    server = FakeServer(handler)
    try:
        with SketchUpClient(port=server.port, timeout=5) as client:
            with pytest.raises(SketchUpCommandError) as info:
                client.call("create_box")
        error = info.value
        assert error.error_class == "ArgumentError"
        assert "必須提供 width" in error.message
        assert error.method == "create_box"
        assert "commands.rb:120" in str(error)
    finally:
        server.close()


def test_connection_refused_message_is_actionable():
    # 綁一個 port 然後立刻關掉，確保沒人在聽
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    client = SketchUpClient(port=port, timeout=2)
    with pytest.raises(SketchUpConnectionError) as info:
        client.call("ping")
    text = str(info.value)
    assert "Agent Bridge" in text
    assert "啟動伺服器" in text


def test_server_closes_connection():
    server = FakeServer(lambda r: b"__CLOSE__")
    try:
        client = SketchUpClient(port=server.port, timeout=5)
        with pytest.raises(SketchUpConnectionError) as info:
            client.call("ping")
        assert "連線" in str(info.value)
    finally:
        server.close()


def test_timeout_when_no_response():
    server = FakeServer(lambda r: None)
    try:
        client = SketchUpClient(port=server.port, timeout=0.5)
        with pytest.raises(SketchUpConnectionError) as info:
            client.call("slow")
        assert "超過" in str(info.value)
    finally:
        server.close()


def test_malformed_json_response():
    server = FakeServer(lambda r: b"this is not json\n")
    try:
        client = SketchUpClient(port=server.port, timeout=5)
        with pytest.raises(SketchUpConnectionError) as info:
            client.call("ping")
        assert "JSON" in str(info.value)
    finally:
        server.close()


def test_non_object_json_response():
    server = FakeServer(lambda r: b"[1, 2, 3]\n")
    try:
        client = SketchUpClient(port=server.port, timeout=5)
        with pytest.raises(SketchUpConnectionError):
            client.call("ping")
    finally:
        server.close()


def test_call_raw_does_not_raise():
    def handler(request):
        return json.dumps(
            {"id": request["id"], "ok": False, "error": {"message": "boom"}}
        ).encode() + b"\n"

    server = FakeServer(handler)
    try:
        with SketchUpClient(port=server.port, timeout=5) as client:
            response = client.call_raw("explode")
        assert response["ok"] is False
        assert response["error"]["message"] == "boom"
    finally:
        server.close()


def test_reconnect_after_close():
    server = FakeServer(lambda r: ok_line(r, "ok"))
    try:
        client = SketchUpClient(port=server.port, timeout=5)
        assert client.call("ping") == "ok"
        client.close()
        assert client.connected is False
        assert client.call("ping") == "ok"  # 會自動重新連線
        client.close()
    finally:
        server.close()


# --- CLI 參數解析 ---------------------------------------------------------


def test_parse_point_variants():
    assert _parse_point("1,2,3") == [1.0, 2.0, 3.0]
    assert _parse_point("[1, 2, 3]") == [1.0, 2.0, 3.0]
    assert _parse_point(None) is None


def test_parse_point_rejects_bad_input():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_point("1,2")


def test_parse_ids():
    assert _parse_ids(["1", "2,3", "4"]) == [1, 2, 3, 4]
    assert _parse_ids(None) is None


def test_parse_json_arg():
    assert _parse_json_arg('{"a": 1}') == {"a": 1}
    assert _parse_json_arg(None) is None
