"""
Executive summary for the dashboard — DETERMINISTIC, not AI-generated.

At session teardown, the dashboard builder calls summarize() with the run's
stats + records (+ optional mobile finding context). Returns a short
high-level summary for non-QA stakeholders (PM, eng leads), embedded at the
top of the dashboard.

It is composed from the run's real numbers on purpose. The release call is a
decision (`decision_status`, computed in Python), not a sentence a model
should invent — this repo's rule is the AI observes and Python decides, and
"Recommended for release" is a verdict. A deterministic summary also cannot
hallucinate metrics or overclaim "meets all requirements" off the pytest
pass count while blind to the mobile findings. (The prior AI version did both.)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def summarize(
    stats: dict,
    records: list,
    decision_status: str = "READY",
    mobile: dict | None = None,
) -> str:
    """Build a short executive summary — DETERMINISTIC, composed from the run's
    real numbers.

    Why not AI here: an executive summary that ends "Recommended for release"
    is a RELEASE VERDICT, and in this repo the AI observes while Python decides
    — the release call is `decision_status`, computed deterministically, not a
    sentence a model invents. An earlier AI version also (a) hallucinated
    metrics it was told to withhold and (b) declared "meets all requirements /
    Recommended for release" off the pytest pass count alone, blind to the
    mobile findings. Composing from the real figures fixes both: it cannot
    overclaim, and it keeps test-execution readiness separate from product
    findings that still need review.

    `mobile`, when present: {"findings": int, "patterns": int,
    "coverage_gaps": int} — the actionable mobile finding/pattern counts and
    the number of structurally-untestable (UNTESTED/UNSUPPORTED) checks.
    """
    p, f, t = stats["passed"], stats["failed"], stats["total"]
    if t == 0:
        return "No tests ran in this session."

    parts: list[str] = []
    if f == 0:
        parts.append(
            f"All {t} regression tests completed successfully with no test failures."
        )
    else:
        parts.append(
            f"{p} of {t} regression tests passed; {f} failed and need triage "
            "(see the failing rows below)."
        )

    if mobile:
        nfind = int(mobile.get("findings") or 0)
        npat = int(mobile.get("patterns") or 0)
        ngap = int(mobile.get("coverage_gaps") or 0)
        if nfind:
            parts.append(
                f"The mobile suite identified {nfind} product finding(s) across "
                f"{npat} issue pattern(s) — see the Mobile section for severity "
                "and device reach."
            )
        if ngap:
            parts.append(
                f"{ngap} mobile check(s) are structurally untestable on the "
                "engine (unsupported CDP capabilities); these are reported as "
                "UNTESTED and excluded from scoring, not counted as passes."
            )

    # The release framing comes from the Python-computed decision, and product
    # findings are explicitly held apart from test-execution readiness.
    if f == 0 and (mobile and (mobile.get("findings") or mobile.get("coverage_gaps"))):
        parts.append(
            f"From a test-execution perspective the run is {decision_status}; "
            "the product findings above should be reviewed separately before "
            "release."
        )
    elif f == 0:
        parts.append(f"Release decision: {decision_status}.")
    else:
        parts.append(
            f"Release decision: {decision_status}. Resolve the failing tests "
            "before release."
        )
    return " ".join(parts)
