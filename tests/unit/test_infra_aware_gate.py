"""
Infra-aware release gate (P0 hygiene, Task 5).

A network outage must never read as a product regression: harness/infra faults
are excluded from the product fail-rate that drives BLOCKED/AT_RISK/WARNING,
and a run whose failures are ALL infrastructure is INCONCLUSIVE (retry), never
BLOCKED and never READY. The smoke gate stays absolute (it correctly blocked
the 2026-09-07 CORS regression).
"""
from __future__ import annotations

from utils.risk_interpreter import ReleaseStatus, interpret


def test_all_infra_failures_are_inconclusive_not_blocked():
    # The 2026-08-26 2am outage shape: every browser test failed on dead DNS.
    d = interpret(total=312, passed=35, failed=277, harness_faults=277)
    assert d.status == ReleaseStatus.INCONCLUSIVE
    assert "Re-run" in d.description


def test_product_failures_still_block():
    d = interpret(total=100, passed=70, failed=30, harness_faults=0)
    assert d.status == ReleaseStatus.BLOCKED


def test_mixed_failures_rate_on_product_only():
    # 30 failures but 25 are infra: product rate = 5/100 = 5% → WARNING, not BLOCKED.
    d = interpret(total=100, passed=70, failed=30, harness_faults=25)
    assert d.status == ReleaseStatus.WARNING
    assert "excluded" in d.description        # infra exclusion stated, not silent


def test_smoke_gate_beats_everything():
    d = interpret(total=100, passed=98, failed=2, smoke_total=2, smoke_passed=1,
                  harness_faults=2)
    assert d.status == ReleaseStatus.BLOCKED  # failing smoke always blocks


def test_all_pass_ready_and_clamp():
    assert interpret(100, 100, 0).status == ReleaseStatus.READY
    # harness_faults can never exceed failed (defensive clamp)
    d = interpret(total=10, passed=9, failed=1, harness_faults=5)
    assert d.status == ReleaseStatus.INCONCLUSIVE
