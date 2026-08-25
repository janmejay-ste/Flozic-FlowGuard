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
  (with mobile data present, weights renormalise to 40/25/20/15 -- v2+ --
  and Mobile itself is Quality × coverage, see SCORING_VERSION below -- v3)

Usage:
    from utils.layered_health_scores import compute, LayeredScores
    scores = compute(records, clusters)
    print(scores.product_health, scores.infra_health, scores.framework_health)
"""

from __future__ import annotations

import math
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
#   v3 = Mobile itself becomes Quality x coverage. Quality is severity x a
#        device-reach curve computed over unique patterns (not raw finding
#        counts), with worst-severity ceilings. Coverage is
#        executed / executable (structural/N-A checks excluded from the
#        denominator). The combined score is gated to None below 0.33
#        executable coverage -- too little was looked at to trust a number.
#        WITHOUT mobile data the weights still renormalise to exactly v1,
#        same as v2.
SCORING_VERSION = 3

# v3 mobile model. PROVISIONAL constants — see the calibration plan in
# docs/superpowers/specs/2026-08-25-mobile-scoring-v3-design.md. K and the
# ceilings have no major/blocker mobile data to fit against yet.
_MOBILE_SEV_WEIGHT = {"minor": 1.0, "major": 5.0, "blocker": 12.0}
_MOBILE_K = 120.0                    # Quality = 50 at Σ ≈ 83 weighted patterns
_MOBILE_CEILING_BLOCKER = 20
_MOBILE_CEILING_MAJOR = 55
_MOBILE_COVERAGE_GATE = 0.33


def _mobile_quality(findings):
    """Quality 0–100 from unique issue patterns: severity × device-reach on a
    saturating curve, capped by the worst severity present."""
    from utils.ai_mobile_triage import group_findings
    counts = {"blocker": 0, "major": 0, "minor": 0}
    for f in findings:
        s = (f.get("severity") or "").lower()
        if s in counts:
            counts[s] += 1
    groups = group_findings(findings)
    if not groups:
        return 100, counts
    total_dev = len({f.get("device") for f in findings if f.get("device")}) or 1

    def reach(n):
        return 0.5 + (n - 1) / max(total_dev - 1, 1)

    sigma = sum(_MOBILE_SEV_WEIGHT[g["severity"]] * reach(len(g["devices"]))
                for g in groups)
    quality = round(100 * math.exp(-sigma / _MOBILE_K))
    if any(g["severity"] == "blocker" for g in groups):
        quality = min(quality, _MOBILE_CEILING_BLOCKER)
    elif any(g["severity"] == "major" for g in groups):
        quality = min(quality, _MOBILE_CEILING_MAJOR)
    return quality, counts


def _mobile_coverage(check_stats):
    """coverage = executed / executable, executable = attempted − na_structural.
    None => not instrumented (cannot discount what we did not measure).
    0.0  => a check_stats dict was present but nothing was executable (includes
            attempted == 0, and all-structural attempted > 0 cases) — this
            drives the coverage gate to fire, since 0.0 < _MOBILE_COVERAGE_GATE."""
    if not check_stats:
        return None
    att = int(check_stats.get("attempted", 0) or 0)
    executable = att - int(check_stats.get("na_structural", 0) or 0)
    if executable <= 0:
        return 0.0
    return min(int(check_stats.get("executed", 0) or 0) / executable, 1.0)


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
    mobile_quality: int | None = None
    mobile_coverage: float | None = None
    mobile_major: int = 0
    mobile_minor: int = 0
    mobile_blocker: int = 0
    scoring_version: int = SCORING_VERSION


def compute(
    records: Sequence[TestRecord],
    clusters: Sequence[ErrorCluster],
    mobile_findings: Sequence[dict] | None = None,
    mobile_tested: bool | None = None,
    check_stats: dict | None = None,
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
    mobile_quality: int | None = None
    mobile_coverage: float | None = None
    m_blocker = m_major = m_minor = 0
    if tested:
        mobile_quality, counts = _mobile_quality(mf)
        m_blocker, m_major, m_minor = counts["blocker"], counts["major"], counts["minor"]
        cov = _mobile_coverage(check_stats)
        mobile_coverage = cov
        if cov is None:
            mobile_health = mobile_quality           # not instrumented: quality alone
        elif cov < _MOBILE_COVERAGE_GATE:
            mobile_health = None                      # insufficient coverage -> excluded
        else:
            mobile_health = round(mobile_quality * cov)

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
        mobile_quality=mobile_quality,
        mobile_coverage=mobile_coverage,
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
