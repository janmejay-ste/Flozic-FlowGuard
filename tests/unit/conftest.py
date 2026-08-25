"""Isolation for the unit suite: no unit test may touch live session state.

Several unit tests exercise the REAL module-level accumulators —
MobileReportCollector.record() appends to utils.mobile_report_builder's
_SESSION_FINDINGS, and triage tests set utils.ai_mobile_triage._SESSION_TRIAGE.
In a full-suite run the unit tests execute after the mobile browser tests
(tests/unit/ sorts last), so any leak lands in the SAME globals the session
teardown reads for the dashboard and scoring.

Both failure directions have happened in production runs:
  * 2026-08-19: reset_session_findings() calls in test_mobile_scoring.py wiped
    391 real findings minutes before scoring — "Mobile: not measured" on a
    green run.
  * 2026-08-20: two report-builder tests in test_auth_routes.py recorded
    synthetic fixtures ('<table class="cmp-table"> renders 720px wide',
    major, pricing/iPhone SE) WITHOUT restoring, so the live dashboard showed
    a phantom major finding that no auditor ever emitted — contradicting the
    passing dedicated pricing-table test on the same page.

A save/restore in one test file cannot prevent the next test file from making
the same mistake, so it lives here, autouse, for the whole directory.
"""

from __future__ import annotations

import pytest

import utils.ai_mobile_triage as amt
import utils.mobile_report_builder as mrb


@pytest.fixture(autouse=True)
def _isolate_mobile_session_state():
    saved_findings = list(mrb._SESSION_FINDINGS)
    saved_devices = set(mrb._SESSION_DEVICES)
    saved_checks = {k: dict(v) for k, v in mrb._SESSION_CHECKS.items()}
    saved_triage = amt._SESSION_TRIAGE
    yield
    mrb._SESSION_FINDINGS[:] = saved_findings
    mrb._SESSION_DEVICES.clear()
    mrb._SESSION_DEVICES.update(saved_devices)
    mrb._SESSION_CHECKS.clear()
    mrb._SESSION_CHECKS.update(saved_checks)
    amt._SESSION_TRIAGE = saved_triage
