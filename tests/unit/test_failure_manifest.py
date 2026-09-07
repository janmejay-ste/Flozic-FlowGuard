"""
Unit tests for utils/failure_manifest — Observability v2 Phase 1.

The extraction rule that matters: backend state comes from network traffic the
run ALREADY captured (deterministic, no new API calls), and a manifest failure
can never fail a test.
"""
from __future__ import annotations

import json

import utils.failure_manifest as fm


def test_parse_connect_ids():
    url = ("https://loop.flozic.ai/customeditor/6a8d885be9fa61fa1791587a/"
           "account/mnjvuoj24ekjh8xesj7ugo93")
    ids = fm.parse_connect_ids(url)
    assert ids["connect_id"] == "6a8d885be9fa61fa1791587a"
    assert ids["account_id"] == "mnjvuoj24ekjh8xesj7ugo93"
    assert fm.parse_connect_ids("https://www.flozic.ai/pricing") == {
        "connect_id": None, "account_id": None}


def test_extract_backend_state_filters_and_orders():
    events = [
        {"url": "https://x/gtm.js", "status": 200},                       # irrelevant
        {"url": "https://loop.flozic.ai/customerconnect/getByConnectId/abc",
         "method": "GET", "status": 200, "duration_ms": 40,
         "response_body": '{"status":"pending","steps":2}'},
        {"url": "https://loop.flozic.ai/common/copilotConversations",
         "method": "POST", "status": 500, "duration_ms": 900,
         "response_body": '{"error":"internal"}'},
    ]
    out = fm.extract_backend_state(events)
    assert len(out) == 2                                    # gtm.js excluded
    assert out[-1]["status"] == 500                         # newest last
    assert "internal" in out[-1]["body_excerpt"]
    assert out[0]["endpoint"].startswith("/customerconnect/getByConnectId")


def test_extract_backend_state_caps_and_handles_dict_bodies():
    events = [{"url": f"https://l/customerconnect/x{i}", "status": 200,
               "response_body": {"i": i}} for i in range(9)]
    out = fm.extract_backend_state(events, limit=5)
    assert len(out) == 5 and out[-1]["body_excerpt"] == '{"i": 8}'


def test_write_manifest_end_to_end(tmp_path):
    (tmp_path / "url.txt").write_text(
        "https://loop.flozic.ai/customeditor/abc123/account/def456")
    (tmp_path / "network-events.json").write_text(json.dumps([
        {"url": "https://loop.flozic.ai/customerconnect/getByConnectId/abc123",
         "method": "GET", "status": 200, "response_body": '{"status":"pending"}'},
    ]))
    console = [{"type": "error", "text": "boom"}, {"type": "log", "text": "hi"}]

    path = fm.write_failure_manifest(tmp_path, "TestX::test_y",
                                     "TimeoutError: Timeout 180000ms", console)
    man = json.loads(path.read_text())
    assert man["connect_id"] == "abc123"
    assert man["console"]["errors"] == 1 and man["console"]["total_events"] == 2
    assert man["backend_state"][-1]["status"] == 200
    assert "pending" in man["backend_state"][-1]["body_excerpt"]
    assert man["error_signature"].startswith("TimeoutError")
    # console.json written alongside
    assert json.loads((tmp_path / "console.json").read_text())[0]["text"] == "boom"


def test_write_manifest_missing_folder_is_noop(tmp_path):
    assert fm.write_failure_manifest(tmp_path / "nope", "t", "sig", []) is None


def test_write_manifest_survives_corrupt_inputs(tmp_path):
    (tmp_path / "network-events.json").write_text("{not json")
    path = fm.write_failure_manifest(tmp_path, "t", "", None)
    man = json.loads(path.read_text())
    assert man["backend_state"] == [] and man["page_url"] is None


def test_write_manifest_accepts_dataclass_console_events(tmp_path):
    # Regression: real events are utils.js_console_monitor.ConsoleEvent
    # dataclasses, not dicts. Serializing them directly threw
    # "Object of type ConsoleEvent is not JSON serializable", which silently
    # dropped BOTH console.json and failure.json on every live failure.
    from utils.js_console_monitor import ConsoleEvent
    events = [ConsoleEvent(type="error", text="CORS blocked", location="x:1:2",
                           source_origin="media.flozic.ai"),
              ConsoleEvent(type="log", text="ok")]
    path = fm.write_failure_manifest(tmp_path, "TestX::t", "sig", events)
    assert path is not None                                   # did NOT swallow an error
    man = json.loads(path.read_text())
    assert man["console"]["errors"] == 1 and man["console"]["total_events"] == 2
    dumped = json.loads((tmp_path / "console.json").read_text())
    assert dumped[0]["text"] == "CORS blocked" and dumped[0]["source_origin"] == "media.flozic.ai"
