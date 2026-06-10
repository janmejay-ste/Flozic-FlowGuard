"""
Python port of testing.marketing.seo.HomepageSeoTest.

Pure HTML/meta-tag assertions — verifies the minimum SEO surface a
public marketing page must expose.
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.marketing.home_page import HomePage
from utils.config import is_owned_marketing_host
from utils.test_category import test_category

logger = logging.getLogger(__name__)


def _safe_attr(page: Page, selector: str, attr: str) -> str | None:
    """Locator's get_attribute returns None when not found; treat missing element same way."""
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
    feature="Marketing > Homepage",
)
class TestHomepageSeo:
    """SEO metadata coverage for the flozic.ai marketing homepage."""

    @pytest.fixture(autouse=True)
    def open_homepage(self, page: Page) -> HomePage:
        self._page = page
        return HomePage(page).navigate()

    def test_canonical_link_points_to_marketing_host(self) -> None:
        href = _safe_attr(self._page, "link[rel='canonical']", "href")
        assert href is not None, "No <link rel='canonical'> on homepage"
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

    def test_twitter_card_is_declared(self) -> None:
        card = _safe_attr(self._page, "meta[name='twitter:card']", "content")
        assert card and card.strip(), "twitter:card missing or blank"

    def test_homepage_is_indexable(self) -> None:
        robots = _safe_attr(self._page, "meta[name='robots']", "content")
        # robots tag is optional; absent => default indexable.
        if robots is not None:
            assert "noindex" not in robots.lower(), (
                f"Homepage marked noindex — search visibility lost. Tag: {robots}"
            )

    def test_has_at_least_one_structured_data_block(self) -> None:
        ld = self._page.locator("script[type='application/ld+json']")
        count = ld.count()
        assert count > 0, "No JSON-LD structured-data block found on homepage"

        # Any of the blocks must look like valid JSON
        any_parseable = False
        for i in range(count):
            body = ld.nth(i).inner_text() or ""
            t = body.strip()
            if t.startswith("{") or t.startswith("["):
                any_parseable = True
                break
        assert any_parseable, (
            "JSON-LD blocks are present but none look like valid JSON"
        )
