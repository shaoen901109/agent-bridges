# encoding: utf-8
"""client.py 的單元測試：用假的 TCP server 模擬 Rhino 端協定。

執行：
    <venv>\\Scripts\\python.exe -m pytest tests -v
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from rhino_bridge.cli import _parse_ids, _parse_json_arg, _parse_point
from rhino_bridge.client import (
    RhinoClient,
    RhinoCommandError,
    RhinoConnectionError,
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
        with RhinoClient(port=server.port, timeout=5) as client:
            assert client.call("ping") == {"pong": True}
        assert server.requests[0]["method"] == "ping"
        assert server.requests[0]["id"] == 1
    finally:
        server.close()


def test_params_are_sent():
    server = FakeServer(lambda r: ok_line(r, r.get("params")))
    try:
        with RhinoClient(port=server.port, timeout=5) as client:
            result = client.call("create_box", {"width": 10, "units": "mm"})
        assert result == {"width": 10, "units": "mm"}
    finally:
        server.close()


def test_multiple_requests_one_connection():
    server = FakeServer(lambda r: ok_line(r, {"n": r["id"]}))
    try:
        with RhinoClient(port=server.port, timeout=5) as client:
            assert client.call("ping") == {"n": 1}
            assert client.call("ping") == {"n": 2}
            assert client.call("ping") == {"n": 3}
        assert len(server.requests) == 3
    finally:
        server.close()


def test_unicode_roundtrip():
    server = FakeServer(lambda r: ok_line(r, {"名稱": "牆面 A", "note": r["params"]["text"]}))
    try:
        with RhinoClient(port=server.port, timeout=5) as client:
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
        with RhinoClient(port=server.port, timeout=10) as client:
            result = client.call("big")
        assert result["data"] == "x" * 5000
    finally:
        server.close()


def test_large_message():
    """模擬 base64 截圖等級的大回應（約 3 MB）。"""
    blob = "A" * (3 * 1024 * 1024)
    server = FakeServer(lambda r: ok_line(r, {"image_base64": blob}))
    try:
        with RhinoClient(port=server.port, timeout=30) as client:
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
        with RhinoClient(port=server.port, timeout=5) as client:
            assert client.call("a") == "first"
            assert client.call("b") == "second"
    finally:
        server.close()


def test_blank_lines_are_skipped():
    def handler(request):
        return b"\n\n" + ok_line(request, "ok")

    server = FakeServer(handler)
    try:
        with RhinoClient(port=server.port, timeout=5) as client:
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
                    "class": "RuntimeError",
                    "message": "必須提供 width",
                    "backtrace": ["rb_core.py:120", "main.py:200"],
                },
            }
        ).encode() + b"\n"

    server = FakeServer(handler)
    try:
        with RhinoClient(port=server.port, timeout=5) as client:
            with pytest.raises(RhinoCommandError) as info:
                client.call("create_box")
        error = info.value
        assert error.error_class == "RuntimeError"
        assert "必須提供 width" in error.message
        assert error.method == "create_box"
        assert "rb_core.py:120" in str(error)
    finally:
        server.close()


def test_connection_refused_message_is_actionable():
    # 綁一個 port 然後立刻關掉，確保沒人在聽
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    client = RhinoClient(port=port, timeout=2)
    with pytest.raises(RhinoConnectionError) as info:
        client.call("ping")
    text = str(info.value)
    assert text.startswith("RHINO_NOT_AVAILABLE")
    assert "RhinoBridgeStart" in text


def test_server_closes_connection():
    server = FakeServer(lambda r: b"__CLOSE__")
    try:
        client = RhinoClient(port=server.port, timeout=5)
        with pytest.raises(RhinoConnectionError) as info:
            client.call("ping")
        assert "連線" in str(info.value)
    finally:
        server.close()


def test_timeout_when_no_response():
    server = FakeServer(lambda r: None)
    try:
        client = RhinoClient(port=server.port, timeout=0.5)
        with pytest.raises(RhinoConnectionError) as info:
            client.call("slow")
        assert "超過" in str(info.value)
    finally:
        server.close()


def test_malformed_json_response():
    server = FakeServer(lambda r: b"this is not json\n")
    try:
        client = RhinoClient(port=server.port, timeout=5)
        with pytest.raises(RhinoConnectionError) as info:
            client.call("ping")
        assert "JSON" in str(info.value)
    finally:
        server.close()


def test_non_object_json_response():
    server = FakeServer(lambda r: b"[1, 2, 3]\n")
    try:
        client = RhinoClient(port=server.port, timeout=5)
        with pytest.raises(RhinoConnectionError):
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
        with RhinoClient(port=server.port, timeout=5) as client:
            response = client.call_raw("explode")
        assert response["ok"] is False
        assert response["error"]["message"] == "boom"
    finally:
        server.close()


def test_reconnect_after_close():
    server = FakeServer(lambda r: ok_line(r, "ok"))
    try:
        client = RhinoClient(port=server.port, timeout=5)
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
    assert _parse_ids(["a1", "b2,c3", "d4"]) == ["a1", "b2", "c3", "d4"]
    assert _parse_ids(None) is None


def test_parse_json_arg():
    assert _parse_json_arg('{"a": 1}') == {"a": 1}
    assert _parse_json_arg(None) is None


# --- Rhino 專屬：timeout 傳遞 / CLI 結束碼 / MCP 包裝 ---------------------------


def test_timeout_is_sent_to_server():
    """client 要把 timeout 以頂層欄位送給 Rhino 端（比自己的逾時少 1 秒）。"""
    server = FakeServer(lambda r: ok_line(r, "ok"))
    try:
        with RhinoClient(port=server.port, timeout=30) as client:
            client.call("ping")
            client.call("slow", timeout=100)
        assert server.requests[0]["timeout"] == 29
        assert server.requests[1]["timeout"] == 99
    finally:
        server.close()


def test_default_port_is_9878():
    from rhino_bridge.client import DEFAULT_PORT

    assert DEFAULT_PORT == 9878


def test_cli_exit_codes(capsys):
    from rhino_bridge.cli import main

    ok = FakeServer(lambda r: ok_line(r, {"pong": True}))
    try:
        assert main(["--port", str(ok.port), "ping"]) == 0
        assert '"pong": true' in capsys.readouterr().out
    finally:
        ok.close()

    def fail(request):
        return json.dumps({"id": request["id"], "ok": False,
                           "error": {"message": "boom", "class": "BridgeError"}}).encode() + b"\n"

    bad = FakeServer(fail)
    try:
        assert main(["--port", str(bad.port), "ping"]) == 1
    finally:
        bad.close()
    capsys.readouterr()

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    assert main(["--port", str(port), "--timeout", "1", "ping"]) == 2
    assert "RHINO_NOT_AVAILABLE" in capsys.readouterr().err


def test_cli_screenshot_failure_exit_3(capsys):
    from rhino_bridge.cli import main

    def fail(request):
        return json.dumps({"id": request["id"], "ok": False,
                           "error": {"message": "ViewCapture 失敗"}}).encode() + b"\n"

    server = FakeServer(fail)
    try:
        assert main(["--port", str(server.port), "screenshot"]) == 3
    finally:
        server.close()


def test_cli_builds_box_params():
    from rhino_bridge.cli import main

    server = FakeServer(lambda r: ok_line(r, {"created": []}))
    try:
        assert main(["--port", str(server.port), "--units", "mm", "box", "--width", "10",
                     "--depth", "20", "--height", "30", "--layer", "A::B"]) == 0
        req = server.requests[0]
        assert req["method"] == "create_box"
        assert req["params"]["width"] == 10.0 and req["params"]["units"] == "mm"
        assert req["params"]["layer"] == "A::B"
    finally:
        server.close()


def test_cli_gh_set_value_parsing():
    from rhino_bridge.cli import main

    server = FakeServer(lambda r: ok_line(r, {}))
    try:
        assert main(["--port", str(server.port), "gh-set", "abc", "[0,0,10]", "--input", "D"]) == 0
        assert server.requests[0]["params"] == {"id": "abc", "value": [0, 0, 10], "input": "D"}
        assert main(["--port", str(server.port), "gh-connect", "s", "t", "--target-input", "1"]) == 0
        assert server.requests[1]["params"]["target_input"] == 1
    finally:
        server.close()


def test_mcp_tool_names_and_prefix():
    import asyncio

    from rhino_bridge import mcp_server

    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names and all(n.startswith(("rhino_", "gh_")) for n in names)
    for required in ("rhino_get_info", "rhino_eval_python", "rhino_viewport_screenshot", "rhino_launch",
                     "rhino_model_file", "gh_status", "gh_connect", "gh_solve", "gh_bake",
                     "gh_canvas_screenshot", "gh_eval_python", "gh_save_document"):
        assert required in names


def test_mcp_not_available_prefix(monkeypatch):
    from rhino_bridge import mcp_server

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(mcp_server, "_client", RhinoClient(port=port, timeout=1))
    text = mcp_server.rhino_get_info()
    assert text.startswith("RHINO_NOT_AVAILABLE")
    assert mcp_server.gh_status().startswith("RHINO_NOT_AVAILABLE")


def test_mcp_command_failed_prefix(monkeypatch):
    from rhino_bridge import mcp_server

    def fail(request):
        return json.dumps({"id": request["id"], "ok": False,
                           "error": {"message": "找不到物件", "class": "BridgeError"}}).encode() + b"\n"

    server = FakeServer(fail)
    try:
        monkeypatch.setattr(mcp_server, "_client", RhinoClient(port=server.port, timeout=5))
        assert mcp_server.rhino_delete_objects(["x"]).startswith("RHINO_COMMAND_FAILED")
    finally:
        server.close()


def test_mcp_model_file_validation():
    from rhino_bridge import mcp_server

    assert mcp_server.rhino_model_file("explode").startswith("RHINO_COMMAND_FAILED")
    assert mcp_server.rhino_model_file("save_as").startswith("RHINO_COMMAND_FAILED")


def test_launch_when_already_running():
    from rhino_bridge import launcher

    server = FakeServer(lambda r: ok_line(r, {"pong": True, "rhino_version": "7"}))
    try:
        result = launcher.launch(port=server.port, wait_seconds=1)
        assert result["ready"] is True and result["already_running"] is True
    finally:
        server.close()


def test_launch_rejects_missing_file(tmp_path):
    from rhino_bridge import launcher

    result = launcher.launch(path=str(tmp_path / "nope.3dm"), wait_seconds=1)
    assert result["ready"] is False and "找不到檔案" in result["error"]
