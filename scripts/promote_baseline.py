#!/usr/bin/env python
"""
Promote the latest canvas screenshot of a test to its baseline.

Usage:
    python scripts/promote_baseline.py <test_id> [<test_id> ...]
    python scripts/promote_baseline.py --all      # promote every test that
                                                  # currently has FIRST_RUN
    python scripts/promote_baseline.py --list     # show baseline status

The baseline lives at:  baselines/<test_id>/canvas.png   (repo root, in git)
The current capture is: reports/recordings/<test_id>/canvas.png

NEVER promote without eyeballing the screenshot first. The whole point of
baselines is to be a stable source of historical truth — auto-promotion
would defeat that.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the project root importable when this is run as a script.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.ai_visual_diff import (  # noqa: E402  (after sys.path edit)
    BASELINE_ROOT,
    baseline_path_for,
    promote_baseline,
)

RECORDINGS_ROOT = PROJECT_ROOT / "reports" / "recordings"


def _current_png(test_id: str) -> Path:
    return RECORDINGS_ROOT / test_id / "canvas.png"


def cmd_promote(test_ids: list[str]) -> int:
    rc = 0
    for tid in test_ids:
        src = _current_png(tid)
        if not src.exists():
            print(f"[skip] {tid}: no current canvas at {src}")
            rc = max(rc, 1)
            continue
        dest = promote_baseline(tid, src)
        print(f"[ok]   {tid}: {src.relative_to(PROJECT_ROOT)} -> "
              f"{dest.relative_to(PROJECT_ROOT)}")
    return rc


def cmd_list() -> int:
    print(f"{'test_id':<60}  {'baseline':<8}  {'current':<8}")
    print("-" * 80)
    test_ids: set[str] = set()
    if RECORDINGS_ROOT.exists():
        test_ids.update(d.name for d in RECORDINGS_ROOT.iterdir() if d.is_dir())
    if BASELINE_ROOT.exists():
        test_ids.update(d.name for d in BASELINE_ROOT.iterdir() if d.is_dir())
    for tid in sorted(test_ids):
        b = "yes" if baseline_path_for(tid).exists() else "MISSING"
        c = "yes" if _current_png(tid).exists() else "—"
        print(f"{tid:<60}  {b:<8}  {c:<8}")
    return 0


def cmd_promote_all_first_run() -> int:
    """Promote every test that currently has no baseline."""
    if not RECORDINGS_ROOT.exists():
        print(f"No recordings directory at {RECORDINGS_ROOT}", file=sys.stderr)
        return 1
    targets = []
    for d in RECORDINGS_ROOT.iterdir():
        if not d.is_dir():
            continue
        if not _current_png(d.name).exists():
            continue
        if baseline_path_for(d.name).exists():
            continue
        targets.append(d.name)
    if not targets:
        print("Nothing to promote — all recorded tests already have baselines.")
        return 0
    print(f"Promoting {len(targets)} first-run tests:")
    return cmd_promote(targets)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("test_ids", nargs="*", default=[],
                   help="Test IDs to promote (e.g. test_flozic_paypal_connect)")
    g.add_argument("--all", action="store_true",
                   help="Promote every test currently missing a baseline")
    g.add_argument("--list", action="store_true",
                   help="Show baseline status for every known test")
    args = ap.parse_args()

    if args.list:
        return cmd_list()
    if args.all:
        return cmd_promote_all_first_run()
    if not args.test_ids:
        ap.error("Provide at least one test_id, or --all, or --list.")
    return cmd_promote(args.test_ids)


if __name__ == "__main__":
    sys.exit(main())
