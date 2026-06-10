"""
Python port of testing.ExploreMenuIntegrationTest.
Iterates 'Popular App Integrations' Explore-menu section.
"""

from __future__ import annotations

from playwright.sync_api import Page

from pages.explore_menu_helper import run_section
from utils.test_category import test_category


@test_category(
    type="REGRESSION",
    requires_login=True,
    feature="Popular App Integrations",
)
class TestExploreMenuIntegration:
    def test_popular_app_integrations(self, page: Page) -> None:
        run_section(page, "Popular App Integrations")
