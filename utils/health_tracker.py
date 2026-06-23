"""
Session-level health tracker — 0-100 score computation.
Python port of Java HealthTracker.

Responsibilities:
  - Accumulate JS console events from per-test monitors across the session.
  - Build ErrorCluster groups on demand.
  - Compute a 0–100 health score:
        base  = pass_rate (0–100)
        penalty per CRITICAL cluster: -5 pts (capped at -20 total)
        penalty per HIGH cluster:     -2 pts (capped at -10 total)
  - Expose the clusters list for the dashboard and snapshot writer.

Thread-safe: all mutations are protected by a Lock (pytest-xdist workers
each have their own process, so the Lock is only relevant for future
intra-process parallel use).

Usage (in conftest.py page-fixture teardown):
    from utils.health_tracker import record_js_events
    record_js_events(monitor.events, request.node.name)

Usage (in conftest.py session teardown):
    from utils.health_tracker import get_clusters, compute_score
    clusters = get_clusters()
    score    = compute_score(total, passed, failed)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Sequence

from utils.js_console_monitor import ConsoleEvent
from utils.error_clusterer import ErrorCluster, cluster as _build_clusters, load_previous_titles


# ── Module-level singleton state ─────────────────────────────────────────────

@dataclass
class _HealthState:
    js_events: list[tuple[ConsoleEvent, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _clusters_cache: list[ErrorCluster] | None = None  # invalidated on new events
    # Login-route observations: (test_name, route, url) — route ∈
    # {"legacy-appypie", "flozic-authv2", "unknown"}. Surfaced on the dashboard
    # so we can see when the legacy accounts.appypie.com path was hit vs the
    # new direct flozic login.
    login_routes: list[tuple[str, str, str]] = field(default_factory=list)


_STATE = _HealthState()


# ── Public API ───────────────────────────────────────────────────────────────

def record_js_events(events: Sequence[ConsoleEvent], test_name: str) -> None:
    """
    Record JS console events from one test's JsConsoleMonitor.
    Thread-safe. Call once per test at teardown.
    """
    with _STATE.lock:
        _STATE._clusters_cache = None  # invalidate lazy cache
        for ev in events:
            _STATE.js_events.append((ev, test_name))


def get_clusters(*, reload_previous: bool = True) -> list[ErrorCluster]:
    """
    Return (cached) ErrorCluster groups built from all events recorded so far.

    reload_previous: if True, mark clusters as is_new=True when their
                     fingerprint was not present in the previous run's snapshot.
    """
    with _STATE.lock:
        if _STATE._clusters_cache is None:
            prev = load_previous_titles() if reload_previous else set()
            _STATE._clusters_cache = _build_clusters(
                list(_STATE.js_events),
                previous_titles=prev,
            )
        return list(_STATE._clusters_cache)


def compute_score(total: int, passed: int, failed: int) -> int:
    """
    Compute a 0–100 health score for this session.

    Formula (mirrors Java HealthTracker):
      base            = round(passed / total × 100)   [0–100, 0 when no tests]
      crit_penalty    = min(critical_cluster_count × 5, 20)
      high_penalty    = min(high_cluster_count     × 2, 10)
      score           = max(0, base − crit_penalty − high_penalty)

    Returns 0 when total == 0 (no data).
    """
    if total == 0:
        return 0

    base = round(passed / total * 100)

    clusters = get_clusters()
    crit_count = sum(1 for c in clusters if c.severity == "CRITICAL")
    high_count = sum(1 for c in clusters if c.severity == "HIGH")

    crit_penalty = min(crit_count * 5, 20)
    high_penalty = min(high_count * 2, 10)

    return max(0, base - crit_penalty - high_penalty)


def record_login_route(test_name: str, route: str, url: str) -> None:
    """Record which login surface a test hit. Thread-safe."""
    with _STATE.lock:
        _STATE.login_routes.append((test_name, route, url))


def get_login_routes() -> list[tuple[str, str, str]]:
    """Return a snapshot of the recorded login-route observations."""
    with _STATE.lock:
        return list(_STATE.login_routes)


def reset() -> None:
    """Clear all accumulated state. Useful between sessions in tests-of-tests."""
    with _STATE.lock:
        _STATE.js_events.clear()
        _STATE._clusters_cache = None
        _STATE.login_routes.clear()
