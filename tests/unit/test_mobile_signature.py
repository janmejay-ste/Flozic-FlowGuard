"""
Unit tests for mobile failure-signature capture (tests/mobile/conftest.py).

Regression: mobile tests record via their OWN autouse fixture, which did not
pass error_signature — so mobile-layout failures persisted with an empty
signature and were silently excluded from systemic clustering. These lock the
fix: the mobile recorder now resolves the signature the same way the root
recorder does (stashed exception, with an 'E '-line longrepr fallback).
"""
from __future__ import annotations

import types

import tests.mobile.conftest as mc


def _node(**kw):
    base = {
        "_error_signature": "",
        "rep_call": None,
        "rep_setup": None,
        "name": "test_marketing_page_mobile_layout[iPhone_SE-pricing]",
        "harness_fault": False,
        "module": types.SimpleNamespace(__name__="tests.mobile.test_mobile_marketing_layout"),
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_resolve_uses_stashed_exception_signature():
    node = _node(
        _error_signature="AssertionError: BUTTON 155x25px on iPhone SE below 44px",
        rep_call=types.SimpleNamespace(failed=True, duration=0.1, longrepr="self = <x>"),
    )
    assert mc._resolve_error_signature(node, failed=True) == \
        "AssertionError: BUTTON 155x25px on iPhone SE below 44px"


def test_resolve_falls_back_to_longrepr_E_line_not_self_line():
    longrepr = (
        "self = <tests.mobile.test_mobile_marketing_layout object at 0x1>\n"
        "    def test_marketing_page_mobile_layout():\n"
        ">       assert not blockers\n"
        "E       AssertionError: 3 tap targets below 44px on Pixel 5\n"
        "tests/mobile/test_mobile_marketing_layout.py:32: AssertionError"
    )
    node = _node(_error_signature="",
                 rep_call=types.SimpleNamespace(failed=True, duration=0.0, longrepr=longrepr))
    sig = mc._resolve_error_signature(node, failed=True)
    assert sig == "AssertionError: 3 tap targets below 44px on Pixel 5"
    assert not sig.startswith("self =")   # never the class-repr line


def test_resolve_empty_when_not_failed():
    assert mc._resolve_error_signature(_node(), failed=False) == ""


def test_record_mobile_passes_signature_to_snapshot(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(mc, "add_test_record", lambda **kw: captured.update(kw))
    node = _node(
        _error_signature="AssertionError: font-size 14px below 16px on iPhone SE",
        rep_call=types.SimpleNamespace(failed=True, duration=0.2, longrepr="self = <x>"),
    )
    request = types.SimpleNamespace(
        node=node, config=types.SimpleNamespace(getoption=lambda o: "chromium"))
    mc._record_mobile(request)
    assert captured["status"] == "FAIL"
    assert captured["error_signature"] == "AssertionError: font-size 14px below 16px on iPhone SE"
    assert captured["cohort"] == "baseline-mobile"


def test_record_mobile_pass_has_no_signature(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(mc, "add_test_record", lambda **kw: captured.update(kw))
    node = _node(rep_call=types.SimpleNamespace(failed=False, duration=0.3, longrepr=None))
    request = types.SimpleNamespace(
        node=node, config=types.SimpleNamespace(getoption=lambda o: "webkit"))
    mc._record_mobile(request)
    assert captured["status"] == "PASS"
    assert captured["error_signature"] == ""
    assert captured["cohort"] == "baseline-mobile-webkit"
