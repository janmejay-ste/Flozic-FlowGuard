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


def compute(
    records: Sequence[TestRecord],
    clusters: Sequence[ErrorCluster],
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
    # "product failures" = any test that failed (excluding framework-tagged ones)
    product_fails = [
        r for r in records
        if r.status == "FAIL" and r.feature.upper() not in ("FRAMEWORK", "AUTOMATION")
    ]

    if total == 0:
        product_base = 100
    else:
        product_base = round((total - len(product_fails)) / total * 100)

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

    # ── Overall (weighted) ────────────────────────────────────────────────────
    overall = round(
        product_health   * 0.50 +
        infra_health     * 0.30 +
        framework_health * 0.20
    )

    return LayeredScores(
        product_health=product_health,
        infra_health=infra_health,
        framework_health=framework_health,
        overall=overall,
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
