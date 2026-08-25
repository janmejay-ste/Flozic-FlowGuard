"""
tests/mobile/test_mobile_marketing_layout.py

Audits the marketing pages (homepage, pricing) for mobile-friendliness on
every device in MOBILE_DEVICE_NAMES/TABLET_DEVICE_NAMES, then separately
checks whether the SAME page renders consistently across those devices.

Two different bug classes, two different test functions:
  - test_marketing_page_mobile_layout: is this page broken on THIS device?
  - test_marketing_page_cross_device_consistency: does this page behave
    the SAME way across mobile devices, or does it silently diverge?
"""
import pytest

from utils.mobile_layout_auditor import MobileLayoutAuditor
from utils.mobile_cross_device import capture_snapshot, compare_snapshots
from pages.mobile.mobile_devices import MOBILE_DEVICE_NAMES, ALL_DEVICE_PROFILES
from tests.mobile.mobile_targets import MARKETING_PAGES


@pytest.mark.mobile
@pytest.mark.parametrize("page_key,url", list(MARKETING_PAGES.items()))
def test_marketing_page_mobile_layout(mobile_page, mobile_report_collector, page_key, url):
    page, device_name = mobile_page
    page.goto(url, wait_until="networkidle")

    auditor = MobileLayoutAuditor(page, page_name=page_key, device_name=device_name)
    findings = auditor.run_all()
    mobile_report_collector.record(findings)

    blockers = [f for f in findings if f.severity == "blocker"]
    assert not blockers, "\n".join(f.message for f in blockers)


@pytest.mark.mobile
@pytest.mark.parametrize("page_key,url", list(MARKETING_PAGES.items()))
def test_marketing_page_cross_device_consistency(browser_instance, mobile_report_collector, page_key, url):
    """
    Not parametrized through `mobile_page` — this test needs ALL mobile
    devices open in the same run so it can diff them against each other,
    rather than one device per test invocation.
    """
    snapshots = []
    for device_name in MOBILE_DEVICE_NAMES:
        context = browser_instance.new_context(**ALL_DEVICE_PROFILES[device_name])
        page = context.new_page()
        page.goto(url, wait_until="networkidle")
        snapshots.append(capture_snapshot(page, device_name))
        context.close()

    findings = compare_snapshots(page_key, snapshots)
    mobile_report_collector.record(findings)

    blockers = [f for f in findings if f.severity == "blocker"]
    assert not blockers, "\n".join(f.message for f in blockers)