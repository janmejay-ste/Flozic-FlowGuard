"""
Failure clustering across runs.

Reads every archived snapshot under reports/trend/snapshots/*.json (newest
first), groups failures by a stable fingerprint, and produces a list of
clusters with a recurring-failure score.

Why the existing ErrorClusterer is not enough: that one clusters JS errors
WITHIN a single run. This module clusters TEST FAILURES ACROSS RUNS — a
different signal entirely. A test that fails 5 times in 30 runs is a
known-flaky/known-bad bug; a one-off failure may just be noise.

Fingerprint strategy:
  hash(test_method + normalized_exception_message)

Where normalization strips:
  - Timestamps (dates / times / unix epochs)
  - Hex / uuid / base64 tokens (session ids, mongo oids)
  - Memory addresses (0x...)
  - Trailing 'after Nms', 'in N.Ns' duration suffixes
  - Specific URL paths (keep host)
  - Line numbers ':L12' in playwright locator descriptions

so two failures that differ only in dynamic content land in the same cluster.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = Path("reports/trend/snapshots")

# Default window: 30 runs. The user originally proposed this number in the
# roadmap discussion ("a bug appearing 5 times in 30 runs is far more
# important than a one-off failure").
DEFAULT_WINDOW = 30


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation regexes — applied in order. Each strips a class of dynamic
# content that would otherwise prevent two semantically-identical failures
# from sharing a fingerprint.
# ─────────────────────────────────────────────────────────────────────────────
_NORMALIZE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ISO-8601-ish timestamps and dates
    (re.compile(r"\d{4}-\d{2}-\d{2}[T_ ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"), "<TS>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "<DATE>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<TIME>"),
    # Memory addresses, hex blobs ≥ 6 chars, mongo ObjectIds, UUIDs
    (re.compile(r"0x[0-9a-fA-F]+"), "<ADDR>"),
    (re.compile(r"\b[0-9a-fA-F]{24,}\b"), "<ID>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    # Base64-ish (>=20 chars of [A-Za-z0-9+/=]). Matches the AIFormAutomationPrompt= URL params.
    (re.compile(r"[A-Za-z0-9+/=]{20,}"), "<BLOB>"),
    # 'Timeout 30000ms', 'after 5.2s', etc.
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|seconds?|s|minutes?|min)\b",
                re.IGNORECASE), "<DUR>"),
    # File paths beyond the file name (e.g. C:\Users\foo\bar.py -> bar.py)
    (re.compile(r"[A-Za-z]:[\\/](?:[^\s'\"]+[\\/])+"), "<PATH>"),
    (re.compile(r"/(?:[^\s'\"]+/)+"), "<PATH>"),
    # Compact runs of digits — most often dynamic line/index/duration numbers
    (re.compile(r"\b\d{3,}\b"), "<N>"),
]


def normalize_message(msg: str) -> str:
    """Strip dynamic content so two semantically-identical failures match."""
    out = msg or ""
    for pattern, replacement in _NORMALIZE_PATTERNS:
        out = pattern.sub(replacement, out)
    # Collapse repeated whitespace
    out = re.sub(r"\s+", " ", out).strip()
    return out


def fingerprint(test_method: str, exception_or_message: str) -> str:
    """Stable hash for a (test_method, normalized_message) pair."""
    body = f"{test_method}\n{normalize_message(exception_or_message)}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot loading
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FailureOccurrence:
    """A single failed test from one historical run."""
    run_id: str          # filename stem of the archived snapshot, e.g. '20260609_143012'
    test_method: str
    feature: str = ""
    category: str = ""
    message: str = ""    # original (un-normalised) failure message, if available
    fingerprint: str = ""


def _occurrence_from_record(
    run_id: str, record: dict,
) -> FailureOccurrence | None:
    """Build a FailureOccurrence from one record in a snapshot. Returns None if
    the record is not a FAIL or doesn't have enough data."""
    if record.get("status") != "FAIL":
        return None
    method  = record.get("method") or record.get("name") or "<unknown>"
    feat    = record.get("feature", "")
    cat     = record.get("category", "")
    # The snapshot doesn't store the exception text by design. We use the
    # test method name + the artifact_folder timestamp as a coarse signal.
    # The dashboard's ai_triage.json (when present) would give a richer
    # message, but for cross-run clustering we want a stable signal that's
    # present in EVERY snapshot — so we stick to the test name + feature.
    # If two distinct exceptions in the same test happen, they'll cluster
    # together; that's acceptable for v1.
    message = f"feature={feat}"
    return FailureOccurrence(
        run_id=run_id,
        test_method=method,
        feature=feat,
        category=cat,
        message=message,
        fingerprint=fingerprint(method, message),
    )


