"""
Python port of testing.marketing.seo.PricingSeoTest.

Same SEO assertions as homepage but on the pricing page.
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.marketing.pricing_page import PricingPage
from utils.config import is_owned_marketing_host
from utils.test_category import test_category

logger = logging.getLogger(__name__)


def _safe_attr(page: Page, selector: str, attr: str) -> str | None:
    loc = page.locator(selector).first
    if loc.count() == 0:
        return None
    try:
        return loc.get_attribute(attr)
    except Exception:
        return None


@test_category(
    type="REGRESSION",
    requires_login=False,
    feature="Marketing > Pricing",
)
class TestPricingSeo:
    """SEO metadata coverage for the flozic.ai pricing page."""

    @pytest.fixture(autouse=True)
    def open_pricing(self, page: Page) -> PricingPage:
        self._page = page
        return PricingPage(page).navigate()

    def test_canonical_link_points_to_marketing_host(self) -> None:
        href = _safe_attr(self._page, "link[rel='canonical']", "href")
        assert href is not None, "No <link rel='canonical'> on pricing page"
        assert is_owned_marketing_host(href), (
            f"Canonical href is not an owned marketing host. Actual: {href}"
        )

    def test_open_graph_title_is_present(self) -> None:
        og = _safe_attr(self._page, "meta[property='og:title']", "content")
        assert og and og.strip(), "og:title missing or blank"

    def test_open_graph_description_is_present(self) -> None:
        og = _safe_attr(self._page, "meta[property='og:description']", "content")
        assert og and og.strip(), "og:description missing or blank"

    def test_open_graph_image_is_present(self) -> None:
        og = _safe_attr(self._page, "meta[property='og:image']", "content")
        assert og and og.strip(), "og:image missing or blank"

    def test_pricing_page_is_indexable(self) -> None:
        robots = _safe_attr(self._page, "meta[name='robots']", "content")
        if robots is not None:
            assert "noindex" not in robots.lower(), (
                f"Pricing page marked noindex — high-intent search visibility lost. "
                f"Tag: {robots}"
            )
