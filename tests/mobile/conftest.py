"""
tests/mobile/conftest.py

Device-parametrized fixtures for the mobile layout branch — built on top
of the project's own `browser_instance` fixture (root conftest.py), NOT
the pytest-playwright plugin. This project doesn't install that plugin,
so there's no `browser`/`playwright` fixture to rely on; `browser_instance`
is the real, session-scoped browser your whole suite already uses.

Every test that takes `mobile_page` automatically runs once per entry in
ALL_DEVICE_NAMES — that's what turns a single `def test_x(mobile_page)`
into N parametrized runs, one per device, without loops in the test body.
"""
import pytest

from pages.mobile.mobile_devices import ALL_DEVICE_NAMES, ALL_DEVICE_PROFILES, device_id
from utils.mobile_report_builder import MobileReportCollector


@pytest.fixture(scope="session")
def mobile_report_collector():
    """One collector shared across the whole mobile session; written once at teardown."""
    collector = MobileReportCollector()
    yield collector
    collector.write()


@pytest.fixture(params=ALL_DEVICE_NAMES, ids=[device_id(n) for n in ALL_DEVICE_NAMES])
def mobile_context(request, browser_instance):
    device_name = request.param
    profile = ALL_DEVICE_PROFILES[device_name]
    context = browser_instance.new_context(**profile)
    yield context, device_name
    context.close()


@pytest.fixture
def mobile_page(mobile_context):
    context, device_name = mobile_context
    page = context.new_page()
    yield page, device_name
    page.close()