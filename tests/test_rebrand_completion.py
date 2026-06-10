"""
Python port of testing.RebrandCompletionTest.

Verifies the appypieautomate.ai → flozic.ai rebrand is complete by
checking that:
  1. The legacy domain 301-redirects to the current marketing host
  2. Direct navigation to the current host stays on it
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page

from utils.config import (
    MARKETING_BASE,
    is_current_marketing_host,
    marketing_hostname,
)
from utils.test_category import test_category

logger = logging.getLogger(__name__)


@test_category(
    type="REGRESSION",
    requires_login=False,
    feature="Rebrand",
)
class TestRebrandCompletion:
    """Verifies the rebrand redirect chain holds."""

    def test_legacy_domain_redirects_to_current_marketing_host(
        self, page: Page
    ) -> None:
        page.goto("https://www.appypieautomate.ai", wait_until="domcontentloaded", timeout=30_000)
        # Allow up to 15s for any client-side redirect to settle.
        try:
            page.wait_for_url(
                lambda url: is_current_marketing_host(url),
                timeout=15_000,
            )
        except Exception:
            pass  # the assertion below produces the readable message

        final = page.url
        logger.info("Legacy domain final URL: %s", final)
        assert is_current_marketing_host(final), (
            f"Legacy appypieautomate.ai must redirect to {marketing_hostname()}. "
            f"Actual: {final}"
        )

    def test_current_marketing_host_loads_directly(self, page: Page) -> None:
        page.goto(MARKETING_BASE, wait_until="domcontentloaded", timeout=30_000)
        final = page.url
        logger.info("Direct navigation final URL: %s", final)
        assert is_current_marketing_host(final), (
            f"Direct navigation to {MARKETING_BASE} did not stay on "
            f"{marketing_hostname()}. Actual: {final}"
        )
