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
import logging

import pytest

from utils.snapshot_writer import add_test_record

from pages.mobile.mobile_devices import ALL_DEVICE_NAMES, ALL_DEVICE_PROFILES, device_id
from utils.mobile_report_builder import MobileReportCollector, note_device_exercised


@pytest.fixture(scope="session")
def mobile_report_collector(request):
    """One collector shared across the whole mobile session; written at teardown.

    Namespaced by engine so a WebKit run cannot overwrite the Chromium report.
    Cross-engine comparison is the entire point of running both, and it is
    impossible if each run clobbers the last.
    """
    engine = request.config.getoption("--browser")
    collector = MobileReportCollector(engine=engine)
    yield collector
    # No standalone HTML report — the dashboard is the single human surface.
    # But a machine-readable per-engine sidecar IS written: the printable/PDF
    # export must be able to show every engine's latest mobile results, and a
    # WebKit run's findings otherwise exist only in that process's memory.
    path = collector.write_summary()
    logging.getLogger(__name__).info(
        "[mobile] %s: %d finding(s) recorded — dashboard section + %s "
        "(data sidecar for cross-engine export; not a report).",
        engine, len(collector.findings), path,
    )


@pytest.fixture(params=ALL_DEVICE_NAMES, ids=[device_id(n) for n in ALL_DEVICE_NAMES])
def mobile_context(request, browser_instance):
    device_name = request.param
    # Register the device BEFORE the test body, so a clean run that raises no
    # findings still counts as "mobile was exercised" and scores 100 rather
    # than reading as not-measured.
    note_device_exercised(device_name)
    profile = ALL_DEVICE_PROFILES[device_name]
    context = browser_instance.new_context(**profile)
    yield context, device_name
    context.close()


@pytest.fixture
def mobile_page(mobile_context, request):
    context, device_name = mobile_context
    page = context.new_page()
    yield page, device_name
    # Failure artifacts + AI triage for MOBILE tests. The root conftest does
    # this in the `page` fixture teardown, which mobile tests never touch —
    # so until now a failing mobile test produced no screenshot, no DOM, and
    # no triage: the same fixture-bypass shape as the missing-TestRecords bug.
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        try:
            import conftest as _root
            from pathlib import Path as _P
            folder = _root._capture_failure_artifacts(page, request.node.name)
            try:
                from utils.ai_triage import triage as _ai_triage
                _ai_triage(
                    test_name=request.node.name,
                    failure_folder=_P("reports/failures") / folder,
                    exception_message=str(getattr(rep, "longrepr", "")).splitlines()[0],
                    traceback_text="\n".join(
                        str(getattr(rep, "longrepr", "")).splitlines()[-20:]),
                )
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "[mobile] AI failure triage failed (non-fatal): %s", e)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "[mobile] failure-artifact capture failed (non-fatal): %s", e)
    page.close()

# ── Outcome recording ──────────────────────────────────────────────────
#
# WHY THIS EXISTS: the root conftest records a TestRecord in the `page`
# fixture's teardown. Mobile tests use `mobile_page`, which builds its own
# context from browser_instance and never touches `page` — so a 90-test mobile
# run wrote **0 test records** and appeared nowhere in the dashboard's totals,
# test table, or pass rate. Ninety green tests, invisible.
#
# These tests are also module-level functions, not classes, so the root
# recorder's `request.node.cls._test_category` lookup would yield
# UNKNOWN/Unknown even if it did fire. Metadata is supplied explicitly here.

