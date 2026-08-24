"""
tests/mobile/test_mobile_interactions.py

Mobile FUNCTIONAL tests — does the page work when touched, not just render.

Separate from test_mobile_marketing_layout.py on purpose. Those tests assert
on geometry and would all pass on a page whose buttons do nothing and which
cannot be scrolled. These drive real touch gestures (CDP
Input.dispatchTouchEvent / synthesizeScrollGesture) and require an observable
effect from each one.

Severity contract, matching the layout suite:
  blocker -> fails the test. Reserved for "a user cannot get at the content":
             a page that will not scroll, a menu that will not open.
  major   -> recorded, does not fail. Real defects, but the page is usable.
  minor   -> recorded.
  info    -> recorded. "This check did not apply here" (no carousel, no chat
             widget) or "this check crashed and verified nothing". Kept in the
             report so absent coverage is never mistaken for passing coverage.
"""

import logging

import pytest

from tests.mobile.mobile_targets import AUTH_PAGES, MARKETING_PAGES, open_auth_page
from utils.mobile_interaction_auditor import MobileInteractionAuditor

logger = logging.getLogger(__name__)


def _report(findings, page_key, device_name):
    """Log a per-run digest so a CI log is useful without opening the report."""
    counts = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    logger.info(
        "[mobile-fn] %s on %s: %s",
        page_key, device_name,
        ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no findings",
    )
    for f in findings:
        if f.severity in ("blocker", "major"):
            logger.warning("[mobile-fn] %s | %s", f.category, f.message)


@pytest.mark.mobile
@pytest.mark.parametrize("page_key,url", list(MARKETING_PAGES.items()))
def test_marketing_page_mobile_interactions(
    mobile_page, mobile_report_collector, page_key, url,
):
    page, device_name = mobile_page
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)

    auditor = MobileInteractionAuditor(page, page_name=page_key, device_name=device_name)
    findings = auditor.run_all()
    mobile_report_collector.record(findings)
    _report(findings, page_key, device_name)

    blockers = [f for f in findings if f.severity == "blocker"]
    assert not blockers, "\n".join(f.message for f in blockers)


@pytest.mark.mobile
@pytest.mark.parametrize("page_key,target", list(AUTH_PAGES.items()))
def test_auth_page_mobile_interactions(
    mobile_page, mobile_report_collector, page_key, target,
):
    """Auth pages get a narrowed check set.

    `tap` is excluded because following a nav link off the Cognito Hosted UI
    abandons the OAuth flow, and `swipe`/`widget` because neither exists on a
    login form — running them would only add info findings for every device.
    Scroll, sticky, form and orientation are the ones that matter here: a
    login form you cannot scroll to the submit button is unusable.
    """
    entry_url, follow_link, _above_fold = target
    page, device_name = mobile_page
    final_url = open_auth_page(page, entry_url, follow_link)
    logger.info("[mobile-fn] %s on %s -> %s", page_key, device_name, final_url)

    auditor = MobileInteractionAuditor(page, page_name=page_key, device_name=device_name)
    findings = auditor.run_all(
        include={"scroll", "h_pan", "sticky", "form", "orientation", "input_zoom"})
    mobile_report_collector.record(findings)
    _report(findings, page_key, device_name)

    blockers = [f for f in findings if f.severity == "blocker"]
    assert not blockers, "\n".join(f.message for f in blockers)


@pytest.mark.mobile
@pytest.mark.parametrize("page_key,url", list(MARKETING_PAGES.items()))
def test_marketing_page_survives_viewport_changes(
    mobile_page, mobile_report_collector, page_key, url,
):
    """Resize across breakpoints WITHOUT reloading.

    A reload lets the page re-run its layout from scratch, which hides the bug
    this test is for: a responsive layout that only settles correctly on first
    paint and does not reflow when the viewport actually changes — which is
    what happens on rotation, on a split-screen resize, or when the URL bar
    collapses.
    """
    page, device_name = mobile_page
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)

    widths = [320, 390, 430, 768]
    problems = []
    for w in widths:
        page.set_viewport_size({"width": w, "height": 800})
        page.wait_for_timeout(500)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth"
            " - document.documentElement.clientWidth"
        )
        if overflow > 2:
            problems.append((w, overflow))

    auditor = MobileInteractionAuditor(page, page_name=page_key, device_name=device_name)
    for w, overflow in problems:
        auditor._add(
            "reflow", "major",
            f"Resizing to {w}px wide without a reload leaves {overflow}px of "
            f"horizontal overflow on {page_key} — the layout does not reflow "
            f"on a live viewport change.",
            details={"width": w, "overflow_px": overflow},
        )
    if not problems:
        auditor._add(
            "reflow", "info",
            f"{page_key} reflowed cleanly at {', '.join(str(w) for w in widths)}px "
            f"without a reload.",
        )
    mobile_report_collector.record(auditor.findings)
    _report(auditor.findings, page_key, device_name)
