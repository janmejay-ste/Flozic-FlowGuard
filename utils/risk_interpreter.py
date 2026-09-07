"""
Release-risk interpreter — pure Python equivalent of Java RiskInterpreter.
Consumes test records and produces a ReleaseStatus + human description.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class ReleaseStatus(str, Enum):
    READY    = "READY"
    WARNING  = "WARNING"
    AT_RISK  = "AT_RISK"
    BLOCKED  = "BLOCKED"
    # Run dominated by harness/infrastructure faults with no product failures:
    # the product was never properly exercised, so the honest verdict is
    # "retry", never BLOCKED (a network outage must not read as a product
    # regression) and never READY (nothing was proven).
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class ReleaseDecision:
    status: ReleaseStatus
    label: str        # short badge text
    description: str  # one-line human reason
    color: str        # hex for UI
    bg: str           # background hex


def interpret(
    total: int,
    passed: int,
    failed: int,
    smoke_total: int = 0,
    smoke_passed: int = 0,
    harness_faults: int = 0,
) -> ReleaseDecision:
    """
    Priority ladder (mirrors Java RiskInterpreter):
      1. Any failures in smoke tests   → BLOCKED
      2. all failures are harness/infra → INCONCLUSIVE (retry — not a product verdict)
      3. product fail rate > 20 %      → BLOCKED
      4. product fail rate > 5 %       → AT_RISK
      5. product fail rate > 0 %       → WARNING
      6. all pass                      → READY

    `harness_faults` = failures attributed to the harness/infrastructure
    (connectivity loss, missing env — see utils/harness_errors). They are
    EXCLUDED from the product fail-rate that drives BLOCKED/AT_RISK/WARNING:
    the product was never exercised by them, so a network outage must never
    read as a product regression (the 2026-08-26 2am outage would have
    false-BLOCKED a release). They still fail the suite and still show in the
    description — infra-aware, not infra-blind.
    """
    if total == 0:
        return ReleaseDecision(
            status=ReleaseStatus.WARNING,
            label="NO DATA",
            description="No tests recorded this run.",
            color="#92400e", bg="#fffbeb",
        )

    # Smoke gate — a failing smoke test blocks regardless of anything else.
    if smoke_total > 0 and smoke_passed < smoke_total:
        smoke_failures = smoke_total - smoke_passed
        return ReleaseDecision(
            status=ReleaseStatus.BLOCKED,
            label="BLOCKED",
            description=f"{smoke_failures} smoke test(s) failed — release blocked.",
            color="#dc2626", bg="#fef2f2",
        )

    harness_faults = max(0, min(harness_faults, failed))
    product_failed = failed - harness_faults

    # Everything that failed was infrastructure: the product was not exercised.
    if failed > 0 and product_failed == 0:
        return ReleaseDecision(
            status=ReleaseStatus.INCONCLUSIVE,
            label="INCONCLUSIVE",
            description=(f"All {failed} failure(s) were harness/infrastructure faults — "
                         "the product was not exercised by them. Re-run before deciding."),
            color="#3730a3", bg="#eef2ff",
        )

    infra_note = (f" ({harness_faults} additional infra fault(s) excluded from this rate)"
                  if harness_faults else "")
    failed = product_failed          # the ladder below judges PRODUCT failures
    fail_rate = failed / total

    if fail_rate > 0.20:
        return ReleaseDecision(
            status=ReleaseStatus.BLOCKED,
            label="BLOCKED",
            description=f"{failed}/{total} product tests failed ({fail_rate:.0%}) — critical regression.{infra_note}",
            color="#dc2626", bg="#fef2f2",
        )
    if fail_rate > 0.05:
        return ReleaseDecision(
            status=ReleaseStatus.AT_RISK,
            label="AT RISK",
            description=f"{failed}/{total} product tests failed ({fail_rate:.0%}) — proceed with caution.{infra_note}",
            color="#ea580c", bg="#fff7ed",
        )
    if fail_rate > 0:
        return ReleaseDecision(
            status=ReleaseStatus.WARNING,
            label="WARNING",
            description=f"{failed}/{total} product tests failed — minor issues, review before release.{infra_note}",
            color="#ca8a04", bg="#fefce8",
        )

    return ReleaseDecision(
        status=ReleaseStatus.READY,
        label="READY",
        description="All tests passed. Safe to release.",
        color="#16a34a", bg="#f0fdf4",
    )
