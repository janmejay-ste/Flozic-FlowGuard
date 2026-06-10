"""
Ad-hoc probe (NOT a real test): for each of the 10 flozic.ai app pages,
type a dummy prompt, click the 'Build my workflow' button, and capture
where the browser is redirected. Outputs a table classifying each app
as login | signup | other.

Run with:
    python -m pytest tests/_flozic_redirect_probe.py::test_probe_redirects --headed -s
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from playwright.sync_api import Page

from pages.flozic_landing_page import FlozicLandingPage

logger = logging.getLogger(__name__)

PROMPTS_FILE = Path(__file__).with_name("flozic_prompts.json")


def _classify(url: str) -> str:
    low = url.lower()
    if "/signup" in low or "register" in low:
        return "SIGNUP"
    if "/login" in low or "accounts.appypie" in low:
        return "LOGIN"
    return "OTHER"


def test_probe_redirects(page: Page) -> None:
    cfg = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    results: list[tuple[str, str, str]] = []  # (app_key, classification, url)

    for app_key, entry in cfg.items():
        if app_key.startswith("_"):
            continue
        slug = entry["url_slug"]
        prompt = entry["prompt"]
        landing = FlozicLandingPage(page)
        try:
            landing.open_app(slug)
            landing.submit_prompt(prompt)
            # Wait briefly for the redirect to settle.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(2_000)
            url = page.url
            classification = _classify(url)
            results.append((app_key, classification, url))
            logger.info("[PROBE] %-18s -> %-6s  %s", app_key, classification, url)
        except Exception as e:
            results.append((app_key, "ERROR", f"{type(e).__name__}: {str(e).splitlines()[0]}"))
            logger.warning("[PROBE] %-18s -> ERROR  %s", app_key, e)

    # Final table.
    print("\n\n=== FLOZIC REDIRECT PROBE RESULTS ===")
    print(f"{'app':<18} {'destination':<8} url")
    print("-" * 100)
    for app_key, classification, url in results:
        print(f"{app_key:<18} {classification:<8} {url[:80]}")
    print("=" * 100)

    signup_apps = [a for a, c, _ in results if c == "SIGNUP"]
    login_apps  = [a for a, c, _ in results if c == "LOGIN"]
    other_apps  = [a for a, c, _ in results if c not in ("SIGNUP", "LOGIN")]
    print(f"SIGNUP ({len(signup_apps)}): {', '.join(signup_apps) or '(none)'}")
    print(f"LOGIN  ({len(login_apps)}):  {', '.join(login_apps) or '(none)'}")
    print(f"OTHER  ({len(other_apps)}):  {', '.join(other_apps) or '(none)'}")
