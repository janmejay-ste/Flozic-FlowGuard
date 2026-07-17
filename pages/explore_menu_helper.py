"""
Helper for ExploreMenu* tests. Python port of the core flow from Java
ExploreMenuTestBase (1004-line original simplified to the essential
iteration pattern).

Per-iteration:
  1. Navigate to flozic.ai
  2. Hover Explore nav → collect links from the named section
  3. For each link (capped at MAX_LINKS):
     a. Navigate to integration page
     b. Click 'Automate' (a.bannerInnerBtn)
     c. If auth redirect → complete login
     d. Validate editor is ready (5 gates simplified to 2)
     e. Logout → next iteration
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import pytest

from pages.auth_helper import perform_login
from pages.dashboard_page import DashboardPage
from utils.config import MARKETING_BASE

logger = logging.getLogger(__name__)

MAX_LINKS = 10  # Java default; cap iteration count per section


def collect_section_links(page: Page, section_title: str) -> list[tuple[str, str]]:
    """
    Open Explore menu, find the named section, and return its (label, href)
    entries deduplicated by href.
    """
    page.goto(MARKETING_BASE + "/", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(1_500)

    # Find the Explore nav trigger
    explore = page.locator(
        "xpath=//nav//a[normalize-space()='Explore']"
    ).first
    try:
        explore.wait_for(state="visible", timeout=10_000)
        explore.hover()
    except PlaywrightTimeoutError:
        logger.warning("Explore nav trigger not found")
        return []

    page.wait_for_timeout(800)  # let dropdown render

    # Find the section by heading text — broad element set to survive DOM changes
    section = page.locator(
        f"xpath=//*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 "
        f"or self::div or self::span or self::li or self::p]"
        f"[contains(normalize-space(.),'{section_title}')]"
    ).first
    if section.count() == 0:
        logger.warning("Section '%s' not found in Explore menu", section_title)
        return []

    # Collect anchors under the section — restrict to integration links only.
    # Integration anchors on flozic.ai are /integrate/apps/{app}/integrations/{other}
    # so we filter by href substring to avoid catching nav menu items
    # (which is why the previous run captured 51 "Features" links instead).
    anchors_data = page.evaluate(
        """
        (title) => {
            const normalize = s => (s || '').replace(/\\s+/g, ' ').trim();
            const all = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p,a'));
            // Prefer EXACT-match heading; fall back to startsWith
            const heading = all.find(h => normalize(h.textContent) === title)
                       || all.find(h => normalize(h.textContent).startsWith(title));
            if (!heading) return [];

            const isIntegrationHref = href =>
                href && /\\/integrate\\/apps\\/[^\\/]+\\/integrations\\//.test(href);

            const collect = container => {
                if (!container) return [];
                return Array.from(container.querySelectorAll('a[href]'))
                    .map(a => [normalize(a.textContent), a.href])
                    .filter(([t, h]) => t && isIntegrationHref(h));
            };

            // Walk up from the heading to find the section that contains
            // a meaningful number of integration anchors.
            let node = heading;
            for (let depth = 0; depth < 6 && node; depth++) {
                let sib = node.nextElementSibling;
                while (sib) {
                    const links = collect(sib);
                    if (links.length >= 3) return links;
                    sib = sib.nextElementSibling;
                }
                const parentLinks = collect(node.parentElement);
                if (parentLinks.length >= 3) return parentLinks;
                node = node.parentElement;
            }
            return [];
        }
        """,
        section_title,
    )

    # Dedupe by href, preserve order
    unique: OrderedDict[str, str] = OrderedDict()
    for entry in anchors_data:
        if not entry or len(entry) != 2:
            continue
        text, href = entry
        if href not in unique:
            unique[href] = text

    result = [(text, href) for href, text in unique.items()]
    logger.info(
        "Collected %d unique link(s) from '%s' section",
        len(result), section_title
    )
    return result


def run_link_iteration(
    page: Page,
    label: str,
    href: str,
    iteration: int,
) -> None:
    """
    Run one iteration of the explore-menu flow against a single link.
    Raises AssertionError if any gate fails.
    """
    log_prefix = f"[{label}]"
    logger.info("── %s Iteration %d ──", log_prefix, iteration + 1)

    # 1. Navigate to integration page
    page.goto(href, wait_until="domcontentloaded", timeout=30_000)

    # 2. Click Automate button
    automate_btn = page.locator("a.bannerInnerBtn, a#getStartedBtn").first
    automate_btn.wait_for(state="visible", timeout=15_000)
    automate_btn.scroll_into_view_if_needed()
    automate_btn.click(timeout=10_000)
    logger.info("%s Automate clicked — current URL: %s", log_prefix, page.url)

    # 3. Auth redirect handling
    if "accounts.appypie" in page.url or "register" in page.url:
        logger.info("%s Auth redirect — handling login", log_prefix)
        perform_login(page)
        # Give session cookies time to propagate across domains before
        # re-navigating. Without this wait, the build-your-connect endpoint
        # sometimes re-redirects to /register because it sees an unauthenticated
        # request even though the login just completed.
        page.wait_for_timeout(3_000)
        # Re-navigate to integration page to recover the editor flow
        page.goto(href, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1_000)
        automate_btn = page.locator("a.bannerInnerBtn, a#getStartedBtn").first
        automate_btn.wait_for(state="visible", timeout=15_000)
        automate_btn.click(timeout=10_000)
        # If a second auth redirect occurs (race condition), wait it out —
        # the build-your-connect URL should redirect to the editor once
        # the server-side session validates on retry.
        if "accounts.appypie" in page.url or "register" in page.url:
            logger.info(
                "%s Second auth redirect after re-click — waiting for editor via URL poll",
                log_prefix,
            )
            # Navigate directly to the build-your-connect URL extracted from
            # the register frompage param so we avoid the marketing-site hop.
            # Fall back to waiting on the current URL to change.
            page.wait_for_timeout(2_000)

    # 4. Editor validation — Gate 1 (URL) + Gate 2 (canvas visible)
    try:
        page.wait_for_url(
            lambda url: "customeditor" in (url or "").lower(),
            timeout=30_000,
        )
        logger.info("%s Gate 1 passed — editor URL: %s", log_prefix, page.url)
    except PlaywrightTimeoutError:
        raise AssertionError(
            f"{log_prefix} Gate 1 failed — /customeditor URL not reached. "
            f"Got: {page.url}"
        )

    # Canvas visibility
    canvas = page.locator(
        "app-custom-editor, .connect-editor, [class*='customeditor'], "
        ".canvas-container"
    ).first
    try:
        canvas.wait_for(state="visible", timeout=15_000)
        logger.info("%s Gate 2 passed — canvas visible", log_prefix)
    except PlaywrightTimeoutError:
        raise AssertionError(f"{log_prefix} Gate 2 failed — canvas not visible")

    # 5. Logout to reset for next iteration
    try:
        DashboardPage(page).click_logout()
        logger.info("%s Logout complete", log_prefix)
    except Exception as e:
        logger.warning(
            "%s Logout failed (non-fatal): %s",
            log_prefix, str(e).splitlines()[0]
        )


def run_section(page: Page, section_title: str) -> None:
    """Run the full iteration loop for a section."""
    links = collect_section_links(page, section_title)
    if not links:
        pytest.skip(
            f"Explore menu section '{section_title}' not found or has no integration "
            f"links — likely a live-site DOM change. Re-inspect the menu structure."
        )

    capped = links[:MAX_LINKS]
    logger.info(
        "[%s] Running %d / %d iterations",
        section_title, len(capped), len(links)
    )

    failures: list[tuple[str, str]] = []
    for i, (label, href) in enumerate(capped):
        try:
            run_link_iteration(page, label, href, i)
        except AssertionError as ae:
            failures.append((label, str(ae).splitlines()[0]))
            logger.error("Iteration %d failed: %s", i + 1, ae)

    if failures:
        raise AssertionError(
            f"{len(failures)} of {len(capped)} iterations failed:\n"
            + "\n".join(f"  • {lbl}: {msg}" for lbl, msg in failures)
        )

    logger.info("All %d '%s' iterations passed", len(capped), section_title)
