"""
Unit tests for utils.failure_signals — the normalization layer that
fingerprinting will depend on.

The whole point of normalization is that **two runs of the same product
issue produce byte-identical signals**. These tests pin that property so
a future change to the regex set doesn't silently break fingerprint
stability.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.failure_signals import (
    FailureSignals,
    FailureSignalBuilder,
    SCHEMA_VERSION,
    _normalize_message,
    _normalize_traceback,
    _normalize_url,
    _extract_network_signals,
)


# ── URL normalization ─────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    # UUID
    ("https://api.example.com/users/abc12345-def4-1234-5678-90abcdef1234/profile",
     "https://api.example.com/users/<uuid>/profile"),
    # MongoDB ObjectId
    ("https://loop.flozic.ai/customeditor/6a3b6acf98442748133cacf7/account/90zye4g65bt8c4y3lh8entny",
     "https://loop.flozic.ai/customeditor/<oid>/account/<rand>"),
    # Numeric path
    ("https://api.example.com/users/12345/orders/9876",
     "https://api.example.com/users/<id>/orders/<id>"),
    # Query string stripped
    ("https://api.example.com/build?retry=3&token=xyz",
     "https://api.example.com/build"),
    # Combination: UUID + query
    ("https://api.example.com/job/abc12345-def4-1234-5678-90abcdef1234?state=pending",
     "https://api.example.com/job/<uuid>"),
])
def test_normalize_url_strips_volatile_segments(raw, expected):
    assert _normalize_url(raw) == expected


def test_normalize_url_idempotent():
    """Already-normalized URLs must not change on re-normalization."""
    once = _normalize_url("https://api.example.com/users/12345?x=1")
    twice = _normalize_url(once)
    assert once == twice


def test_normalize_url_empty_input_safe():
    assert _normalize_url("") == ""
    assert _normalize_url(None) == ""  # type: ignore[arg-type]


# ── Message normalization ─────────────────────────────────────────────


def test_normalize_message_collapses_whitespace_and_punctuation():
    assert _normalize_message("AI Rejected Canvas. ") == "ai rejected canvas"
    assert _normalize_message("Multiple   spaces\t\there") == "multiple spaces here"


def test_normalize_message_strips_volatile_tokens():
    msg = ("Timeout 20000ms exceeded at 2026-06-24T05:13:25.123Z "
           "while waiting for locator on 0x7fff1234abcd")
    n = _normalize_message(msg)
    assert "<ms>" in n
    assert "<ts>" in n
    assert "<addr>" in n
    # The literal volatile tokens must be gone.
    assert "20000ms" not in n
    assert "2026-06-24" not in n
    assert "0x7fff1234abcd" not in n


def test_normalize_message_idempotent():
    once = _normalize_message("Timeout 20000ms at file.py:42")
    twice = _normalize_message(once)
    assert once == twice


# ── Traceback normalization ───────────────────────────────────────────


def test_normalize_traceback_strips_line_numbers_but_keeps_frames():
    tb = (
        "Traceback (most recent call last):\n"
        "  File '/path/to/test_foo.py', line 42, in test_bar\n"
        "    self.assertEqual(x, y)\n"
        "AssertionError: 1 != 2 at 0xdeadbeef"
    )
    n = _normalize_traceback(tb)
    assert "test_foo.py" in n         # path preserved
    assert "test_bar" in n            # symbol preserved
    assert "AssertionError" in n      # exception preserved
    assert "<addr>" in n              # memory address scrubbed
    assert "0xdeadbeef" not in n


# ── Network signal extraction ─────────────────────────────────────────


def test_extract_network_signals_groups_by_endpoint():
    summary = {
        "failed_count": 5,
        "failures": [
            {"method": "POST",
             "url": "https://api/build?attempt=1",
             "status": 500},
            {"method": "POST",
             "url": "https://api/build?attempt=2",   # different query
             "status": 500},
            {"method": "GET",
             "url": "https://api/job/abc12345-def4-1234-5678-90abcdef1234",
             "status": 404},
            {"method": "GET",
             "url": "https://api/job/def67890-aaaa-1111-2222-333344445555",  # different uuid
             "status": 404},
            {"method": "POST",
             "url": "https://api/never-connects",
             "failure_reason": "net::ERR_TIMED_OUT"},
        ],
    }
    sigs = _extract_network_signals(summary)
    # Two POST 500s on the same endpoint collapse to one signal with count=2.
    by_url = {e["url_normalized"]: e for e in sigs.failed_endpoints}
    assert by_url["https://api/build"]["count"] == 2
    assert by_url["https://api/build"]["status_token"] == "500"
    # Two GET 404s on different UUIDs collapse to one signal with count=2.
    assert by_url["https://api/job/<uuid>"]["count"] == 2
    # Timeout is counted.
    assert sigs.timeout_count == 1
    assert sigs.total_failures == 5


def test_extract_network_signals_handles_empty_summary():
    sigs = _extract_network_signals({})
    assert sigs.failed_endpoints == []
    assert sigs.timeout_count == 0
    assert sigs.total_failures == 0


# ── End-to-end builder ────────────────────────────────────────────────


def test_build_for_consolidates_all_evidence(tmp_path: Path):
    # Set up a synthetic failure folder with the three input artifacts.
    folder = tmp_path / "test_foo_20260626_120000"
    folder.mkdir()

    # 1. network-summary.json
    (folder / "network-summary.json").write_text(json.dumps({
        "failed_count": 1,
        "failures": [{
            "method": "POST", "url": "https://api/build?x=1",
            "status": 500, "response_body": "{\"error\":\"x\"}",
        }],
    }))

    # 2. triage.json
    (folder / "triage.json").write_text(json.dumps({
        "category": "PRODUCT_BUG",
        "diagnosis": "AI rejected canvas at 2026-06-24T05:13:25.123Z due to wrong action.",
        "severity": "major",
    }))

    # 3. ai_verdict.json — lives outside the failure folder; pass via verdict_paths.
    verdict_dir = tmp_path / "recordings" / "test_foo"
    verdict_dir.mkdir(parents=True)
    verdict_file = verdict_dir / "ai_verdict.json"
    verdict_file.write_text(json.dumps({
        "status": "INVALID",
        "reasoning": "Action card shows Create Image instead of Summarize.",
    }))

    signals = FailureSignalBuilder.build_for(
        folder,
        test_name="test_foo",
        exception_type="AssertionError",
        exception_msg="AI rejected canvas for app foo. See file.py:169",
        traceback="File '/path/test_foo.py', line 42, in test_foo\n  raise AssertionError",
        verdict_paths=[verdict_file],
    )

    # Schema is pinned.
    assert signals.schema_version == SCHEMA_VERSION
    assert signals.test_name == "test_foo"

    # Network: query stripped, exactly one normalized failed endpoint.
    assert len(signals.network.failed_endpoints) == 1
    assert signals.network.failed_endpoints[0]["url_normalized"] == "https://api/build"
    assert signals.network.failed_endpoints[0]["status_token"] == "500"

    # AI verdict consolidated, reasoning normalized.
    assert signals.ai.validator_status == "INVALID"
    assert "create image" in signals.ai.validator_reasoning_norm
    assert "summarize" in signals.ai.validator_reasoning_norm

    # AI triage consolidated, timestamp scrubbed.
    assert signals.ai.triage_category == "PRODUCT_BUG"
    assert "<ts>" in signals.ai.triage_diagnosis_norm
    assert "2026-06-24" not in signals.ai.triage_diagnosis_norm

    # Exception type kept verbatim; message normalized.
    assert signals.exception.type == "AssertionError"
    assert "ai rejected canvas for app foo" in signals.exception.message_norm

    # Traceback keeps frame structure but loses line numbers.
    assert "test_foo.py" in signals.exception.traceback_norm
    assert "line 42" not in signals.exception.traceback_norm.lower() or \
           ":42" not in signals.exception.traceback_norm


def test_export_to_writes_versioned_file(tmp_path: Path):
    folder = tmp_path / "failure"
    folder.mkdir()
    signals = FailureSignals(test_name="t", schema_version=SCHEMA_VERSION)
    out = FailureSignalBuilder.export_to(folder, signals)
    payload = json.loads(out.read_text())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["test_name"] == "t"
    assert "network" in payload and "ai" in payload and "exception" in payload


def test_build_for_handles_missing_artifacts(tmp_path: Path):
    """No network-summary, no triage, no verdicts — builder must still
    produce a valid (empty) signals record so the failure-export path
    never crashes on a freshly-failed test with no AI/network data."""
    folder = tmp_path / "bare_failure"
    folder.mkdir()
    signals = FailureSignalBuilder.build_for(
        folder,
        test_name="bare",
        exception_type="TimeoutError",
        exception_msg="Locator timeout",
    )
    assert signals.network.failed_endpoints == []
    assert signals.ai.triage_category == ""
    assert signals.exception.type == "TimeoutError"
    assert signals.exception.message_norm == "locator timeout"
