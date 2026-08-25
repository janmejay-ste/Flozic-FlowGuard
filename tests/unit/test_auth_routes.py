"""
Unit tests for the auth-route predicates.

These are pure string checks, but they encode a distinction that is easy to
get wrong and expensive when it is: PASSWORD_STEP_PATHS and POST_SUBMIT_PATHS
both describe "somewhere after the username box", yet they demand opposite
handling. Conflating them makes `handle_authv2_login_if_present` return before
submitting a password, and every login-required test then fails at its
post-login URL wait with no hint that auth was the cause.

Cognito moved the password step to its own route (/login/continue) in
2026-08. The `is_on_auth_route` cases exist to prove that the framework's many
`"/login" in url` substring checks still recognise it, so that migration did
not need a sweep through pages/.
"""

from __future__ import annotations

import os

import pytest

from pages import auth_state
from pages.authv2_helper import (
    PASSWORD_STEP_PATHS,
    POST_SUBMIT_PATHS,
    is_on_password_step,
)

# A representative step-3 authorize URL. The authorize params ride along, so
# a predicate written against a bare path would miss it — that shape is the
# point of these tests. state/code_challenge are DUMMY placeholders, not a
# real session: the project treats a live state/code_challenge as a leak
# (see utils/email_report.strip_query), so no live-session URL is stored here.
# The assertions below depend only on the /login/continue path, never on the
# auth-param values.
STEP3_URL = (
    "https://accounts.flozic.ai/login/continue"
    "?client_id=58b8gtnccnaio60rilfsffetrk&response_type=code"
    "&scope=openid+email+profile+aws.cognito.signin.user.admin"
    "&redirect_uri=https%3A%2F%2Floop.flozic.ai%2Fauth%2Fcognito%2Fcallback"
    "&state=0000000000000000000000000000000000000000000000000000000000000000"
    "&code_challenge=DUMMY_TEST_CODE_CHALLENGE_NOT_A_REAL_VALUE"
    "&code_challenge_method=S256"
)
STEP1_URL = "https://accounts.flozic.ai/login?client_id=58b8gtnccnaio60rilfsffetrk"


class FakePage:
    """auth_state's predicates take a Page only for `.url`."""
    def __init__(self, url: str) -> None:
        self.url = url


# ── The distinction that must not collapse ─────────────────────────────


def test_password_step_and_post_submit_are_disjoint():
    """The regression guard. If a future edit adds /login/continue to
    POST_SUBMIT_PATHS, the helper skips the form instead of filling it and
    every login-required test fails somewhere far from the cause."""
    assert not set(PASSWORD_STEP_PATHS) & set(POST_SUBMIT_PATHS)


def test_step3_route_is_not_treated_as_finished():
    assert not any(p in STEP3_URL for p in POST_SUBMIT_PATHS)


@pytest.mark.parametrize("url", [
    "https://accounts.flozic.ai/verifyPassword",
    "https://loop.flozic.ai/auth/cognito/callback?code=abc",
])
def test_genuinely_finished_routes_are_not_the_password_step(url):
    assert any(p in url for p in POST_SUBMIT_PATHS)
    assert not is_on_password_step(url)


# ── is_on_password_step ────────────────────────────────────────────────


def test_step3_url_is_the_password_step():
    assert is_on_password_step(STEP3_URL) is True


def test_step1_url_is_not_the_password_step():
    """Step 1 must fall through to the username box, not skip to a password
    field that has not been rendered yet."""
    assert is_on_password_step(STEP1_URL) is False


@pytest.mark.parametrize("url", ["", None])
def test_password_step_handles_empty_url(url):
    assert is_on_password_step(url) is False


# ── The substring checks scattered through pages/ ──────────────────────


@pytest.mark.parametrize("url", [STEP1_URL, STEP3_URL])
def test_both_login_steps_count_as_an_auth_route(url):
    """`"/login" in url` is how ~5 call sites recognise the login page.
    /login/continue keeps matching, which is why the host migration did not
    need those sites touched."""
    assert auth_state.is_on_auth_route(FakePage(url)) is True


def test_step3_url_is_on_a_known_auth_host():
    assert auth_state.is_on_auth_host(STEP3_URL) is True


# ── Blank-credential guard ─────────────────────────────────────────────


class ExplodingPage:
    """Any interaction is a failure. The guard must return before touching
    the page at all — a fill("") on a real page CLEARS the username box and
    the resulting 'Missing username.' error reads as a product defect."""
    url = "https://accounts.flozic.ai/login?client_id=x"

    def __getattr__(self, name):
        raise AssertionError(
            f"guard let execution reach page.{name}() with blank credentials"
        )


