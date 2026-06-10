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
) -> ReleaseDecision:
    """
    Priority ladder (mirrors Java RiskInterpreter):
      1. Any failures in smoke tests → BLOCKED
      2. fail rate > 20 % overall   → BLOCKED
      3. fail rate > 5 %            → AT_RISK
      4. fail rate > 0 %            → WARNING
      5. all pass                   → READY
    """
    if total == 0:
        return ReleaseDecision(
            status=ReleaseStatus.WARNING,
            label="NO DATA",
            description="No tests recorded this run.",
            color="#92400e", bg="#fffbeb",
        )

    # Smoke gate
    if smoke_total > 0 and smoke_passed < smoke_total:
        smoke_failures = smoke_total - smoke_passed
        return ReleaseDecision(
            status=ReleaseStatus.BLOCKED,
            label="BLOCKED",
            description=f"{smoke_failures} smoke test(s) failed — release blocked.",
            color="#dc2626", bg="#fef2f2",
        )

    fail_rate = failed / total

    if fail_rate > 0.20:
        return ReleaseDecision(
            status=ReleaseStatus.BLOCKED,
            label="BLOCKED",
            description=f"{failed}/{total} tests failed ({fail_rate:.0%}) — critical regression.",
            color="#dc2626", bg="#fef2f2",
        )
    if fail_rate > 0.05:
        return ReleaseDecision(
            status=ReleaseStatus.AT_RISK,
            label="AT RISK",
            description=f"{failed}/{total} tests failed ({fail_rate:.0%}) — proceed with caution.",
            color="#ea580c", bg="#fff7ed",
        )
    if fail_rate > 0:
        return ReleaseDecision(
            status=ReleaseStatus.WARNING,
            label="WARNING",
            description=f"{failed}/{total} tests failed — minor issues, review before release.",
            color="#ca8a04", bg="#fefce8",
        )

    return ReleaseDecision(
        status=ReleaseStatus.READY,
        label="READY",
        description="All tests passed. Safe to release.",
        color="#16a34a", bg="#f0fdf4",
    )
