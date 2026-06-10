"""
Python ports of testing.LoginTest and testing.SignupTest.

Both tests are minimal entry-point checks: navigate to the marketing
homepage, click the login (or signup) link, verify the page transitions
to a recognised auth state.

Java sources:
  - src/test/java/testing/LoginTest.java
  - src/test/java/testing/SignupTest.java

The Java versions extend BaseTest which navigates to a default starting
page. We mirror that explicitly by going to the marketing homepage in
the test setup.
"""

from __future__ import annotations

import logging

import pytest
from playwright.sync_api import Page

from pages.auth_state import is_in_valid_state, wait_for_auth_transition
from utils.config import MARKETING_BASE
from utils.test_category import test_category

logger = logging.getLogger(__name__)


@test_category(
    type="SANITY",
    requires_login=False,
    feature="Authentication",
)
class TestLoginEntry:
    """Verifies the homepage 'log in' link reaches an auth flow."""

    @pytest.fixture(autouse=True)
    def open_homepage(self, page: Page) -> None:
        self._page = page
        page.goto(MARKETING_BASE + "/")
        page.wait_for_load_state("domcontentloaded")

    def test_login_flow_starts_correctly(self) -> None:
        # Mirror Java: a[title='log in']
        self._page.locator("a[title='log in']").first.click()
        wait_for_auth_transition(self._page, timeout_ms=30_000)
        assert is_in_valid_state(self._page), (
            f"Login did not reach a valid auth state. URL={self._page.url}"
        )


@test_category(
    type="SANITY",
    requires_login=False,
    feature="Authentication",
)
class TestSignupEntry:
    """Verifies the homepage register/signup link reaches an auth flow."""

    SIGNUP_LINK = "a[href*='register'], a[href*='signup'], a.btn-signup"

    @pytest.fixture(autouse=True)
    def open_homepage(self, page: Page) -> None:
        self._page = page
        page.goto(MARKETING_BASE + "/")
        page.wait_for_load_state("domcontentloaded")

    def test_signup_flow_starts_correctly(self) -> None:
        self._page.locator(self.SIGNUP_LINK).first.click()
        wait_for_auth_transition(self._page, timeout_ms=30_000)
        assert is_in_valid_state(self._page), (
            f"Signup did not reach a valid auth state. URL={self._page.url}"
        )
