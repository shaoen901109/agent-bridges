# encoding: utf-8
"""不需要 PAD 的純邏輯測試：run URL 跳脫、執行紀錄解析、CLI 參數轉型。"""

import json
import subprocess
import sys

import pytest

from pad_bridge import console, runs
from pad_bridge.cli import _parse_value


def _argv_roundtrip(url: str) -> str:
    """模擬 Windows 把 "%1" 交給 PAD 的命令列解析，取回 PAD 實際看到的字串。"""
    code = "import sys; print(sys.argv[1], end='')"
    out = subprocess.run(f'"{sys.executable}" -c "{code}" "{url}"', capture_output=True, text=True, encoding="utf-8")
    return out.stdout


@pytest.mark.parametrize(
    "value",
    ["hello", 'a "quoted" b', "C:\\dir\\", "x&y#z", "中文 測試", "back\\\"slash", "line1\nline2"],
)
def test_inputs_survive_command_line(value):
    url = console.build_run_url("flow", inputs={"Msg": value, "N": 3, "B": True}, run_id="r")
    seen = _argv_roundtrip(url)
    query = seen.split("?", 1)[1]
    params = dict(p.split("=", 1) for p in query.split("&"))
    assert params["workflowName"] == "flow"
    assert json.loads(params["inputArguments"]) == {"Msg": value, "N": 3, "B": True}


def test_rejects_unsupported_input_type():
    with pytest.raises(ValueError):
        console.build_run_url("flow", inputs={"L": [1, 2]})


def test_rejects_ampersand_in_flow_name():
    with pytest.raises(ValueError):
        console.build_run_url("a&b")


def test_parse_time_handles_seven_digit_fraction():
    t = runs.parse_time("2026-10-14T10:53:05.3715792Z")
    assert t.year == 2026 and t.microsecond == 371579


def test_summarize_run(tmp_path):
    run_dir = tmp_path / "flow-id" / "Runs" / "run-id"
    run_dir.mkdir(parents=True)
    (run_dir / "RunDefinition.json").write_text(
        json.dumps(
            {
                "startTime": "2026-10-14T10:00:00.0000000Z",
                "endTime": "2026-10-14T10:00:02.5000000Z",
                "workflow": {"workflowId": "flow-id", "name": "測試"},
                "status": "Failed",
                "outputs": {},
            }
        ),
        encoding="utf-8",
    )
    actions = [
        {"index": 1, "name": "設定變數", "systemModuleName": "Variables", "systemActionName": "Assign", "status": "Succeeded"},
        {
            "index": 2,
            "name": "轉數字",
            "systemModuleName": "Text",
            "systemActionName": "ToNumber",
            "status": "Failed",
            "error": {"code": "E", "message": "無法轉換\r\n \r\nStack trace..."},
        },
    ]
    (run_dir / "Actions.log").write_text("\n".join(json.dumps(a, ensure_ascii=False) for a in actions), encoding="utf-8")
    s = runs.summarize_run(run_dir)
    assert s["status"] == "Failed" and s["finished"] and s["duration_s"] == 2.5
    assert s["not_succeeded_actions"][0]["error"] == {"code": "E", "message": "無法轉換"}


@pytest.mark.parametrize("text,expected", [("true", True), ("3", 3), ("2.5", 2.5), ("abc", "abc")])
def test_parse_value(text, expected):
    assert _parse_value(text) == expected
