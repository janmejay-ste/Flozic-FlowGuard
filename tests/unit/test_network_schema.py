"""
Golden-file regression test for network artifact schema v1.

Sprint 1's network observability layer froze the on-disk format at
schema_version=1. This test re-runs the export against a known event set
and compares byte-for-byte (modulo `generated_at`) against the checked-in
golden files at tests/unit/fixtures/golden/.

If you see this test fail, it means the network schema changed. Two options:

  1. The change was unintentional — revert the source change.
  2. The change was intentional — bump SCHEMA_VERSION in network_monitor.py
     AND regenerate the golden fixtures:
         python -c "
         from tests.unit.test_network_schema import _build_synthetic_events
         from utils.network_monitor import NetworkExporter
         from pathlib import Path
         NetworkExporter.export_to(
             Path('tests/unit/fixtures/golden'),
             _build_synthetic_events(),
         )
         "
     Then strip `generated_at` from both files (see the generator script
     used to create the original fixtures).

Schema changes require an explicit version bump so downstream consumers
(dashboard renderers, fingerprinting, future HAR exporters) can detect
incompatibility instead of silently misreading the new format.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from utils.network_monitor import NetworkEvent, NetworkExporter, SCHEMA_VERSION


GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"


def _build_synthetic_events() -> list[NetworkEvent]:
    """The canonical event set for the golden fixture.

    Covers:
      - Failed fetch with response body (request_body + response_body)
      - Successful fetch (status, no body)
      - Sensitive-URL fetch (body redacted, headers stripped)
      - Image 404 (metadata-only resource type)
      - Request that never connected (failure_reason set, no status)
    """
    return [
        NetworkEvent(
            sequence_id=1, timestamp=1000.0, method="POST",
            url="https://api.example.com/build",
            resource_type="fetch", status=500, duration_ms=2500.0,
            request_headers={"Content-Type": "application/json",
                             "Authorization": "***REDACTED***"},
            response_headers={"Content-Type": "application/json"},
            request_body='{"prompt":"hi"}',
            response_body='{"error":"rate_limit_exceeded","retry_after":60}',
        ),
        NetworkEvent(
            sequence_id=2, timestamp=1001.0, method="GET",
            url="https://api.example.com/status",
            resource_type="fetch", status=200, duration_ms=80.0,
            request_headers={"Accept": "application/json"},
            response_headers={"Content-Type": "application/json"},
        ),
        NetworkEvent(
            sequence_id=3, timestamp=1002.0, method="POST",
            url="https://accounts.example.com/login",
            resource_type="fetch", status=401, duration_ms=120.0,
            request_body="<redacted: sensitive URL>",
            response_body="<redacted: sensitive URL>",
        ),
        NetworkEvent(
            sequence_id=4, timestamp=1003.0, method="GET",
            url="https://cdn.example.com/missing.png",
            resource_type="image", status=404, duration_ms=30.0,
        ),
        NetworkEvent(
            sequence_id=5, timestamp=1004.0, method="GET",
            url="https://api.example.com/never-connects",
            resource_type="fetch", failure_reason="net::ERR_CONNECTION_REFUSED",
            duration_ms=15.0,
        ),
    ]


def _strip_volatile(obj: dict) -> dict:
    """Remove fields that intentionally vary between runs (timestamps that
    aren't part of the schema contract)."""
    obj.pop("generated_at", None)
    return obj


def test_schema_version_pinned():
    """The constant must be exactly 1. Any schema change must be explicit."""
    assert SCHEMA_VERSION == 1, (
        f"Schema version changed to {SCHEMA_VERSION}. If intentional, "
        "regenerate tests/unit/fixtures/golden/ — see the docstring at "
        "the top of this file."
    )


def test_export_matches_golden_summary(tmp_path: Path):
    NetworkExporter.export_to(tmp_path, _build_synthetic_events())
    fresh = _strip_volatile(json.loads((tmp_path / "network-summary.json").read_text()))
    golden = _strip_volatile(json.loads((GOLDEN_DIR / "network-summary.json").read_text()))
    assert fresh == golden, (
        "network-summary.json no longer matches the golden fixture. See the "
        "docstring at the top of this file for how to handle this."
    )


def test_export_matches_golden_events(tmp_path: Path):
    NetworkExporter.export_to(tmp_path, _build_synthetic_events())
    fresh = _strip_volatile(json.loads((tmp_path / "network-events.json").read_text()))
    golden = _strip_volatile(json.loads((GOLDEN_DIR / "network-events.json").read_text()))
    assert fresh == golden, (
        "network-events.json no longer matches the golden fixture. See the "
        "docstring at the top of this file for how to handle this."
    )


def test_event_ids_are_unique_and_sorted_on_export(tmp_path: Path):
    """Independent of the golden file — verifies the export contract: events
    appear in sequence_id order, no duplicates."""
    NetworkExporter.export_to(tmp_path, _build_synthetic_events())
    events = json.loads((tmp_path / "network-events.json").read_text())["events"]
    ids = [e["sequence_id"] for e in events]
    assert ids == sorted(ids), "events must be sorted by sequence_id"
    assert len(ids) == len(set(ids)), "sequence_id must be unique"


def test_terminal_states_are_mutually_exclusive(tmp_path: Path):
    """Each event must have either status or failure_reason, never both,
    never neither. Lifecycle invariant — Sprint-2 fingerprinting will rely
    on this."""
    NetworkExporter.export_to(tmp_path, _build_synthetic_events())
    events = json.loads((tmp_path / "network-events.json").read_text())["events"]
    for e in events:
        has_status = e.get("status") is not None
        has_failure = e.get("failure_reason") is not None
        assert has_status != has_failure, (
            f"event {e['sequence_id']} has both/neither terminal state "
            f"(status={e.get('status')}, failure_reason={e.get('failure_reason')})"
        )
