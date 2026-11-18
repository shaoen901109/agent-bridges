# encoding: utf-8
"""client.py / cli.py / mcp_server.py 的單元測試：用假的 TCP server 模擬 Revit 端協定。

執行：
    <venv>\\Scripts\\python.exe -m pytest tests -v
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from revit_bridge.cli import _json_or_text, _parse_ids, _parse_json_arg, _parse_point
from revit_bridge.client import (
    RevitClient,
    RevitCommandError,
    RevitConnectionError,
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


def err_line(request, message, error_class="BridgeError", backtrace=None):
    error = {"message": message, "class": error_class}
    if backtrace:
        error["backtrace"] = backtrace
    return json.dumps({"id": request["id"], "ok": False, "error": error}).encode() + b"\n"


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


# --- 基本往返 -------------------------------------------------------------


def test_ping_roundtrip():
    server = FakeServer(lambda r: ok_line(r, {"pong": True}))
    try:
        with RevitClient(port=server.port, timeout=5) as client:
            assert client.call("ping") == {"pong": True}
        assert server.requests[0]["method"] == "ping"
        assert server.requests[0]["id"] == 1
    finally:
        server.close()


def test_params_are_sent():
    server = FakeServer(lambda r: ok_line(r, r.get("params")))
    try:
        with RevitClient(port=server.port, timeout=5) as client:
            result = client.call("create_wall", {"start": [0, 0], "height": 3000})
        assert result == {"start": [0, 0], "height": 3000}
    finally:
        server.close()


def test_multiple_requests_one_connection():
    server = FakeServer(lambda r: ok_line(r, {"n": r["id"]}))
    try:
        with RevitClient(port=server.port, timeout=5) as client:
            assert client.call("ping") == {"n": 1}
            assert client.call("ping") == {"n": 2}
            assert client.call("ping") == {"n": 3}
        assert len(server.requests) == 3
    finally:
        server.close()


def test_unicode_roundtrip():
    server = FakeServer(lambda r: ok_line(r, {"名稱": "牆面 A", "note": r["params"]["text"]}))
    try:
        with RevitClient(port=server.port, timeout=5) as client:
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
        with RevitClient(port=server.port, timeout=10) as client:
            result = client.call("big")
        assert result["data"] == "x" * 5000
    finally:
        server.close()


def test_large_message():
    """模擬大回應（約 3 MB）。"""
    blob = "A" * (3 * 1024 * 1024)
    server = FakeServer(lambda r: ok_line(r, {"blob": blob}))
    try:
        with RevitClient(port=server.port, timeout=30) as client:
            result = client.call("big")
        assert len(result["blob"]) == len(blob)
    finally:
        server.close()


def test_two_responses_arriving_together():
    """兩行回應黏在同一個 TCP 封包時，第二行必須留在緩衝區給下一次 call。"""

    def handler(request):
        if request["id"] == 1:
            return (
                json.dumps({"id": 1, "ok": True, "result": "first"}).encode()
                + b"\n"
                + json.dumps({"id": 2, "ok": True, "result": "second"}).encode()
                + b"\n"
            )
        return None

    server = FakeServer(handler)
    try:
        with RevitClient(port=server.port, timeout=5) as client:
            assert client.call("a") == "first"
            assert client.call("b") == "second"
    finally:
        server.close()


def test_blank_lines_are_skipped():
    def handler(request):
        return b"\n\n" + ok_line(request, "ok")

    server = FakeServer(handler)
    try:
        with RevitClient(port=server.port, timeout=5) as client:
            assert client.call("ping") == "ok"
    finally:
        server.close()


# --- 錯誤路徑 -------------------------------------------------------------


def test_command_error_raises():
    server = FakeServer(lambda r: err_line(r, "必須提供 start", "BridgeError",
                                           ["Handlers.CreateWall()", "ApiHandler.Execute()"]))
    try:
        with RevitClient(port=server.port, timeout=5) as client:
            with pytest.raises(RevitCommandError) as info:
                client.call("create_wall")
        error = info.value
        assert error.error_class == "BridgeError"
        assert "必須提供 start" in error.message
        assert error.method == "create_wall"
        assert "Handlers.CreateWall()" in str(error)
    finally:
        server.close()


def test_connection_refused_message_is_actionable():
    client = RevitClient(port=free_port(), timeout=2)
    with pytest.raises(RevitConnectionError) as info:
        client.call("ping")
    text = str(info.value)
    assert text.startswith("REVIT_NOT_AVAILABLE")
    assert "Agent Bridge" in text and "對話框" in text


def test_server_closes_connection():
    server = FakeServer(lambda r: b"__CLOSE__")
    try:
        client = RevitClient(port=server.port, timeout=5)
        with pytest.raises(RevitConnectionError) as info:
            client.call("ping")
        assert "連線" in str(info.value)
    finally:
        server.close()


def test_timeout_when_no_response_mentions_dialog():
    server = FakeServer(lambda r: None)
    try:
        client = RevitClient(port=server.port, timeout=0.5)
        with pytest.raises(RevitConnectionError) as info:
            client.call("slow")
        assert "超過" in str(info.value)
        assert "對話框" in str(info.value)
    finally:
        server.close()


def test_malformed_json_response():
    server = FakeServer(lambda r: b"this is not json\n")
    try:
        client = RevitClient(port=server.port, timeout=5)
        with pytest.raises(RevitConnectionError) as info:
            client.call("ping")
        assert "JSON" in str(info.value)
    finally:
        server.close()


def test_non_object_json_response():
    server = FakeServer(lambda r: b"[1, 2, 3]\n")
    try:
        client = RevitClient(port=server.port, timeout=5)
        with pytest.raises(RevitConnectionError):
            client.call("ping")
    finally:
        server.close()


def test_call_raw_does_not_raise():
    server = FakeServer(lambda r: err_line(r, "boom"))
    try:
        with RevitClient(port=server.port, timeout=5) as client:
            response = client.call_raw("explode")
        assert response["ok"] is False
        assert response["error"]["message"] == "boom"
    finally:
        server.close()


def test_reconnect_after_close():
    server = FakeServer(lambda r: ok_line(r, "ok"))
    try:
        client = RevitClient(port=server.port, timeout=5)
        assert client.call("ping") == "ok"
        client.close()
        assert client.connected is False
        assert client.call("ping") == "ok"  # 會自動重新連線
        client.close()
    finally:
        server.close()


# --- Revit 專屬：timeout 傳遞 / 預設埠 -------------------------------------------


def test_timeout_is_sent_to_server():
    """client 要把 timeout 以頂層欄位送給 Revit 端（比自己的逾時少 1 秒）。"""
    server = FakeServer(lambda r: ok_line(r, "ok"))
    try:
        with RevitClient(port=server.port, timeout=30) as client:
            client.call("ping")
            client.call("slow", timeout=100)
        assert server.requests[0]["timeout"] == 29
        assert server.requests[1]["timeout"] == 99
    finally:
        server.close()


def test_default_port_is_9879():
    from revit_bridge.client import DEFAULT_PORT

    assert DEFAULT_PORT == 9879


# --- CLI ------------------------------------------------------------------


def test_parse_point_variants():
    assert _parse_point("1,2,3") == [1.0, 2.0, 3.0]
    assert _parse_point("[1, 2]") == [1.0, 2.0]
    assert _parse_point(None) is None


def test_parse_point_rejects_bad_input():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_point("1")


def test_parse_ids_are_integers():
    assert _parse_ids(["101", "102,103", "104"]) == [101, 102, 103, 104]
    assert _parse_ids(None) is None


def test_parse_json_arg_and_value():
    assert _parse_json_arg('{"a": 1}') == {"a": 1}
    assert _parse_json_arg(None) is None
    assert _json_or_text("3000") == 3000
    assert _json_or_text("true") is True
    assert _json_or_text("3 m") == "3 m"


def test_cli_exit_codes(capsys):
    from revit_bridge.cli import main

    ok = FakeServer(lambda r: ok_line(r, {"pong": True}))
    try:
        assert main(["--port", str(ok.port), "ping"]) == 0
        assert '"pong": true' in capsys.readouterr().out
    finally:
        ok.close()

    bad = FakeServer(lambda r: err_line(r, "boom"))
    try:
        assert main(["--port", str(bad.port), "ping"]) == 1
    finally:
        bad.close()
    capsys.readouterr()

    assert main(["--port", str(free_port()), "--timeout", "1", "ping"]) == 2
    assert "REVIT_NOT_AVAILABLE" in capsys.readouterr().err


def test_cli_screenshot_failure_exit_3(capsys):
    from revit_bridge.cli import main

    server = FakeServer(lambda r: err_line(r, "ExportImage 失敗"))
    try:
        assert main(["--port", str(server.port), "screenshot"]) == 3
    finally:
        server.close()


def test_cli_builds_wall_params():
    from revit_bridge.cli import main

    server = FakeServer(lambda r: ok_line(r, {"id": 1}))
    try:
        assert main(["--port", str(server.port), "wall", "--start", "0,0", "--end", "5000,0",
                     "--height", "2800", "--level", "Level 1"]) == 0
        req = server.requests[0]
        assert req["method"] == "create_wall"
        assert req["params"]["start"] == [0.0, 0.0] and req["params"]["end"] == [5000.0, 0.0]
        assert req["params"]["height"] == 2800.0 and req["params"]["level"] == "Level 1"
    finally:
        server.close()


def test_cli_eval_reads_file_and_transaction_flag(tmp_path):
    from revit_bridge.cli import main

    code_file = tmp_path / "snippet.cs"
    code_file.write_text('return "中文 " + doc.Title;', encoding="utf-8")
    server = FakeServer(lambda r: ok_line(r, {"result": "x", "output": []}))
    try:
        assert main(["--port", str(server.port), "eval", "--file", str(code_file), "--no-transaction"]) == 0
        params = server.requests[0]["params"]
        assert server.requests[0]["method"] == "eval_csharp"
        assert params["code"] == 'return "中文 " + doc.Title;'
        assert params["transaction"] is False
    finally:
        server.close()


def test_cli_model_requires_path_and_maps_method():
    from revit_bridge.cli import main

    server = FakeServer(lambda r: ok_line(r, {"saved": True}))
    try:
        assert main(["--port", str(server.port), "model", "save", "--confirm"]) == 0
        assert server.requests[0]["method"] == "save_model"
        assert server.requests[0]["params"] == {"confirm": True}
        with pytest.raises(SystemExit):
            main(["--port", str(server.port), "model", "open"])
    finally:
        server.close()


# --- MCP server -----------------------------------------------------------

EXPECTED_TOOLS = (
    "revit_get_info", "revit_get_scene_info", "revit_list_elements", "revit_get_element",
    "revit_set_parameter", "revit_create_wall", "revit_create_floor", "revit_create_level",
    "revit_place_family_instance", "revit_delete_elements", "revit_select_elements", "revit_set_view",
    "revit_view_screenshot", "revit_window_screenshot", "revit_eval_csharp", "revit_model_file",
    "revit_undo", "revit_launch",
)


def test_mcp_tool_names_and_prefix():
    import asyncio

    from revit_bridge import mcp_server

    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names and all(n.startswith("revit_") for n in names)
    for required in EXPECTED_TOOLS:
        assert required in names
    assert names == set(EXPECTED_TOOLS)


def test_mcp_not_available_prefix(monkeypatch):
    from revit_bridge import mcp_server

    monkeypatch.setattr(mcp_server, "_client", RevitClient(port=free_port(), timeout=1))
    assert mcp_server.revit_get_info().startswith("REVIT_NOT_AVAILABLE")
    assert mcp_server.revit_get_scene_info().startswith("REVIT_NOT_AVAILABLE")


def test_mcp_timeout_is_not_resent(monkeypatch):
    """逾時代表指令可能仍在 Revit 內執行：不可自動重送（否則牆會建兩次）。"""
    from revit_bridge import mcp_server

    server = FakeServer(lambda r: None)  # 永不回應
    try:
        monkeypatch.setattr(mcp_server, "_client", RevitClient(port=server.port, timeout=1))
        text = mcp_server.revit_create_wall(start=[0, 0], end=[1000, 0])
        assert text.startswith("REVIT_NOT_AVAILABLE")
        assert "不會自動重送" in text
        time.sleep(0.3)
        assert len(server.requests) == 1
    finally:
        server.close()


def test_mcp_command_failed_prefix(monkeypatch):
    from revit_bridge import mcp_server

    server = FakeServer(lambda r: err_line(r, "找不到元件 id=1"))
    try:
        monkeypatch.setattr(mcp_server, "_client", RevitClient(port=server.port, timeout=5))
        text = mcp_server.revit_delete_elements([1])
        assert text.startswith("REVIT_COMMAND_FAILED")
        assert "找不到元件" in text
    finally:
        server.close()


def test_mcp_tools_serialize_params(monkeypatch):
    """每個工具要把參數正確轉成協定欄位（None 不送、布林旗標 False 不送）。"""
    from revit_bridge import mcp_server

    server = FakeServer(lambda r: ok_line(r, {"echo": r.get("params")}))
    try:
        monkeypatch.setattr(mcp_server, "_client", RevitClient(port=server.port, timeout=5))
        mcp_server.revit_create_wall([0, 0], [5000, 0], level="Level 1", height=2800)
        mcp_server.revit_create_floor([[0, 0], [4000, 0], [4000, 3000]], floor_type="Generic 150mm")
        mcp_server.revit_create_level(3000, name="Level 2")
        mcp_server.revit_place_family_instance("M_Single-Flush : 0915 x 2134mm", [1000, 0], host_id=555)
        mcp_server.revit_list_elements(category="Walls", types_only=True, limit=5)
        mcp_server.revit_set_parameter(123, 3500, name="Unconnected Height")
        mcp_server.revit_select_elements([1, 2], add=True)
        mcp_server.revit_set_view("3d", zoom_extents=True)
        mcp_server.revit_eval_csharp("return doc.Title;", transaction=False)
        methods = [r["method"] for r in server.requests]
        assert methods == ["create_wall", "create_floor", "create_level", "place_family_instance",
                           "list_elements", "set_parameter", "select_elements", "set_view", "eval_csharp"]
        by = {r["method"]: r["params"] for r in server.requests}
        assert by["create_wall"] == {"start": [0, 0], "end": [5000, 0], "level": "Level 1", "height": 2800}
        assert by["create_floor"]["floor_type"] == "Generic 150mm" and "level" not in by["create_floor"]
        assert by["create_level"] == {"elevation": 3000, "name": "Level 2", "create_plan_view": True}
        assert by["place_family_instance"]["host_id"] == 555 and "rotation_deg" not in by["place_family_instance"]
        assert by["list_elements"] == {"category": "Walls", "types_only": True, "limit": 5, "offset": 0}
        assert by["set_parameter"] == {"id": 123, "value": 3500, "name": "Unconnected Height"}
        assert by["select_elements"] == {"ids": [1, 2], "add": True}
        assert by["set_view"] == {"view": "3d", "zoom_extents": True}
        assert by["eval_csharp"] == {"code": "return doc.Title;", "transaction": False}
    finally:
        server.close()


def test_mcp_result_is_json_text(monkeypatch):
    from revit_bridge import mcp_server

    server = FakeServer(lambda r: ok_line(r, {"名稱": "牆", "elevation_mm": 3000.0}))
    try:
        monkeypatch.setattr(mcp_server, "_client", RevitClient(port=server.port, timeout=5))
        data = json.loads(mcp_server.revit_get_info())
        assert data == {"名稱": "牆", "elevation_mm": 3000.0}
    finally:
        server.close()


def test_mcp_view_screenshot_returns_image(monkeypatch, tmp_path):
    from PIL import Image as PILImage

    from revit_bridge import mcp_server

    png = tmp_path / "view.png"
    PILImage.new("RGB", (2400, 1200), (200, 30, 30)).save(png)
    server = FakeServer(lambda r: ok_line(r, {"path": str(png), "view_name": "{3D}"}))
    try:
        monkeypatch.setattr(mcp_server, "_client", RevitClient(port=server.port, timeout=5))
        result = mcp_server.revit_view_screenshot(pixel_size=2400)
        assert server.requests[0]["params"] == {"pixel_size": 2400}
        assert not isinstance(result, str)  # 是 image content，不是錯誤文字
        # 縮圖邏輯：最長邊不超過 MAX_EDGE
        from revit_bridge import images

        data, mime = images.encode_for_model(PILImage.open(png), mcp_server.MAX_EDGE)
        assert max(images.load_image(data).size) <= mcp_server.MAX_EDGE
    finally:
        server.close()


def test_mcp_view_screenshot_missing_file(monkeypatch, tmp_path):
    from revit_bridge import mcp_server

    server = FakeServer(lambda r: ok_line(r, {"path": str(tmp_path / "nope.png")}))
    try:
        monkeypatch.setattr(mcp_server, "_client", RevitClient(port=server.port, timeout=5))
        assert mcp_server.revit_view_screenshot().startswith("REVIT_COMMAND_FAILED")
    finally:
        server.close()


def test_mcp_model_file_validation():
    from revit_bridge import mcp_server

    assert mcp_server.revit_model_file("explode").startswith("REVIT_COMMAND_FAILED")
    assert mcp_server.revit_model_file("save_as").startswith("REVIT_COMMAND_FAILED")
    assert mcp_server.revit_model_file("new").startswith("REVIT_COMMAND_FAILED")
    assert mcp_server.revit_model_file("open").startswith("REVIT_COMMAND_FAILED")


def test_mcp_model_file_maps_methods_and_flags(monkeypatch):
    from revit_bridge import mcp_server

    server = FakeServer(lambda r: ok_line(r, {"ok": True}))
    try:
        monkeypatch.setattr(mcp_server, "_client", RevitClient(port=server.port, timeout=5))
        mcp_server.revit_model_file("save", path="ignored.rvt", confirm=True)
        mcp_server.revit_model_file("save_as", path=r"C:\temp\a.rvt", overwrite=True)
        mcp_server.revit_model_file("new", path=r"C:\temp\b.rvt", confirm=True)
        assert [r["method"] for r in server.requests] == ["save_model", "save_model", "new_model"]
        assert server.requests[0]["params"] == {"confirm": True}  # save 不送 path
        assert server.requests[1]["params"] == {"path": r"C:\temp\a.rvt", "overwrite": True}
        assert server.requests[2]["params"] == {"path": r"C:\temp\b.rvt", "confirm": True}
    finally:
        server.close()


def test_mcp_undo_loops_and_redo(monkeypatch):
    from revit_bridge import mcp_server

    server = FakeServer(lambda r: ok_line(r, {"posted": True}))
    try:
        monkeypatch.setattr(mcp_server, "_client", RevitClient(port=server.port, timeout=5))
        monkeypatch.setattr(mcp_server.time, "sleep", lambda s: None)
        mcp_server.revit_undo(times=2)
        mcp_server.revit_undo(direction="redo")
        assert [r["method"] for r in server.requests] == ["undo", "undo", "redo"]
    finally:
        server.close()


# --- launcher -------------------------------------------------------------


def test_launch_when_already_running():
    from revit_bridge import launcher

    server = FakeServer(lambda r: ok_line(r, {"pong": True, "revit_version": "Autodesk Revit 2024"}))
    try:
        result = launcher.launch(port=server.port, wait_seconds=1)
        assert result["ready"] is True and result["already_running"] is True
    finally:
        server.close()


def test_launch_rejects_missing_file(tmp_path):
    from revit_bridge import launcher

    result = launcher.launch(path=str(tmp_path / "nope.rvt"), wait_seconds=1)
    assert result["ready"] is False and "找不到檔案" in result["error"]


def test_launch_reports_missing_exe(monkeypatch):
    from revit_bridge import launcher

    monkeypatch.setattr(launcher, "revit_process_running", lambda: False)
    monkeypatch.setattr(launcher, "find_revit_exe", lambda: None)
    result = launcher.launch(port=free_port(), wait_seconds=1)
    assert result["ready"] is False and "REVIT_EXE" in result["error"]


def test_find_revit_exe_env_override(monkeypatch, tmp_path):
    from revit_bridge import launcher

    fake = tmp_path / "Revit.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("REVIT_EXE", str(fake))
    assert launcher.find_revit_exe() == str(fake)
