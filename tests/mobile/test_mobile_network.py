"""
tests/mobile/test_mobile_network.py

Phase 5: does the marketing site remain usable on a slow mobile connection?

Throttling uses CDP Network.emulateNetworkConditions with the standard
Fast 3G profile (the same one Chrome DevTools ships): 1.6 Mbps down,
750 Kbps up, 150 ms RTT. That is CHROMIUM-ONLY — WebKit has no equivalent
API, so on WebKit the test skips with an explicit reason rather than running
unthrottled and calling it a network test.

What "usable" means here, in observable terms:
  * domcontentloaded within the budget,
  * the fixed nav and the hero prompt actually render,
  * sections below the fold gain real height when scrolled to
    (a lazy-loader that never fires on a slow connection is exactly the
    defect class this exists to catch),
  * load time is recorded as an info finding so regressions are visible on
    the dashboard even while they stay under the budget.
"""

from __future__ import annotations

import logging
import time

import pytest

from tests.mobile import flozic_marketing_flows as F
from utils.mobile_interaction_auditor import MobileInteractionAuditor

logger = logging.getLogger(__name__)

# Chrome DevTools' Fast 3G preset.
FAST_3G = {
    "offline": False,
    "latency": 150,                             # ms RTT
    "downloadThroughput": int(1.6 * 1024 * 1024 / 8),   # 1.6 Mbps
    "uploadThroughput": int(750 * 1024 / 8),            # 750 Kbps
    "connectionType": "cellular3g",
}
# Generous on purpose: this is a "does it work at all" gate, not a perf
# budget. Tightening it into a performance SLO is a product decision.
LOAD_BUDGET_MS = 60_000


@pytest.mark.mobile
def test_network_fast_3g_homepage_stays_usable(mobile_page, mobile_report_collector):
    page, device = mobile_page
    a = MobileInteractionAuditor(page, page_name="homepage", device_name=device)

    if a.engine != "chromium":
        a._add("network", "info",
               f"Network throttling is UNSUPPORTED on {a.engine} ({device}): "
               f"it needs CDP Network.emulateNetworkConditions. Not "
               f"substituted with an unthrottled load — slow-network "
               f"behaviour is UNTESTED on this engine.")
        mobile_report_collector.record(a.findings)
        pytest.skip(f"CDP network throttling unavailable on {a.engine}")

    cdp = page.context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", FAST_3G)
    try:
        t0 = time.monotonic()
        page.goto(F.url("homepage"), wait_until="domcontentloaded",
                  timeout=LOAD_BUDGET_MS)
        load_ms = int((time.monotonic() - t0) * 1000)
        page.wait_for_timeout(1_500)

        assert page.locator(F.TOP_NAV).first.is_visible(), (
            f"Fixed nav never rendered on Fast 3G on {device} "
            f"(loaded in {load_ms}ms)."
        )
        hero = page.locator(F.HERO_TEXTAREA).first
        if not hero.count():
            a._add("network", "minor",
                   f"Hero prompt (#hero-prompt) absent after a Fast 3G load on "
                   f"{device} ({load_ms}ms) — the primary CTA does not survive "
                   f"a slow connection.",
                   details={"load_ms": load_ms})

        # Below-the-fold sections must still initialise on a slow pipe.
        vp_h = (page.viewport_size or {"height": 800})["height"]
        for _ in range(6):
            before = page.evaluate("() => window.scrollY")
            a._traverse_scroll(dy=vp_h)
            if page.evaluate("() => window.scrollY") - before < 8:
                break
        empty = page.evaluate("""
        () => [...document.querySelectorAll('section')]
          .filter(s => s.getBoundingClientRect().height < 8).length
        """)
        total = page.locator("section").count()
        if total and empty > total * 0.3:
            a._add("network", "major",
                   f"{empty} of {total} sections never rendered after a Fast "
                   f"3G load on {device} — lazy-loaded content is not "
                   f"surviving a slow connection.",
                   details={"empty": empty, "total": total, "load_ms": load_ms})
        a._add("network", "info",
               f"Homepage on Fast 3G ({device}): domcontentloaded in "
               f"{load_ms}ms (budget {LOAD_BUDGET_MS}ms), {total - empty}/"
               f"{total} sections rendered after scrolling.",
               details={"load_ms": load_ms, "sections": total, "empty": empty})
    finally:
        # Never leak throttling into whatever shares this context afterwards.
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 0,
            "downloadThroughput": -1, "uploadThroughput": -1,
        })
    mobile_report_collector.record(a.findings)
    majors = [f for f in a.findings if f.severity in ("blocker", "major")]
    assert not majors, "\n".join(f.message for f in majors)
