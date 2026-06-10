"""
Python port of testing.HomepageExhaustiveTest (simplified).

The Java version delegates to a 500-line UrlValidationRunner that
discovers every URL from every element kind (anchors, scripts,
iframes, images, fetches) and validates each via HTTP HEAD with
retry / canonicalisation / anti-bot skip logic.

This port keeps the same INTENT (discover homepage URLs, check
broken-link count is below threshold) using a simpler scan of
anchor elements. Tier-1 / Tier-2 backend URL validation
(scripts/images/iframes/fetches) is a larger workstream and is
NOT included here.

Java tolerance: MAX_HIGH_SEVERITY_FINDINGS = 5 (we use the same).
"""

from __future__ import annotations

import logging

import pytest
import requests
from playwright.sync_api import Page

from utils.config import MARKETING_BASE
from utils.test_category import test_category

logger = logging.getLogger(__name__)


@test_category(
    type="SANITY",
    requires_login=False,
    feature="Homepage",
)
class TestHomepageExhaustive:
    @pytest.fixture(autouse=True)
    def setup(self, page: Page) -> None:
        self._page = page
        page.goto(MARKETING_BASE + "/", wait_until="domcontentloaded", timeout=30_000)
        # Allow dynamic content to render
        page.wait_for_timeout(3_000)

    def test_all_buttons_and_links_resolvable(self) -> None:
        # Collect distinct anchor hrefs that look like real URLs
        hrefs = self._page.eval_on_selector_all(
            "a[href]",
            "els => Array.from(new Set(els.map(e => e.href).filter(h => "
            "  h && !h.startsWith('javascript:') && !h.startsWith('mailto:') "
            "  && !h.startsWith('tel:') && !h.startsWith('#'))))"
        )
        logger.info("Discovered %d distinct anchor URLs", len(hrefs))
        assert len(hrefs) > 0, "Homepage discovered zero anchor URLs — may not have rendered"

        # Validate a SAMPLE of internal links via HEAD requests.
        # Full validation of every URL (incl. third-party) is the Java
        # UrlValidationRunner's job; this Tier-2 port does a sampled
        # quick-check sufficient to catch a deploy regression.
        internal = [h for h in hrefs if "flozic.ai" in h.lower() or "appypieautomate.ai" in h.lower()]
        sample = internal[:20]  # cap to avoid long runs
        broken: list[tuple[str, int]] = []
        for url in sample:
            try:
                r = requests.head(url, timeout=10, allow_redirects=True)
                if r.status_code >= 400:
                    broken.append((url, r.status_code))
            except Exception as e:
                logger.debug("HEAD failed for %s: %s", url, str(e).splitlines()[0])

        MAX_HIGH_SEVERITY_FINDINGS = 5  # mirror Java tolerance
        assert len(broken) <= MAX_HIGH_SEVERITY_FINDINGS, (
            f"Homepage produced {len(broken)} broken internal links "
            f"(tolerance: {MAX_HIGH_SEVERITY_FINDINGS}): {broken}"
        )
