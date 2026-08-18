"""
tests/mobile/test_mobile_auth_layout.py

Login and signup get their own test file because they carry a check the
marketing pages don't need: `above_fold_targets`. A signup form that
overflows is bad; a signup form whose SUBMIT BUTTON requires scrolling
past the on-screen keyboard to reach is a conversion-killing bug that's
easy to miss testing only on desktop.
"""
import logging

import pytest

from utils.mobile_layout_auditor import MobileLayoutAuditor
from tests.mobile.mobile_targets import AUTH_PAGES, open_auth_page

logger = logging.getLogger(__name__)


@pytest.mark.mobile
@pytest.mark.parametrize("page_key,target", list(AUTH_PAGES.items()))
def test_auth_page_mobile_layout(mobile_page, mobile_report_collector, page_key, target):
    entry_url, follow_link, above_fold_targets = target
    page, device_name = mobile_page
    # Reached via the live OAuth redirect rather than a hardcoded URL — see the
    # module docstring in mobile_targets.py for why that matters here.
    final_url = open_auth_page(page, entry_url, follow_link)
    logger.info("[mobile] %s on %s -> %s", page_key, device_name, final_url)

    auditor = MobileLayoutAuditor(page, page_name=page_key, device_name=device_name)
    findings = auditor.run_all(above_fold_targets=above_fold_targets)
    mobile_report_collector.record(findings)

    blockers = [f for f in findings if f.severity == "blocker"]
    assert not blockers, "\n".join(f.message for f in blockers)