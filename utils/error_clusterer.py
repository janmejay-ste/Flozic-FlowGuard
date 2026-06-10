"""
Error clusterer — groups JS console events into named clusters.
Python port of Java ErrorClusterer.

The clusterer:
  1. Filters to FAIL/WARN-severity events only
  2. Normalises each message to a stable fingerprint (strips URLs, numbers,
     stack-frame line refs)
  3. Groups events sharing the same fingerprint
  4. Classifies each cluster's domain (PRODUCT / INFRASTRUCTURE / FRAMEWORK)
     and severity (CRITICAL / HIGH / MEDIUM / LOW)

Usage:
    from utils.error_clusterer import cluster
    clusters = cluster([(event, "test_foo"), (event2, "test_bar")])
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from utils.js_console_monitor import ConsoleEvent, Severity, JsConsoleMonitor

# ── Domain classification patterns ──────────────────────────────────────────

_FRAMEWORK_PATTERNS = [
    "ExpressionChangedAfterItHasBeenChecked",
    "NgZone",
    "ChangeDetectionStrategy",
    "Angular",
    "react",
    "vuejs",
    "hydration",
    "ChunkLoadError",
    "Loading chunk",
    "module federation",
    "lazy loading",
    "__webpack",
    "zone.js",
    "core.js",
]

_INFRA_PATTERNS = [
    "net::ERR_",
    "Failed to fetch",
    "NetworkError",
    "ERR_CONNECTION",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_FAILED",
    "status 5",
    " 502 ",
    " 503 ",
    " 504 ",
    "gateway timeout",
    "service unavailable",
    "CORS",
    "Cross-Origin",
    "WebSocket",
    "socket hang up",
    "ECONNRESET",
    "ETIMEDOUT",
]

# ── Severity classification patterns ────────────────────────────────────────

_CRITICAL_PATTERNS = [
    "Uncaught",
    "TypeError",
    "ReferenceError",
    "SyntaxError",
    "RangeError",
    "Cannot read propert",
    "is not a function",
    "is not defined",
    "is not a constructor",
    " 500 ",
    " 503 ",
    "Internal Server Error",
    "ChunkLoadError",
    "Loading chunk",
    "Invariant Violation",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_NAME_NOT_RESOLVED",
]

_HIGH_PATTERNS = [
    "net::ERR_",
    "Failed to fetch",
    "CORS",
    " 502 ",
    " 404 ",
    "NetworkError",
    "ExpressionChangedAfterItHasBeenChecked",
]


# ── Public data class ────────────────────────────────────────────────────────

@dataclass
class ErrorCluster:
    """
    One group of related JS errors.

    title          — normalised fingerprint (serves as the cluster ID)
    count          — number of raw events matching this fingerprint
    domain         — "PRODUCT" | "INFRASTRUCTURE" | "FRAMEWORK" | "UNKNOWN"
    severity       — "CRITICAL" | "HIGH" | "MEDIUM" | "LOW"
    sample_message — first raw message, truncated to 300 chars
    first_seen_in  — test method name that produced the first event
    is_new         — True if this fingerprint was not seen in the previous run
    """
    title: str
    count: int
    domain: str
    severity: str
    sample_message: str
    first_seen_in: str
    is_new: bool = False


# ── Public entry point ───────────────────────────────────────────────────────

def cluster(
    events: Sequence[tuple[ConsoleEvent, str]],
    *,
    previous_titles: set[str] | None = None,
) -> list[ErrorCluster]:
    """
    Cluster JS console events into ErrorCluster groups.

    events          — sequence of (ConsoleEvent, test_name) tuples accumulated
                      across the test session.
    previous_titles — fingerprint titles seen in the previous run; clusters
                      absent from this set are marked is_new=True.

    Returns a list sorted by severity (CRITICAL first) then count descending.
    """
    prev = previous_titles or set()

    # 1. Keep only FAIL/WARN events
    relevant = [
        (ev, name)
        for ev, name in events
        if JsConsoleMonitor.classify(ev) in (Severity.FAIL, Severity.WARN)
    ]

    # 2. Group by fingerprint
    groups: dict[str, list[tuple[ConsoleEvent, str]]] = {}
    for ev, name in relevant:
        fp = _fingerprint(ev.text)
        groups.setdefault(fp, []).append((ev, name))

    # 3. Build clusters
    result: list[ErrorCluster] = []
    for fp, group in groups.items():
        first_ev, first_test = group[0]
        result.append(
            ErrorCluster(
                title=fp,
                count=len(group),
                domain=_classify_domain(first_ev.text),
                severity=_classify_severity(first_ev.text),
                sample_message=(first_ev.text or "")[:300],
                first_seen_in=first_test,
                is_new=(fp not in prev),
            )
        )

    # 4. Sort: severity order then count descending
    _order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    result.sort(key=lambda c: (_order.get(c.severity, 4), -c.count))
    return result


def load_previous_titles(snapshot_path: str = "reports/trend/python-health-snapshot.json") -> set[str]:
    """
    Read the previous run's cluster titles from the snapshot JSON.
    Returns empty set if file missing or malformed.
    """
    import json
    from pathlib import Path

    try:
        p = Path(snapshot_path)
        if not p.exists():
            return set()
        data = json.loads(p.read_text(encoding="utf-8"))
        titles: set[str] = set()
        for c in data.get("jsClusters", []):
            t = c.get("title")
            if t:
                titles.add(t)
        return titles
    except Exception:
        return set()


# ── Private helpers ──────────────────────────────────────────────────────────

def _fingerprint(text: str) -> str:
    """
    Normalise a JS error message to a stable, human-readable fingerprint.
    Mirrors Java ErrorClusterer.computeFingerprint().

    Steps:
      - strip URLs → <URL>
      - strip long numbers (IDs, timestamps, line numbers ≥4 digits) → <N>
      - strip hex addresses → <HEX>
      - collapse whitespace
      - truncate to 120 chars
    """
    s = text or ""
    s = re.sub(r"https?://[^\s\"']+", "<URL>", s)
    s = re.sub(r"0x[0-9a-fA-F]+", "<HEX>", s)
    s = re.sub(r"\b\d{4,}\b", "<N>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]


def _classify_domain(text: str) -> str:
    lower = (text or "").lower()
    # Framework patterns take priority (they're often first-party code noise)
    for p in _FRAMEWORK_PATTERNS:
        if p.lower() in lower:
            return "FRAMEWORK"
    for p in _INFRA_PATTERNS:
        if p.lower() in lower:
            return "INFRASTRUCTURE"
    return "PRODUCT"


def _classify_severity(text: str) -> str:
    lower = (text or "").lower()
    for p in _CRITICAL_PATTERNS:
        if p.lower() in lower:
            return "CRITICAL"
    for p in _HIGH_PATTERNS:
        if p.lower() in lower:
            return "HIGH"
    return "MEDIUM"
