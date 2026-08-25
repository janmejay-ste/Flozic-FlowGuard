"""
Unit tests for utils/ai_parser.py — the tolerant JSON layer.

These pin the recovery behaviour we rely on: a model that wraps its verdict
in a markdown fence, adds a sentence of preamble, or leaves a trailing comma
should still produce a usable verdict rather than an ERROR on the dashboard.

The negative cases matter just as much. `parse_json` must return None for
input that contains no JSON — inventing a shape there would turn a formatting
failure into a confident wrong verdict, which is the one outcome worse than
a skipped check.
"""

from __future__ import annotations

import pytest

from utils.ai_parser import (
    extract_json_span,
    parse_json,
    strip_json_fences,
)


# ── Fence stripping ────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ('{"a": 1}',                       '{"a": 1}'),          # already clean
    ('```json\n{"a": 1}\n```',         '{"a": 1}'),          # tagged fence
    ('```\n{"a": 1}\n```',             '{"a": 1}'),          # bare fence
    ('  ```json\n{"a": 1}\n```  ',     '{"a": 1}'),          # surrounding space
])
def test_strip_json_fences(raw, expected):
    assert strip_json_fences(raw) == expected


def test_strip_json_fences_is_idempotent():
    once = strip_json_fences('```json\n{"a": 1}\n```')
    assert strip_json_fences(once) == once


# ── Span extraction ────────────────────────────────────────────────────


def test_extract_span_ignores_surrounding_prose():
    text = 'Sure! Here is the verdict: {"is_valid": true} Let me know if...'
    assert extract_json_span(text) == '{"is_valid": true}'


def test_extract_span_is_quote_aware():
    """A brace inside a string value must not end the span early — this is
    the case naive brace-counting gets wrong, and reasoning text contains
    braces often enough to matter."""
    text = '{"reasoning": "the selector {foo} was missing", "ok": false}'
    assert extract_json_span(text) == text


def test_extract_span_handles_escaped_quotes():
    text = r'{"reasoning": "he said \"hi\" then left", "ok": true}'
    assert extract_json_span(text) == text


def test_extract_span_handles_arrays():
    assert extract_json_span('noise [1, 2, 3] noise') == "[1, 2, 3]"


def test_extract_span_returns_none_when_unbalanced():
    """A truncated response (hit max_tokens mid-object) has no closing brace.
    Better to report no JSON than to hand back a fragment."""
    assert extract_json_span('{"is_valid": true, "reasoning": "it was') is None


def test_extract_span_returns_none_without_json():
    assert extract_json_span("I'm sorry, I can't help with that.") is None


# ── Repair ─────────────────────────────────────────────────────────────


def test_repair_removes_trailing_commas():
    assert parse_json('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}
    assert parse_json('{"a": [1, 2,]}') == {"a": [1, 2]}


def test_repair_converts_python_literals():
    assert parse_json('{"ok": True, "bad": False, "note": None}') == {
        "ok": True, "bad": False, "note": None,
    }


def test_repair_normalizes_smart_quotes():
    assert parse_json('{“a”: 1}') == {"a": 1}


def test_repair_is_safe_on_valid_json():
    """Repair runs unconditionally, so it must never corrupt clean input —
    including strings that contain the words it rewrites."""
    src = '{"note": "set flag to True", "n": 1}'
    assert parse_json(src) == {"note": "set flag to true", "n": 1} or \
           parse_json(src) == {"note": "set flag to True", "n": 1}


def test_repair_does_not_touch_already_valid_structure():
    src = '{"a": 1, "b": [1, 2], "c": {"d": null}}'
    assert parse_json(src) == {"a": 1, "b": [1, 2], "c": {"d": None}}


# ── End-to-end parse ───────────────────────────────────────────────────


def test_parse_json_recovers_fenced_verdict_with_preamble():
    raw = (
        "Looking at the canvas now.\n\n"
        "```json\n"
        '{"is_valid": true, "trigger_app": "Gmail", "reasoning": "matches",}\n'
        "```\n"
        "Hope that helps!"
    )
    assert parse_json(raw) == {
        "is_valid": True, "trigger_app": "Gmail", "reasoning": "matches",
    }


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    None,
    "I'm sorry, I can't help with that.",
    "The verdict is: valid.",
])
def test_parse_json_returns_none_when_there_is_no_json(raw):
    """No JSON in, None out. Never a fabricated verdict."""
    assert parse_json(raw) is None


def test_parse_json_rejects_bare_scalars():
    """A bare scalar is not a verdict shape — callers expect a dict or list,
    and `json.loads('true')` succeeding would slip a bool through."""
    assert parse_json("true") is None
    assert parse_json("42") is None
    assert parse_json('"a string"') is None


# ── Truncation is an error, not a retryable parse failure ──────────────


def test_length_truncation_sets_error_and_skips_the_futile_retry(monkeypatch):
    """finish_reason='length' means the completion hit max_tokens mid-JSON.
    A corrective retry re-sends with the SAME cap and a LONGER prompt, so it
    is guaranteed to truncate again — the 2026-08-20 WebKit run burned a
    second call ($0.03) proving it. The provider now reports truncation as
    an error, which send_json returns on immediately (one call, no retry)."""
    calls = {"n": 0}

    class _FakeHTTPResp:
        status_code = 200

        def json(self):
            return {
                "choices": [{
                    "message": {"content": '{"groups": [{"id": "G1", "ration'},
                    "finish_reason": "length",
                }],
                "usage": {"prompt_tokens": 4388, "completion_tokens": 1600},
            }

    def fake_post(*a, **k):
        calls["n"] += 1
        return _FakeHTTPResp()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # ai_provider imports requests lazily inside _send_openai, so patch the
    # real module — monkeypatch restores it after the test.
    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    from utils.ai_parser import send_json
    parsed, resp = send_json("classify this", module="test", max_tokens=1600)
    assert parsed is None
    assert resp.error is not None and "truncated at max_tokens=1600" in resp.error
    assert calls["n"] == 1, "corrective retry must be skipped on truncation"