@pytest.mark.parametrize("email, password", [
    ("", "pw"),
    ("user@example.com", ""),
    ("", ""),
    ("   ", "pw"),
])
def test_blank_credentials_never_reach_the_form(email, password):
    from pages.authv2_helper import handle_authv2_login_if_present
    assert handle_authv2_login_if_present(
        ExplodingPage(), email=email, password=password,
    ) is False


def test_unset_env_is_what_makes_credentials_blank():
    """Documents the mechanism: os.environ.get(..., "") yields "" rather than
    raising, so an unset var flows all the way to fill() unless guarded."""
    from pages import auth_helper
    assert auth_helper.DEFAULT_EMAIL == os.environ.get("AUTOMATE_EMAIL", "")


# ── perform_login's post-state verification ────────────────────────────


class StubPage:
    """Records wait_for_url calls and fails them on demand.

    `perform_login` used to fall off the end of its stage-2 block without
    checking anything, so a refused login reported "Step 1 DONE: Logged in."
    and the run failed 20s later on a dashboard assertion — scored as
    Product: 0. These tests pin the check that replaced that silence.
    """

    def __init__(self, url="https://loop.flozic.ai/auth/cognito/login", match=False):
        self.url = url
        self.match = match
        self.waits: list[int] = []

    def wait_for_url(self, pattern, timeout=None):
        self.waits.append(timeout)
        if not self.match:
            raise RuntimeError("Timeout waiting for URL")


def _verify(page, email="u@e.com", password="pw", minutes=3):
    from pages.auth_helper import _verify_logged_in
    return _verify_logged_in(
        page, email, password, lambda u: True, minutes, grace_ms=10,
    )


def test_verification_passes_when_post_login_url_matches():
    page = StubPage(url="https://loop.flozic.ai/connects", match=True)
    assert _verify(page) is None
    assert len(page.waits) == 1, "should not fall through to the manual window"


def test_missing_credentials_raise_instead_of_waiting_for_a_human():
    """A blank env var is a config error. Blocking 3 minutes per test for a
    manual login nobody asked for turns one mistake into a hung suite."""
    from utils.harness_errors import HarnessConfigError
    page = StubPage()
    with pytest.raises(HarnessConfigError) as e:
        _verify(page, email="", password="")
    msg = str(e.value)
    assert "AUTOMATE_EMAIL" in msg and "AUTOMATE_PASSWORD" in msg
    assert "not a product defect" in msg, "triage needs the attribution hint"
    assert len(page.waits) == 1, "must not open the manual window"


def test_present_credentials_fall_back_to_the_manual_window():
    """Credentials set but login didn't land = selector drift or a challenge
    screen, which is exactly what the manual window is for."""
    page = StubPage()
    with pytest.raises(RuntimeError):
        _verify(page, minutes=2)
    assert page.waits == [10, 120_000], f"expected grace then manual, got {page.waits}"


def test_verification_never_reports_success_without_a_matching_url():
    """The regression this whole block exists for: silence must be impossible."""
    page = StubPage()
    with pytest.raises(Exception):
        _verify(page, email="", password="")


# ── Harness-fault attribution ──────────────────────────────────────────


def _rec(status="FAIL", feature="Core Journey", harness_fault=False):
    from utils.snapshot_writer import TestRecord
    return TestRecord(
        category="SANITY", login="Auth", feature=feature, clazz="C",
        method="m", status=status, duration_ms=1, harness_fault=harness_fault,
    )


def test_harness_fault_does_not_score_against_the_product():
    """One unset env var used to report Product: 0 — "your application is
    critically broken" for a run that never reached the application."""
    from utils.layered_health_scores import compute
    scores = compute([_rec(harness_fault=True)], [])
    assert scores.product_health == 100
    assert scores.harness_fault_count == 1


def test_a_real_product_failure_still_scores_zero():
    """The exclusion must not become a blanket amnesty."""
    from utils.layered_health_scores import compute
    scores = compute([_rec()], [])
    assert scores.product_health == 0
    assert scores.harness_fault_count == 0


def test_harness_faults_leave_the_denominator_too():
    """2 tests, 1 harness fault, 1 real failure => 0% of what was scoreable,
    not 50%. Keeping the fault in the denominator would flatter the score."""
    from utils.layered_health_scores import compute
    scores = compute([_rec(harness_fault=True), _rec()], [])
    assert scores.product_health == 0
    assert scores.harness_fault_count == 1


def test_mixed_run_scores_only_the_real_checks():
    from utils.layered_health_scores import compute
    records = [_rec(status="PASS"), _rec(status="PASS"), _rec(harness_fault=True)]
    scores = compute(records, [])
    assert scores.product_health == 100
    assert scores.harness_fault_count == 1


def test_config_error_is_not_an_assertion_error():
    """pytest renders AssertionError as an assertion failure, which is the
    framing this exception exists to avoid."""
    from utils.harness_errors import HarnessConfigError, HarnessError
    assert issubclass(HarnessConfigError, HarnessError)
    assert not issubclass(HarnessConfigError, AssertionError)