# Test-name fragment -> dashboard feature. Mirrors the mobile-compatibility
# taxonomy so the dashboard's per-feature grouping is actually useful rather
# than dumping 90 rows under one label.
# ORDER IS SIGNIFICANT: first match wins, so SPECIFIC fragments must precede
# generic ones. An earlier version listed "scroll"/"section" first, and since
# most of these names mention scrolling, four distinct flows collapsed into
# "Mobile > Scrolling" -- the fixed-header, legal-entity-table and
# section-anchor tests all lost their own grouping on the dashboard.
_FEATURE_MAP = (
    # -- specific ---------------------------------------------------------
    # `anchor` precedes `fixed_header`: the section-anchor test mentions BOTH
    # ("...taps_scroll_clear_of_the_fixed_header") and its subject is anchors —
    # the header is only what it measures against.
    ("anchor",        "Mobile > Anchors"),
    ("fixed_header",  "Mobile > Sticky elements"),
    ("sticky",        "Mobile > Sticky elements"),
    ("legal_entity",  "Mobile > Tables"),
    ("table",         "Mobile > Tables"),
    ("footer",        "Mobile > Footer"),
    ("contact",       "Mobile > Forms"),
    ("menu",          "Mobile > Navigation"),
    ("nav",           "Mobile > Navigation"),
    ("hero",          "Mobile > Hero CTA"),
    ("cta",           "Mobile > Hero CTA"),
    ("breakpoint",    "Mobile > Responsive"),
    ("viewport",      "Mobile > Responsive"),
    ("orientation",   "Mobile > Orientation"),
    ("swipe",         "Mobile > Touch gestures"),
    ("modal",         "Mobile > Modals"),
    ("history",       "Mobile > History"),
    ("network",       "Mobile > Network"),
    ("signup_validation", "Mobile > Signup validation"),
    ("select",        "Mobile > Forms"),
    ("interaction",   "Mobile > Interactions"),
    # -- generic, last ----------------------------------------------------
    ("overflow",      "Mobile > Responsive"),
    ("form",          "Mobile > Forms"),
    ("section",       "Mobile > Scrolling"),
    ("scroll",        "Mobile > Scrolling"),
    ("layout",        "Mobile > Layout"),
)


def _feature_for(test_name: str) -> str:
    low = test_name.lower()
    for fragment, feature in _FEATURE_MAP:
        if fragment in low:
            return feature
    return "Mobile"


def _resolve_error_signature(node, failed: bool) -> str:
    """Failure signature for a mobile test: the exception stashed by the root
    makereport hook (mobile failures go through it too — this path just never
    passed it on), with the same 'E '-line longrepr fallback as the root
    recorder, so mobile failures cluster by cause like every other failure."""
    sig = getattr(node, "_error_signature", "") or ""
    if failed and not sig:
        for phase in ("rep_call", "rep_setup"):
            _r = getattr(node, phase, None)
            if _r is not None and _r.failed and getattr(_r, "longrepr", None):
                elines = [ln.lstrip("E ").strip()
                          for ln in str(_r.longrepr).splitlines()
                          if ln.lstrip().startswith("E ")]
                if elines:
                    return elines[-1][:300]
    return sig


def _record_mobile(request) -> None:
    """Testable body of the autouse recorder (see _record_mobile_outcome)."""
    rep = getattr(request.node, "rep_call", None)
    failed = rep is not None and rep.failed
    if rep is None and getattr(request.node, "rep_setup", None) is not None:
        # Error during setup — still a result worth showing.
        failed = request.node.rep_setup.failed
    engine = request.config.getoption("--browser")
    cohort = "baseline-mobile" if engine == "chromium" else f"baseline-mobile-{engine}"
    error_signature = _resolve_error_signature(request.node, failed)
    add_test_record(
        category="REGRESSION",
        login="Guest",                    # marketing pages need no session
        feature=_feature_for(request.node.name),
        clazz=request.node.module.__name__.rsplit(".", 1)[-1],
        method=request.node.name,         # includes the device parameter
        status="FAIL" if failed else "PASS",
        duration_ms=int((rep.duration if rep else 0) * 1000),
        cohort=cohort,
        harness_fault=bool(getattr(request.node, "harness_fault", False)),
        error_signature=error_signature,
    )


@pytest.fixture(autouse=True)
def _record_mobile_outcome(request):
    """Record every mobile test into the snapshot so the dashboard counts it."""
    yield
    _record_mobile(request)
