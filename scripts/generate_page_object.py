#!/usr/bin/env python
"""
Generate a Page Object scaffold from a URL or HTML file.

NEVER overwrites an existing page object. Output always lands as:
    pages/<snake_case>_page.new.py

Promote it to <name>_page.py only after a human review pass — the
generator is explicitly scaffold-only.

Usage:
    # From a live URL (launches headless Chromium)
    python scripts/generate_page_object.py https://www.flozic.ai/

    # From a local HTML file
    python scripts/generate_page_object.py path/to/page.html

    # Override the generated class/file name
    python scripts/generate_page_object.py https://example.com/login --name LoginPage
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.ai_page_object import generate_page_object  # noqa: E402


def _snake(name: str) -> str:
    """CamelCase -> snake_case ('LoginPage' -> 'login_page')."""
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s).strip("_")
    return s or "generated"


def _class_name_from_input(source: str) -> str:
    """Derive a sensible class name from a URL or filename."""
    # URL: take the last meaningful path segment
    if source.startswith("http://") or source.startswith("https://"):
        from urllib.parse import urlparse
        parts = [p for p in urlparse(source).path.split("/") if p]
        stem  = parts[-1] if parts else urlparse(source).hostname or "Generated"
    else:
        stem = Path(source).stem
    # Strip extension-like suffixes, normalize
    stem = re.sub(r"\.(html?|aspx?|php)$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[^A-Za-z0-9]+", " ", stem).strip()
    parts = [p.capitalize() for p in stem.split() if p]
    if not parts:
        parts = ["Generated"]
    cls = "".join(parts)
    if not cls.endswith("Page"):
        cls += "Page"
    return cls


def _fetch_html_from_url(url: str) -> str:
    """Open the URL headless and return outerHTML of the document."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[gen] Playwright not installed; cannot fetch live URLs. "
              "Pass an HTML file instead.", file=sys.stderr)
        sys.exit(2)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx  = browser.new_context()
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # Best-effort networkidle but don't block forever on chatty pages.
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            html = page.content()
            return html
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("source",
                    help="URL (http/https) or path to a local HTML file.")
    ap.add_argument("--name", default=None,
                    help="Class name to use (default: derived from source).")
    ap.add_argument("--out-dir", default=str(PROJECT_ROOT / "pages"),
                    help="Output directory (default: pages/).")
    args = ap.parse_args()

    class_name = args.name or _class_name_from_input(args.source)
    file_stem  = _snake(class_name)
    out_dir    = Path(args.out_dir)
    target     = out_dir / f"{file_stem}.new.py"

    # Hard guarantee: NEVER overwrite an existing reviewed page object,
    # and never silently replace a previous .new.py either.
    if target.exists():
        print(f"[gen] Refusing to overwrite existing scaffold: {target}",
              file=sys.stderr)
        print(f"[gen] Delete or rename it first, or re-run with --name.",
              file=sys.stderr)
        return 1
    promoted = out_dir / f"{file_stem}.py"
    if promoted.exists():
        print(f"[gen] NOTE: a promoted page object already exists at {promoted}.",
              file=sys.stderr)
        print(f"[gen] The new draft will be written to {target.name} so it "
              f"can be diffed against the existing version.", file=sys.stderr)

    # Fetch HTML
    if args.source.startswith("http://") or args.source.startswith("https://"):
        print(f"[gen] Fetching {args.source} (headless Chromium)…")
        html = _fetch_html_from_url(args.source)
    else:
        path = Path(args.source)
        if not path.exists():
            print(f"[gen] HTML file not found: {path}", file=sys.stderr)
            return 1
        html = path.read_text(encoding="utf-8")
    print(f"[gen] HTML loaded: {len(html):,} bytes. Generating {class_name}…")

    result = generate_page_object(
        class_name=class_name,
        dom_html=html,
        source_label=args.source,
    )

    if result.status == "SKIPPED":
        print(f"[gen] Skipped: {result.error}")
        return 0
    if result.status == "ERROR":
        print(f"[gen] Error: {result.error}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(result.code, encoding="utf-8")
    print(f"[gen] Wrote draft -> {target.relative_to(PROJECT_ROOT)}")
    print()
    # Windows consoles default to cp1252 which can't encode emojis. Encode
    # checklist items defensively so a 🚨 in the output never crashes
    # the script — the file write already succeeded by this point.
    def _safe(s: str) -> str:
        try:
            return s.encode(sys.stdout.encoding or "utf-8",
                            errors="replace").decode(
                sys.stdout.encoding or "utf-8", errors="replace")
        except Exception:
            return s.encode("ascii", errors="replace").decode("ascii")
    print("Manual-review checklist (resolve before promoting to .py):")
    if result.review_checklist:
        for item in result.review_checklist:
            print(f"  - {_safe(item)}")
    else:
        print("  - (no checklist returned)")
    print()
    print(f"To promote: review the file, fix any TODOs, then:")
    print(f"  mv {target.relative_to(PROJECT_ROOT)} "
          f"{(out_dir / (file_stem + '.py')).relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
