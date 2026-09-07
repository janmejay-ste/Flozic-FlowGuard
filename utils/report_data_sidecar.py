"""
report-data.json — the authoritative, versioned per-engine run record.

This is the single source of truth the combined cross-engine report reads. It
persists everything the report needs that would otherwise die with the
in-session process: the authoritative LayeredScores (Product/Infra/Framework
depend on in-session JS ErrorClusters), the release decision, the login-route
observations, and the JS error clusters — plus run provenance (git + browser +
environment) so a future failure can be traced to the exact code and env that
produced it.

Design principles enforced here (per the approved Hybrid spec):
  * VERSIONED. `schema_version` lets the reader evolve without breaking old files.
  * AUTHORITATIVE ONLY. We write the scores the run actually computed in-session.
    The reader NEVER recomputes from partial data; absent data stays absent.
  * EXECUTION STATUS. `execution_status` (COMPLETED / EMPTY / ABORTED / …) lets
    the trend/flake layers exclude non-runs instead of treating 0/0 as a result.

The writer is import-safe and never raises into a run: a report artifact must
never be able to turn a green run red.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FILENAME = "report-data.json"


# ─────────────────────────────────────────────────────────────────────────────
# Provenance capture
# ─────────────────────────────────────────────────────────────────────────────

def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=5,
            cwd=os.getcwd(),
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def git_provenance() -> dict[str, Any]:
    commit = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    status = _git("status", "--porcelain")
    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(status) if status is not None else None,
    }


def _pkg_version(name: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


def environment_provenance(engine: str, browser_version: str | None = None) -> dict[str, Any]:
    return {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "playwright": _pkg_version("playwright"),
        "browser": engine,
        "browser_version": browser_version,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Serialization of in-session-only structures
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_login_routes(routes: Sequence[Any] | None) -> list[dict[str, str]]:
    """health_tracker.get_login_routes() → list[(test_name, route, url)]."""
    out: list[dict[str, str]] = []
    for item in routes or []:
        try:
            test_name, route, url = item
            out.append({"test": str(test_name), "route": str(route), "url": str(url)})
        except (ValueError, TypeError):
            continue
    return out


def _serialize_clusters(clusters: Sequence[Any] | None) -> list[dict[str, Any]]:
    """ErrorCluster objects → plain dicts. Dataclass-aware, with a defensive
    attribute fallback so a schema drift in ErrorCluster never crashes the run."""
    out: list[dict[str, Any]] = []
    for c in clusters or []:
        if is_dataclass(c) and not isinstance(c, type):
            try:
                out.append(_jsonable(asdict(c)))
                continue
            except Exception:
                pass
        out.append({
            k: _jsonable(getattr(c, k))
            for k in ("title", "message", "domain", "severity", "count",
                      "first_seen_in", "sample", "fingerprint")
            if hasattr(c, k)
        })
    return out


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


# ─────────────────────────────────────────────────────────────────────────────
# Schema assembly + write
# ─────────────────────────────────────────────────────────────────────────────

def classify_execution(total: int) -> str:
    """Execution status for the run. P0 minimum: COMPLETED vs EMPTY.
    (ABORTED / INFRA_FAILED / CONFIG_FAILED are reserved for callers that can
    detect them; the trend/flake layers key off COMPLETED.)"""
    return "COMPLETED" if total > 0 else "EMPTY"


def build_sidecar(
    *,
    engine: str,
    run_started_ms: int | None,
    scores: Any,                     # LayeredScores (may be None)
    decision: Any,                   # object/enum/str with the release decision
    stats: dict[str, Any] | None,    # {total, passed, failed, ...}
    login_routes: Sequence[Any] | None = None,
    clusters: Sequence[Any] | None = None,
    browser_version: str | None = None,
    suite: str | None = None,        # "full" | "mobile" | ...
) -> dict[str, Any]:
    """Assemble the report-data.json dict (does not write). Pure + defensive:
    every field degrades to None rather than raising."""
    stats = stats or {}
    total = int(stats.get("total", 0) or 0)
    now_ms = int(time.time() * 1000)

    def sc(attr: str) -> Any:
        return getattr(scores, attr, None) if scores is not None else None

    decision_str = None
    if decision is not None:
        decision_str = getattr(getattr(decision, "status", decision), "value",
                               None) or getattr(decision, "value", None) or str(decision)

    if suite is None:
        suite = "full" if total > 200 else ("mobile" if total else "empty")

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "run_id": f"{engine}-{run_started_ms or now_ms}",
        "run_started_at": run_started_ms,
        "run_finished_at": now_ms,
        "execution_status": classify_execution(total),
        "population": {
            "suite": suite,
            "total": total,
            "executed": total,
        },
        "health": {
            "overall": sc("overall"),
            "product": sc("product_health"),
            "infrastructure": sc("infra_health"),
            "framework": sc("framework_health"),
            "mobile": sc("mobile_health"),
            "scoring_version": sc("scoring_version"),
        },
        "release": {
            "decision": decision_str,
        },
        "stats": {
            "total": total,
            "passed": int(stats.get("passed", 0) or 0),
            "failed": int(stats.get("failed", 0) or 0),
        },
        "login_routes": _serialize_login_routes(login_routes),
        "js_error_clusters": _serialize_clusters(clusters),
        "git": git_provenance(),
        "environment": environment_provenance(engine, browser_version),
    }


PARTIAL_FILENAME = "report-data-partial.json"


def write_sidecar(root: Path | str, payload: dict[str, Any],
                  filename: str | None = None) -> Path | None:
    """Write the sidecar under `root`. Never raises into the run.

    filename=None routes by suite: FULL-suite runs own report-data.json (the
    authoritative record the combined report anchors to); partial runs write
    report-data-partial.json so a mobile-only or subset run can NEVER displace
    the full-run record — the clobber that repeatedly degraded the report."""
    try:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        if filename is None:
            suite = (payload.get("population") or {}).get("suite")
            filename = FILENAME if suite == "full" else PARTIAL_FILENAME
        path = root / filename
        # Atomic swap: the other engine's parallel rebuild reads this sidecar —
        # it must see the previous complete version or this one, never a partial.
        from utils.atomic_io import atomic_write_json
        atomic_write_json(path, payload)
        logger.info("[report-data] wrote %s (schema v%d, status=%s)",
                    path, payload.get("schema_version"), payload.get("execution_status"))
        return path
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[report-data] sidecar write failed (non-fatal): %s", e)
        return None


def read_sidecar(root: Path | str) -> dict[str, Any] | None:
    try:
        return json.loads((Path(root) / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
