"""
Minimal v3-schema snapshot writer. Writes Python-side test results
into a file the Java DashboardBuilder / HealthTracker can later read
or merge.

Phase 2 scope only — this is the bridge, not a full HealthTracker port.
We do NOT compute scores here. We just emit a contributor-rich JSON
file (`reports/trend/python-health-snapshot.json`) in the v3 schema
shape so the Java side can choose to merge it into the canonical
snapshot during dashboard generation.

The schema MUST match `SnapshotSchemaVersion.V3_CONTRIBUTOR_RICH` in
the Java repo (`utils.health.schema.SnapshotSchemaVersion`). If that
schema changes, this writer must change too — see
docs/snapshot-contract.md for the shared contract.

Out of scope (intentionally):
- Score computation (lives in Java HealthPolicy / HealthTracker)
- Penalty application (lives in Java)
- Cluster fingerprinting / C4 work (lives in Java)
- Smoothing, EMA, integrity checks (lives in Java)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SNAPSHOT_PATH = Path("reports/trend/python-health-snapshot.json")
SCHEMA_VERSION = 3  # MUST match Java SnapshotSchemaVersion.V3_CONTRIBUTOR_RICH


@dataclass
class TestRecord:
    """One test method's result, in the shape the Java analytics expects."""
    category: str           # SMOKE / SANITY / REGRESSION / FULL
    login: str              # "Auth" or "Guest"
    feature: str
    clazz: str              # Java-side field name kept for cross-stack compat
    method: str
    status: str             # PASS / FAIL / SKIP
    duration_ms: int
    artifact_folder: str | None = None
    video_path: str | None = None
    # Cohort tagging — separates the stability-baseline measurement from
    # ad-hoc experimental migrations during the observation window.
    # See docs/observation-protocol.md.
    cohort: str = "baseline"
    # True when the failure was a harness fault (see utils/harness_errors).
    # In-memory only — deliberately absent from _test_record_to_json, because
    # the emitted JSON is a schema-3 contract shared with the Java side and
    # this flag is for local scoring, not cross-stack exchange.
    harness_fault: bool = False
    # Raw first line of the failure (assertion/exception). Persisted for FAILs
    # so cross-run + within-run SYSTEMIC clustering can group by NORMALIZED
    # signature rather than by feature name. Empty for passing tests.
    error_signature: str = ""


@dataclass
class _State:
    """Singleton-ish state. Append-only during a pytest session."""
    test_records: list[TestRecord] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))


_STATE = _State()


def add_test_record(
    *,
    category: str,
    login: str,
    feature: str,
    clazz: str,
    method: str,
    status: str,
    duration_ms: int,
    artifact_folder: str | None = None,
    video_path: str | None = None,
    cohort: str = "baseline",
    harness_fault: bool = False,
    error_signature: str = "",
) -> None:
    """Record one test result. Thread-safe."""
    with _STATE.lock:
        _STATE.test_records.append(
            TestRecord(
                category=category,
                login=login,
                feature=feature,
                clazz=clazz,
                method=method,
                status=status,
                duration_ms=duration_ms,
                artifact_folder=artifact_folder,
                video_path=video_path,
                cohort=cohort,
                harness_fault=harness_fault,
                error_signature=error_signature,
            )
        )


def write_snapshot(target: Path = SNAPSHOT_PATH) -> Path:
    """
    Write the v3 schema snapshot. Called once per pytest session at
    teardown. Returns the path written to.

    The shape mirrors the Java health_snapshot.json minimally:
      - schemaVersion: 3
      - timestamp: epoch millis
      - tests: array of test records
      - scoreContributors: empty buckets (the JAVA side computes scores)
      - source: "python" so a Java-side merger can identify origin
    """
    with _STATE.lock:
        records = list(_STATE.test_records)

    target.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "source": "python",
        "timestamp": int(time.time() * 1000),
        "startedAt": _STATE.started_at_ms,
        "tests": [_test_record_to_json(r) for r in records],
        # We deliberately do NOT populate scoreContributors here. Score
        # computation is Java's responsibility per the migration plan;
        # populating these would invite drift between two scoring
        # implementations. Leave the buckets empty; Java merges and scores.
        "scoreContributors": {
            "testFailures": {"source": "testFailures", "items": []},
            "jsErrors": {"source": "jsErrors", "items": []},
            "fallbacks": {"source": "fallbacks", "items": []},
            "totalRaw": 0.0,
            "totalApplied": 0.0,
            "totalSuppressed": 0.0,
        },
    }

    # Run-execution status so history readers (trend, flake window) can skip
    # EMPTY sessions instead of counting them as runs.
    from utils.report_data_sidecar import classify_execution
    payload["runStatus"] = classify_execution(len(records))
    # Atomic swap — snapshots are read cross-engine by the combined report
    # while parallel runs may still be writing.
    from utils.atomic_io import atomic_write_json
    atomic_write_json(target, payload)
    logger.info(
        "[SnapshotWriter] Wrote %d test record(s) to %s",
        len(records),
        target,
    )
    return target


def _test_record_to_json(r: TestRecord) -> dict[str, Any]:
    out: dict[str, Any] = {
        "category": r.category,
        "login": r.login,
        "feature": r.feature,
        "class": r.clazz,  # match Java field name
        "method": r.method,
        "status": r.status,
        "duration": r.duration_ms,
        "cohort": r.cohort,  # baseline / experiment-* / etc.
    }
    if r.artifact_folder is not None:
        out["artifacts"] = r.artifact_folder
    if r.video_path is not None:
        out["videoPath"] = r.video_path
    # Only emitted for failures — keeps the passing-test JSON identical to the
    # schema-3 contract; consumers that don't know the field simply ignore it.
    if r.error_signature:
        out["errorSignature"] = r.error_signature
    return out
