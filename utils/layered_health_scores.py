"""
Layered health scores — domain-split 0-100 scoring.
Python port of Java LayeredHealthScores.

Three domain scores are computed independently, then combined into an
overall weighted score:

  Product Health (50% weight)
    — How well the application under test behaves from a user perspective.
    — Penalised by: PRODUCT-domain JS clusters + product feature failures.

  Infrastructure Health (30% weight)
    — How stable the network and backend services are.
    — Penalised by: INFRASTRUCTURE-domain clusters (network errors, 5xx, CORS).

  Framework Health (20% weight)
    — How healthy the frontend framework is (Angular, React, chunking, etc.).
    — Penalised by: FRAMEWORK-domain clusters (ChunkLoadError, ExpressionChanged…).

Overall = round(product × 0.50 + infra × 0.30 + framework × 0.20)

Usage:
    from utils.layered_health_scores import compute, LayeredScores
    scores = compute(records, clusters)
    print(scores.product_health, scores.infra_health, scores.framework_health)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from utils.snapshot_writer import TestRecord
from utils.error_clusterer import ErrorCluster


# Scoring formula version. Bump whenever the weighting or the penalty model
# changes, so a persisted score can be interpreted later.
#   v1 = product 50 / infra 30 / framework 20
#   v2 = adds Mobile. With mobile data: 40/25/20/15. WITHOUT mobile data the
#        weights renormalise to exactly v1, so a run with no mobile tests
#        scores identically under v1 and v2 -- the bump is not a silent
#        rescoring of the existing suite.
SCORING_VERSION = 2

# Mobile penalties, mirroring the cluster model above.
_MOBILE_PENALTY = {"blocker": 25, "major": 8, "minor": 3}
_MOBILE_PENALTY_CAP = 60

_W_WITH_MOBILE = {"product": 0.40, "infra": 0.25, "framework": 0.20, "mobile": 0.15}
_W_NO_MOBILE = {"product": 0.50, "infra": 0.30, "framework": 0.20}


@dataclass
class LayeredScores:
    """
    Domain-split health scores, all in range [0, 100].

    product_health   — App-level quality signal. 0 = critical product failure.
    infra_health     — Backend / network stability. 0 = connectivity collapse.
    framework_health — Frontend framework stability. 0 = broken build/chunking.
    overall          — Weighted composite (50/30/20 split).
    """
    product_health: int
    infra_health: int
    framework_health: int
    overall: int
    # Failures excluded from product scoring because the harness could not run
    # the check. Non-zero means product_health is based on fewer tests than ran
    # — surface it rather than letting a clean-looking score imply coverage.
    harness_fault_count: int = 0
    # None means NO mobile tests ran. Deliberately not 100: claiming perfect
    # mobile health for a run that never opened a mobile viewport is the same
    # false-signal class as scoring an unset env var against the product.
    mobile_health: int | None = None
    mobile_major: int = 0
    mobile_minor: int = 0
    mobile_blocker: int = 0
    scoring_version: int = SCORING_VERSION


def compute(
    records: Sequence[TestRecord],
    clusters: Sequence[ErrorCluster],
    mobile_findings: Sequence[dict] | None = None,
    mobile_tested: bool | None = None,
) -> LayeredScores:
    """
    Compute domain-split health scores from test records + JS error clusters.

    Penalty caps (per domain):
      CRITICAL cluster → −8 pts each (product), −10 pts (infra), −6 pts (framework)
      HIGH cluster     → −3 pts each (product), − 4 pts (infra), −3 pts (framework)
      Max penalty      → −40 pts (product), −50 pts (infra), −30 pts (framework)
    """
    total = len(records)

    # ── Product Health ────────────────────────────────────────────────────────
    product_clusters = [c for c in clusters if c.domain == "PRODUCT"]
    # "product failures" = any test that failed, EXCLUDING two kinds that say
    # nothing about the application:
    #   - framework/automation-tagged features (pre-existing rule)
    #   - harness faults, where the harness could not run the check at all
    #     (unset credentials, missing config — see utils/harness_errors)
    #
    # Attributing a harness fault to the product turns one unset env var into
    # "Product: 0", i.e. a report that the application is critically broken
    # when it was never exercised. Someone acts on that.
    harness_faults = [
        r for r in records
        if r.status == "FAIL" and getattr(r, "harness_fault", False)
    ]
    product_fails = [
        r for r in records
        if r.status == "FAIL"
        and r.feature.upper() not in ("FRAMEWORK", "AUTOMATION")
        and not getattr(r, "harness_fault", False)
    ]

    # Harness faults leave the denominator too: scoring 0/1 as "100% product
    # pass" would be the opposite lie. A run that checked nothing has no
    # product signal, so it reports the neutral 100 and relies on
    # harness_fault_count to say why.
    scored_total = total - len(harness_faults)
    if scored_total <= 0:
        product_base = 100
    else:
        product_base = round((scored_total - len(product_fails)) / scored_total * 100)

    prod_crit    = sum(1 for c in product_clusters if c.severity == "CRITICAL")
    prod_high    = sum(1 for c in product_clusters if c.severity == "HIGH")
    prod_penalty = min(prod_crit * 8 + prod_high * 3, 40)
    product_health = max(0, product_base - prod_penalty)

    # ── Infrastructure Health ─────────────────────────────────────────────────
    infra_clusters = [c for c in clusters if c.domain == "INFRASTRUCTURE"]
    infra_crit     = sum(1 for c in infra_clusters if c.severity == "CRITICAL")
    infra_high     = sum(1 for c in infra_clusters if c.severity == "HIGH")
    infra_penalty  = min(infra_crit * 10 + infra_high * 4, 50)
    infra_health   = max(0, 100 - infra_penalty)

    # ── Framework Health ──────────────────────────────────────────────────────
    fw_clusters   = [c for c in clusters if c.domain == "FRAMEWORK"]
    fw_crit       = sum(1 for c in fw_clusters if c.severity == "CRITICAL")
    fw_high       = sum(1 for c in fw_clusters if c.severity == "HIGH")
    fw_penalty    = min(fw_crit * 6 + fw_high * 3, 30)
    framework_health = max(0, 100 - fw_penalty)

    # ── Mobile Health ─────────────────────────────────────────────────────────
    # `mobile_findings` is the session accumulator from mobile_report_builder.
    # None or empty => no mobile tests ran => mobile is EXCLUDED from `overall`
    # rather than scored 100.
    mf = list(mobile_findings or [])
    # `mobile_tested` distinguishes "ran, found nothing" (score 100) from
    # "never ran" (None). Falling back to `bool(mf)` keeps older callers
    # working, but a clean mobile run then reads as not-measured -- which is
    # why the fixture reports the device list explicitly.
    tested = bool(mf) if mobile_tested is None else bool(mobile_tested)
    mobile_health: int | None = None
    m_blocker = m_major = m_minor = 0
    if tested:
        for f in mf:
            sev = (f.get("severity") or "").lower()
            if sev == "blocker":
                m_blocker += 1
            elif sev == "major":
                m_major += 1
            elif sev == "minor":
                m_minor += 1
        penalty = min(
            m_blocker * _MOBILE_PENALTY["blocker"]
            + m_major * _MOBILE_PENALTY["major"]
            + m_minor * _MOBILE_PENALTY["minor"],
            _MOBILE_PENALTY_CAP,
        )
        mobile_health = max(0, 100 - penalty)

    # ── Overall (weighted) ────────────────────────────────────────────────────
    if mobile_health is None:
        w = _W_NO_MOBILE
        overall = round(
            product_health * w["product"]
            + infra_health * w["infra"]
            + framework_health * w["framework"]
        )
    else:
        w = _W_WITH_MOBILE
        overall = round(
            product_health * w["product"]
            + infra_health * w["infra"]
            + framework_health * w["framework"]
            + mobile_health * w["mobile"]
        )

    return LayeredScores(
        product_health=product_health,
        infra_health=infra_health,
        framework_health=framework_health,
        overall=overall,
        harness_fault_count=len(harness_faults),
        mobile_health=mobile_health,
        mobile_major=m_major,
        mobile_minor=m_minor,
        mobile_blocker=m_blocker,
    )


def score_band(score: int) -> tuple[str, str, str]:
    """
    Return (label, text_colour, bg_colour) for a 0–100 score.
    Matches Java HealthPolicy band thresholds.
    """
    if score >= 90:
        return "EXCELLENT", "#14532d", "#dcfce7"
    if score >= 75:
        return "HEALTHY", "#166534", "#d1fae5"
    if score >= 60:
        return "FAIR", "#713f12", "#fef3c7"
    if score >= 40:
        return "POOR", "#9a3412", "#ffedd5"
    return "CRITICAL", "#dc2626", "#fee2e2"
