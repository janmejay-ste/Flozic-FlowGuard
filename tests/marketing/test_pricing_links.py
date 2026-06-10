"""
Python port of testing.marketing.integrity.PricingLinksTest (simplified).
Same shape as HomepageLinksTest.
"""

from __future__ import annotations

import logging

import pytest
import requests
from playwright.sync_api import Page

from pages.marketing.pricing_page import PricingPage
from utils.test_category import test_category

logger = logging.getLogger(__name__)

MAX_HIGH_SEVERITY_FINDINGS = 5


@test_category(
    type="REGRESSION",
    requires_login=False,
    feature="Marketing > Pricing",
)
class TestPricingLinks:
    @pytest.fixture(autouse=True)
    def open_pricing(self, page: Page) -> None:
        self._page = page
        PricingPage(page).navigate()
        page.wait_for_timeout(2_000)

    def test_pricing_internal_links_reachable(self) -> None:
        hrefs = self._page.eval_on_selector_all(
            "a[href]",
            "els => Array.from(new Set(els.map(e => e.href).filter(h => "
            "  h && !h.startsWith('javascript:') && !h.startsWith('mailto:') "
            "  && !h.startsWith('tel:') && !h.startsWith('#'))))"
        )
        internal = [
            h for h in hrefs
            if "flozic.ai" in h.lower() or "appypieautomate.ai" in h.lower()
        ]
        sample = internal[:20]
        broken: list[tuple[str, int]] = []
        for url in sample:
            try:
                r = requests.head(url, timeout=10, allow_redirects=True)
                if r.status_code >= 400:
                    broken.append((url, r.status_code))
            except Exception:
                pass
        logger.info("Pricing link scan: %d total, %d internal sampled, %d broken",
                    len(hrefs), len(sample), len(broken))
        assert len(broken) <= MAX_HIGH_SEVERITY_FINDINGS, (
            f"Pricing produced {len(broken)} broken-link findings "
            f"(tolerance {MAX_HIGH_SEVERITY_FINDINGS}): {broken}"
        )