# ── Mobile interaction auditor: pure logic ─────────────────────────────


def test_every_registered_check_is_a_real_method():
    """run_all dispatches name -> bound method. A typo would silently drop a
    whole class of interaction from the suite, and the run would still be
    green."""
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    a = A(page=None, page_name="p", device_name="d")
    checks = a._checks()
    assert tuple(checks) == A.CHECK_NAMES, "registry and CHECK_NAMES disagree"
    for name, fn in checks.items():
        assert callable(fn), name


def test_tap_runs_last_because_it_navigates_away():
    """Ordering is load-bearing: any check after `tap` would audit whatever
    page the tap landed on, not the page under test."""
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    assert A.CHECK_NAMES[-1] == "tap"


def test_a_crashing_check_is_recorded_not_swallowed():
    """A check that raises must leave a finding saying the interaction was
    NOT verified. Swallowing it would make a crash look like a clean pass —
    the exact failure mode this suite exists to avoid."""
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    a = A(page=None, page_name="homepage", device_name="iPhone 13")
    findings = a.run_all(include={"scroll"})
    assert len(findings) == 1
    assert findings[0].category == "scroll"
    assert "NOT verified" in findings[0].message


def test_include_narrows_the_check_set():
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    a = A(page=None, page_name="login", device_name="iPhone SE")
    findings = a.run_all(include={"scroll", "form"})
    assert {f.category for f in findings} == {"scroll", "form"}


def test_report_builder_escapes_markup_in_messages():
    """Overflow findings legitimately contain '<table class="cmp-table">'.
    Unescaped, the browser rendered it as a real tag and ate the row."""
    from utils.mobile_layout_auditor import Inconsistency
    from utils.mobile_report_builder import MobileReportCollector
    c = MobileReportCollector()
    c.record([Inconsistency(
        page="pricing", device="iPhone SE", category="overflow", severity="major",
        message='<table class="cmp-table"> renders 720px wide',
    )])
    out = c._render_html(c.summary())
    assert "&lt;table" in out
    assert "<table class=\"cmp-table\">" not in out


def test_report_builder_handles_the_info_severity():
    """info is emitted for not-applicable checks; an unmapped severity used
    to raise KeyError while rendering the HTML."""
    from utils.mobile_layout_auditor import Inconsistency
    from utils.mobile_report_builder import MobileReportCollector
    c = MobileReportCollector()
    c.record([Inconsistency(page="p", device="d", category="swipe",
                            severity="info", message="no carousel found")])
    assert c.summary()["by_severity"]["info"] == 1
    assert "no carousel found" in c._render_html(c.summary())


def test_report_builder_fixtures_do_not_leak_into_session_state():
    """The two tests above record synthetic findings through the REAL
    collector, and record() appends to the module-global _SESSION_FINDINGS.
    On 2026-08-20 those fixtures leaked into the live dashboard of a full
    run as a phantom major ('<table class="cmp-table"> renders 720px wide',
    pricing/iPhone SE) — contradicting the passing dedicated pricing-table
    test on the same page/device. tests/unit/conftest.py now save/restores
    the globals around every unit test; this asserts the restore happened."""
    from utils.mobile_report_builder import session_findings
    leaked = [f for f in session_findings()
              if f.get("message") == '<table class="cmp-table"> renders 720px wide'
              or (f.get("page") == "p" and f.get("device") == "d")]
    assert leaked == []


# ── Connectivity loss is not a product failure ─────────────────────────


@pytest.mark.parametrize("msg", [
    "page.goto: net::ERR_INTERNET_DISCONNECTED at https://www.flozic.ai/",
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_CONNECTION_RESET at https://loop.flozic.ai/connects",
])
def test_connectivity_errors_are_recognised(msg):
    """A real overnight run produced 15 of these across unrelated tests when
    the machine lost WiFi. Unrecognised, they score as a product collapse."""
    from utils.harness_errors import looks_like_connectivity_loss
    assert looks_like_connectivity_loss(msg) is True


@pytest.mark.parametrize("msg", [
    "Timeout 30000ms exceeded waiting for locator('button')",
    "AssertionError: Dashboard did not load",
    "",
    None,
])
def test_timeouts_are_not_treated_as_connectivity_loss(msg):
    """The dangerous direction. A timeout can be a slow product OR a real
    hang; excusing those as infrastructure would hide genuine defects."""
    from utils.harness_errors import looks_like_connectivity_loss
    assert looks_like_connectivity_loss(msg) is False


def test_network_failures_do_not_score_against_the_product():
    from utils.layered_health_scores import compute
    scores = compute([_rec(harness_fault=True), _rec(status="PASS")], [])
    assert scores.product_health == 100
    assert scores.harness_fault_count == 1