def load_history(
    snapshot_dir: Path = SNAPSHOT_DIR,
    last_n: int = DEFAULT_WINDOW,
) -> list[tuple[str, list[FailureOccurrence]]]:
    """
    Load the last N archived snapshots (newest first) and extract their
    failure occurrences. Returns a list of (run_id, [occurrence,...]).

    Older snapshots fall off the end of the list — that's the rolling
    window mentioned in the roadmap.
    """
    if not snapshot_dir.exists():
        return []
    files = sorted(snapshot_dir.glob("*.json"), reverse=True)[:last_n]
    out: list[tuple[str, list[FailureOccurrence]]] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read snapshot %s: %s", f, e)
            continue
        run_id = f.stem
        records = data.get("tests") or data.get("records") or []
        occurrences = [
            o for o in (_occurrence_from_record(run_id, r) for r in records)
            if o is not None
        ]
        out.append((run_id, occurrences))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Clustering
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FailureCluster:
    """A group of failures sharing a fingerprint, observed across runs."""
    fingerprint: str
    sample_method: str
    sample_feature: str = ""
    first_seen: str = ""          # oldest run_id where this was observed
    last_seen:  str = ""          # newest run_id where this was observed
    occurrences_in_history: int = 0   # total times observed across the window
    occurrence_runs: list[str] = field(default_factory=list)  # which runs
    window_size: int = 0          # how many runs were in the window
    recurring_score: float = 0.0  # 0..1, recency-weighted occurrence rate
    sample_message: str = ""
    # Classification (min-observation rule). A failure seen once is NEW, not
    # "recurring" — calling a 1/30 failure recurring is exactly the bug this
    # field fixes. runs_seen / passes_in_history come from the test method's
    # full pass+fail history and let us distinguish FLAKY from CONSISTENT.
    runs_seen: int = 0            # runs (in window) where the test method ran at all
    passes_in_history: int = 0    # runs where the method PASSED
    label: str = "NEW"            # NEW | OBSERVED | RECURRING | FLAKY | CONSISTENT_FAILURE

    def to_dict(self) -> dict:
        return asdict(self)


def classify_failure(fail_runs: int, runs_seen: int, passed_runs: int) -> str:
    """Minimum-observation classification for a failing test.

    1 failure  -> NEW               (a one-off; never call this 'recurring')
    2 failures -> OBSERVED          (seen twice; still not established)
    3+ failures:
        also passed somewhere       -> FLAKY   (mixed pass/fail history)
        fails >=90% of runs seen    -> CONSISTENT_FAILURE
        otherwise                   -> RECURRING
    """
    if fail_runs <= 1:
        return "NEW"
    if fail_runs == 2:
        return "OBSERVED"
    if passed_runs >= 1:
        return "FLAKY"
    if runs_seen > 0 and fail_runs / runs_seen >= 0.9:
        return "CONSISTENT_FAILURE"
    return "RECURRING"


