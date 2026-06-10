"""
Python port of testing.TopAppCombinationsTest.
Iterates 'Top App Combinations' Explore-menu section.
"""

from __future__ import annotations

from playwright.sync_api import Page

from pages.explore_menu_helper import run_section
from utils.test_category import test_category


@test_category(
    type="REGRESSION",
    requires_login=True,
    feature="Top App Combinations",
)
class TestTopAppCombinations:
    def test_top_app_combinations(self, page: Page) -> None:
        run_section(page, "Top App Combinations")
