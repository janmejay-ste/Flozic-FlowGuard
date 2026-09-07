"""
Observability v2 / Phase 2 — Playwright trace retention.

A trace is captured in-memory for every test (context.tracing.start in the page
fixture) but only WRITTEN on failure — a dev opens it with
`playwright show-trace <path>` to scrub the exact DOM/network/console timeline
of the failure. Traces are ~2-10 MB each, so retention is capped: only the
newest MAX_TRACES across the whole run are kept on disk (oldest pruned), and
the report never embeds them (it links only).

Everything here is defensive: a tracing/pruning failure must never fail a test.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

FAILURES_ROOT = Path("reports/failures")
TRACE_NAME = "trace.zip"
# Newest-N kept run-wide. FLOWGUARD_TRACE_MAX overrides; keeps disk bounded on
# a full run that has dozens of failures.
DEFAULT_MAX_TRACES = 20


def _max_traces() -> int:
    try:
        return max(1, int(os.environ.get("FLOWGUARD_TRACE_MAX", DEFAULT_MAX_TRACES)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TRACES


def prune_traces(root: Path | str | None = None, keep: int | None = None) -> list[str]:
    """Keep only the newest `keep` trace.zip files under `root`; delete the
    rest. Returns the deleted paths. Never raises.

    root=None resolves the module global AT CALL TIME (not a frozen default
    argument), so tests and callers that repoint FAILURES_ROOT are honoured."""
    root = FAILURES_ROOT if root is None else root
    keep = _max_traces() if keep is None else keep
    try:
        traces = sorted(Path(root).glob(f"*/{TRACE_NAME}"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        deleted = []
        for p in traces[keep:]:
            try:
                p.unlink()
                deleted.append(str(p))
            except OSError:
                pass
        return deleted
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[trace] prune failed (non-fatal): %s", e)
        return []


def save_trace(context, folder: Path | str) -> Path | None:
    """Stop tracing on `context`, writing trace.zip into `folder`, then prune
    run-wide to the newest N. Returns the trace path (or None on failure).
    Caller guarantees this is a FAILED test."""
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / TRACE_NAME
        context.tracing.stop(path=str(path))
        prune_traces()
        return path
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[trace] save_trace failed (non-fatal): %s", e)
        # Best-effort discard so the tracing buffer doesn't leak into the next test.
        try:
            context.tracing.stop()
        except Exception:
            pass
        return None