def _method_run_stats(snapshot_dir: Path, last_n: int) -> dict[str, dict[str, int]]:
    """Per test-method appearance stats across the window: how many runs it ran
    in (seen), passed in, and failed in. Feeds FLAKY vs CONSISTENT_FAILURE."""
    if not snapshot_dir.exists():
        return {}
    files = sorted(snapshot_dir.glob("*.json"), reverse=True)[:last_n]
    acc: dict[str, dict[str, set]] = {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        run_id = f.stem
        for r in (data.get("tests") or data.get("records") or []):
            m = r.get("method") or r.get("name")
            if not m:
                continue
            s = acc.setdefault(m, {"seen": set(), "passed": set(), "failed": set()})
            s["seen"].add(run_id)
            st = r.get("status")
            if st == "PASS":
                s["passed"].add(run_id)
            elif st == "FAIL":
                s["failed"].add(run_id)
    return {
        m: {"seen": len(v["seen"]), "passed": len(v["passed"]), "failed": len(v["failed"])}
        for m, v in acc.items()
    }


def _recurring_score(
    occurrence_indices: Iterable[int],
    window_size: int,
) -> float:
    """
    Weight recent occurrences more heavily than old ones.

    - Index 0 is the newest run, window_size-1 is the oldest.
    - Each occurrence contributes weight = (window_size - index) / window_size.
    - Score = sum(weights) / max_possible_weight_sum, clipped to [0, 1].

    The max-possible-sum (one occurrence in every run of the window) is
    sum_{k=1..N} k/N = (N+1)/2. Dividing by that pins 'chronic' = 1.0.

    Examples (window_size=30):
      - failed in 5 most recent runs       -> score ≈ 0.30   (hot)
      - failed in 5 runs 25-30 ago         -> score ≈ 0.03   (stale)
      - failed in every single run         -> score = 1.0    (chronic)
    """
    if window_size <= 0:
        return 0.0
    raw = 0.0
    for i in occurrence_indices:
        raw += (window_size - i) / window_size
    max_sum = (window_size + 1) / 2  # closed form of sum_{k=1..N} k/N
    return max(0.0, min(1.0, raw / max_sum))


def build_clusters(
    snapshot_dir: Path = SNAPSHOT_DIR,
    last_n: int = DEFAULT_WINDOW,
) -> list[FailureCluster]:
    """
    Walk the last N snapshots, group failures by fingerprint, and return
    a list of FailureCluster sorted by recurring_score (highest first).
    """
    history = load_history(snapshot_dir, last_n)
    if not history:
        return []

    # Per-fingerprint accumulator: indices into the history list where
    # the fingerprint appeared, plus sample data for display.
    accum: dict[str, dict] = {}
    for idx, (run_id, occurrences) in enumerate(history):
        # A given fingerprint may appear multiple times in one run (rare
        # but possible if two parametrize variants both fail the same way).
        # We dedup at the (fingerprint, run_id) level so re-counts don't
        # inflate the recurring score.
        seen_this_run: set[str] = set()
        for occ in occurrences:
            if occ.fingerprint in seen_this_run:
                continue
            seen_this_run.add(occ.fingerprint)

            entry = accum.setdefault(occ.fingerprint, {
                "sample_method":  occ.test_method,
                "sample_feature": occ.feature,
                "sample_message": occ.message,
                "indices":  [],
                "run_ids":  [],
            })
            entry["indices"].append(idx)
            entry["run_ids"].append(run_id)

    window_size = len(history)
    mstats = _method_run_stats(snapshot_dir, last_n)
    clusters: list[FailureCluster] = []
    for fp, entry in accum.items():
        indices = entry["indices"]
        run_ids = entry["run_ids"]
        method = entry["sample_method"]
        ms = mstats.get(method, {})
        fail_runs = len(indices)
        runs_seen = ms.get("seen", fail_runs)
        passed_runs = ms.get("passed", 0)
        clusters.append(FailureCluster(
            fingerprint=fp,
            sample_method=method,
            sample_feature=entry["sample_feature"],
            sample_message=entry["sample_message"],
            first_seen=run_ids[-1],   # oldest because history is newest-first
            last_seen=run_ids[0],
            occurrences_in_history=fail_runs,
            occurrence_runs=list(reversed(run_ids)),  # chronological
            window_size=window_size,
            recurring_score=_recurring_score(indices, window_size),
            runs_seen=runs_seen,
            passes_in_history=passed_runs,
            label=classify_failure(fail_runs, runs_seen, passed_runs),
        ))

    clusters.sort(key=lambda c: c.recurring_score, reverse=True)
    return clusters


def cluster_for_test(
    test_method: str,
    clusters: list[FailureCluster],
) -> FailureCluster | None:
    """Return the cluster whose sample_method matches, if any. Convenience
    used by the dashboard to render the per-row badge."""
    for c in clusters:
        if c.sample_method == test_method:
            return c
    return None
