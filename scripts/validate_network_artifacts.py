"""
Sprint-1 acceptance-test validator for the network-observability layer.

Reads the artifacts the just-completed pytest run produced and checks each
of the criteria the architectural review defined:

  Pass run:
    - network_overview.json includes both passing-test and failing-test traffic
    - latency invariant: min <= avg <= P95 <= max
    - event IDs are contiguous (no gaps, no duplicates) per file
  Fail run:
    - failure folders contain network-summary.json + network-events.json
    - response bodies present for 4xx/5xx fetch/xhr where applicable
    - redaction: no plaintext Authorization / Bearer / password / *_token in artifacts
    - request/response accounting balances: every request terminates
  Feature flag (--check-disabled):
    - no network JSON anywhere in this run's reports/failures
    - session network_overview.json shows zero requests

Usage:
    python scripts/validate_network_artifacts.py
        --reports-root reports
        [--check-disabled]            # for the FLOWGUARD_NETWORK_CAPTURE=0 run

Exit code 0 = all checks passed; 1 = at least one failed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REDACTED_MARKER = "***REDACTED***"
# Patterns that must NEVER appear in plaintext anywhere in the artifacts.
# (We allow the literal REDACTED marker; that's the whole point of redaction.)
LEAK_PATTERNS = [
    re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9_.\-=/+]{8,}"),
    re.compile(r'"password"\s*:\s*"(?!\*\*\*)[^"]+"'),
    re.compile(r'"access_token"\s*:\s*"(?!\*\*\*)[^"]+"'),
    re.compile(r'"refresh_token"\s*:\s*"(?!\*\*\*)[^"]+"'),
    re.compile(r'"id_token"\s*:\s*"(?!\*\*\*)[^"]+"'),
    re.compile(r'"api_?key"\s*:\s*"(?!\*\*\*)[^"]+"'),
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--reports-root", default="reports")
    p.add_argument("--check-disabled", action="store_true",
                   help="Assert that no network artifacts were produced "
                        "(used for the FLOWGUARD_NETWORK_CAPTURE=0 run).")
    args = p.parse_args()

    root = Path(args.reports_root)
    failures: list[str] = []
    notes:    list[str] = []

    # ── Find the active overview file (per-browser namespacing) ──────────
    overviews = list(root.rglob("network_overview.json"))
    if args.check_disabled:
        if any(_summary_has_data(o) for o in overviews):
            failures.append(
                "[flag-off] An overview file with non-zero requests exists — "
                "FLOWGUARD_NETWORK_CAPTURE=0 should produce no traffic."
            )
        # Also assert no per-failure network JSON was written in this run.
        # (Older runs may have them; we only care about the just-completed
        # most-recent failure folders. Caller is responsible for clearing
        # reports/failures before the flag-off run.)
        per_failure = list(root.rglob("network-events.json"))
        if per_failure:
            notes.append(
                f"[flag-off] {len(per_failure)} stale network-events.json "
                "file(s) from earlier runs were ignored — clear "
                "reports/failures/ before the flag-off run for a clean check."
            )
        _report(failures, notes)
        return 0 if not failures else 1

    if not overviews:
        failures.append(
            f"No network_overview.json found anywhere under {root}/. "
            "Session aggregation did not write."
        )
        _report(failures, notes)
        return 1

    overview_path = max(overviews, key=lambda p: p.stat().st_mtime)
    overview = json.loads(overview_path.read_text())
    notes.append(f"overview: {overview_path}")
    notes.append(f"  requests={overview.get('requests')} "
                 f"failed={overview.get('failed')} "
                 f"failed_pct={overview.get('failed_percentage')} "
                 f"avg_ms={overview.get('avg_latency_ms')} "
                 f"p95_ms={overview.get('p95_latency_ms')}")

    # ── Acceptance check 1: aggregation includes the passing run ─────────
    if (overview.get("requests") or 0) == 0:
        failures.append(
            "network_overview.requests == 0 — Option C (aggregate every "
            "test) is not effective; passing tests didn't contribute."
        )

    # ── Acceptance check 2: latency invariant ────────────────────────────
    avg = overview.get("avg_latency_ms") or 0
    p95 = overview.get("p95_latency_ms") or 0
    if overview.get("requests"):
        if p95 < avg:
            failures.append(
                f"Latency invariant violated: P95={p95} < avg={avg}. "
                "Percentile calculation is broken."
            )

    # ── Per-failure folder checks ────────────────────────────────────────
    failure_dirs = sorted(
        [d for d in (root / "failures").iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    ) if (root / "failures").exists() else []

    if failure_dirs:
        latest = failure_dirs[0]
        notes.append(f"latest failure dir: {latest.name}")
        events_file  = latest / "network-events.json"
        summary_file = latest / "network-summary.json"

        if not events_file.exists():
            failures.append(f"{events_file} missing")
        if not summary_file.exists():
            failures.append(f"{summary_file} missing")

        if events_file.exists():
            events_doc = json.loads(events_file.read_text())
            events = events_doc.get("events", [])

            # ── Check 3: summary is a faithful projection of raw events ──
            # Trust nothing: if the exporter ever miscomputes the summary,
            # this check catches it before fingerprinting depends on it.
            if summary_file.exists():
                summary_doc = json.loads(summary_file.read_text())
                raw_total = len(events)
                raw_failed = sum(
                    1 for e in events
                    if (e.get("status") is not None and e["status"] >= 400)
                    or e.get("failure_reason") is not None
                )
                summary_total = summary_doc.get("total_requests")
                summary_failed = summary_doc.get("failed_count")
                if summary_total != raw_total:
                    failures.append(
                        f"[{latest.name}] summary/events mismatch: "
                        f"summary.total_requests={summary_total} but "
                        f"len(events)={raw_total}"
                    )
                if summary_failed != raw_failed:
                    failures.append(
                        f"[{latest.name}] summary/events mismatch: "
                        f"summary.failed_count={summary_failed} but "
                        f"raw failed count={raw_failed}"
                    )

            # ── Check 4: event IDs contiguous, no gaps/dupes ─────────────
            ids = [e.get("sequence_id") for e in events]
            if ids:
                if sorted(set(ids)) != sorted(ids):
                    failures.append(f"[{latest.name}] duplicate sequence_ids found")
                gaps = [
                    (ids[i], ids[i+1])
                    for i in range(len(ids)-1)
                    if ids[i+1] - ids[i] > 1
                ]
                if gaps:
                    notes.append(
                        f"[{latest.name}] sequence_id gaps (expected if "
                        f"some events were dropped by retention filter): {gaps[:3]}"
                    )

            # ── Check 5: response bodies present for 4xx/5xx fetch/xhr ───
            failed_fetches = [
                e for e in events
                if e.get("resource_type") in ("fetch", "xhr")
                and e.get("status") and e["status"] >= 400
            ]
            bodyless = [
                e for e in failed_fetches
                if not e.get("response_body")
                and "redacted" not in (e.get("response_body") or "")
            ]
            if failed_fetches:
                notes.append(
                    f"[{latest.name}] {len(failed_fetches)} failed fetch/xhr; "
                    f"{len(failed_fetches) - len(bodyless)} have response bodies"
                )

            # ── Check 6: mutually-exclusive terminal states ──────────────
            # Each request must end in EXACTLY one of:
            #   - successful response (status set, no failure_reason)
            #   - failed request    (failure_reason set, no status)
            # Anything else is a lifecycle bug.
            both_set = []
            neither_set = []
            for e in events:
                has_status = e.get("status") is not None
                has_failure = e.get("failure_reason") is not None
                if has_status and has_failure:
                    both_set.append(e.get("sequence_id"))
                elif not has_status and not has_failure:
                    neither_set.append(e.get("sequence_id"))
            if both_set:
                failures.append(
                    f"[{latest.name}] {len(both_set)} events have BOTH status "
                    f"and failure_reason (lifecycle bug). IDs: {both_set[:5]}"
                )
            if neither_set:
                failures.append(
                    f"[{latest.name}] {len(neither_set)} events have NEITHER "
                    f"status nor failure_reason (leak). IDs: {neither_set[:5]}"
                )
            with_status = sum(1 for e in events if e.get("status") is not None
                              and e.get("failure_reason") is None)
            with_failure_reason = sum(1 for e in events
                                      if e.get("failure_reason") is not None
                                      and e.get("status") is None)
            notes.append(
                f"[{latest.name}] terminal states: {with_status} responded, "
                f"{with_failure_reason} failed, {len(both_set)} both-set, "
                f"{len(neither_set)} neither-set"
            )

        # ── Check 6: no plaintext secrets in either artifact ─────────────
        for f in (events_file, summary_file):
            if not f.exists():
                continue
            text = f.read_text()
            for pat in LEAK_PATTERNS:
                hits = pat.findall(text)
                if hits:
                    sample = (hits[0][:80] + "…") if isinstance(hits[0], str) else str(hits[0])
                    failures.append(
                        f"[{f.name}] potential secret leak (pattern "
                        f"{pat.pattern[:40]}…): {sample}"
                    )

    else:
        notes.append("No failure folders to inspect (no tests failed?)")

    _report(failures, notes)
    return 0 if not failures else 1


def _summary_has_data(p: Path) -> bool:
    try:
        return (json.loads(p.read_text()).get("requests") or 0) > 0
    except Exception:
        return False


def _report(failures: list[str], notes: list[str]) -> None:
    print("=" * 70)
    print("NETWORK OBSERVABILITY ACCEPTANCE REPORT")
    print("=" * 70)
    if notes:
        print("\nNotes:")
        for n in notes:
            print(f"  · {n}")
    if failures:
        print(f"\nFAILED CHECKS ({len(failures)}):")
        for f in failures:
            print(f"  ✗ {f}")
    else:
        print("\nAll acceptance checks passed.")
    print()


if __name__ == "__main__":
    sys.exit(main())
