#!/usr/bin/env python
"""
GPT-powered PR review for test/page-object changes — CLI entry point.

Usage:
    # Review uncommitted changes against the index:
    python scripts/ai_pr_review.py

    # Review the current branch against main (typical PR scenario):
    python scripts/ai_pr_review.py --base main

    # Review a specific commit range:
    python scripts/ai_pr_review.py --base origin/main --head HEAD

    # Feed a pre-computed diff via stdin:
    git diff main...HEAD | python scripts/ai_pr_review.py --stdin

    # Don't fail CI on findings (already the default; explicit for docs):
    python scripts/ai_pr_review.py --advisory

The review never blocks: this script always exits 0 unless --strict is
given AND a blocker-severity finding is present. Output is written to:
    reports/pr_review_<sha>.md       (markdown)
    reports/pr_review_<sha>.json     (machine-readable)

CI integration sketch (GitHub Actions):
    - name: AI PR Review
      env:
        OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      run: python scripts/ai_pr_review.py --base ${{ github.base_ref }}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.ai_pr_review import (  # noqa: E402
    render_markdown, review_diff,
)


def _git(args: list[str]) -> str:
    """Run a git command and return stdout, or empty string on failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, check=False,
        )
        if out.returncode != 0:
            print(f"[ai-pr-review] git {' '.join(args)} -> rc={out.returncode}: "
                  f"{out.stderr.strip()[:200]}", file=sys.stderr)
            return ""
        return out.stdout
    except Exception as e:
        print(f"[ai-pr-review] git command failed: {e}", file=sys.stderr)
        return ""


def _get_diff(args: argparse.Namespace) -> tuple[str, str]:
    """
    Return (diff_text, sha_for_filename). sha is a short identifier used
    only for the output filename; not security-sensitive.
    """
    if args.stdin:
        return sys.stdin.read(), "stdin"
    if args.base:
        head = args.head or "HEAD"
        diff = _git(["diff", f"{args.base}...{head}"])
        sha  = _git(["rev-parse", "--short", head]).strip() or "HEAD"
        return diff, sha
    # Default: uncommitted working-tree changes vs index.
    diff = _git(["diff", "HEAD"])
    sha  = _git(["rev-parse", "--short", "HEAD"]).strip() or "working"
    return diff, sha


def main() -> int:
    ap = argparse.ArgumentParser(
        description="AI-powered PR code review for tests/, pages/, utils/.",
    )
    ap.add_argument("--base", default=None,
                    help="Base ref to diff against, e.g. 'main' or 'origin/main'.")
    ap.add_argument("--head", default=None,
                    help="Head ref (default HEAD).")
    ap.add_argument("--stdin", action="store_true",
                    help="Read the unified diff from stdin instead of running git.")
    ap.add_argument("--strict", action="store_true",
                    help="Exit non-zero when a blocker-severity finding is found.")
    ap.add_argument("--advisory", action="store_true",
                    help="No-op: always advisory (this is the default).")
    args = ap.parse_args()

    diff_text, sha = _get_diff(args)
    if not diff_text.strip():
        print("[ai-pr-review] No diff to review.")
        return 0

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("[ai-pr-review] OPENAI_API_KEY not set; review skipped.")
        return 0

    print(f"[ai-pr-review] Reviewing diff (sha={sha}, "
          f"{len(diff_text):,} bytes)…")
    result = review_diff(diff_text)

    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path   = reports_dir / f"pr_review_{sha}.md"
    json_path = reports_dir / f"pr_review_{sha}.json"

    md = render_markdown(result, sha=sha)
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(result.to_json(), encoding="utf-8")
    print(f"[ai-pr-review] Markdown -> {md_path.relative_to(PROJECT_ROOT)}")
    print(f"[ai-pr-review] JSON     -> {json_path.relative_to(PROJECT_ROOT)}")
    print()

    # Short stdout summary. Avoid printing the full markdown to avoid
    # UnicodeEncodeError on Windows consoles that default to cp1252.
    by_sev: dict[str, int] = {}
    for f in result.findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    print(f"Files reviewed: {result.files_reviewed}")
    print(f"Findings:       {len(result.findings)}"
          + (f"   ({', '.join(f'{k}={v}' for k, v in by_sev.items())})"
             if by_sev else ""))
    if result.findings:
        print(f"Open the markdown report for details:")
        print(f"  {md_path}")

    if args.strict and any(f.severity == "blocker" for f in result.findings):
        print("[ai-pr-review] --strict and blocker present -> exit 1.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
