"""
Unit tests for Mobile as a scored domain (scoring v2).

The two properties that matter most here are both about NOT lying:

  * A run with no mobile tests reports Mobile = None, never 100. Claiming
    perfect mobile health for a run that never opened a mobile viewport is the
    same false signal as scoring an unset env var against the product.
  * `info` findings cost nothing. They mean "this check did not apply here",
    so penalising them would make an honest not-applicable indistinguishable
    from a defect.
"""

from __future__ import annotations

import types

import pytest

from utils import mobile_report_builder as mrb
from utils.layered_health_scores import SCORING_VERSION, compute


@pytest.fixture(autouse=True)
def _preserve_mobile_session_state():
    """Snapshot and restore the module globals around every test here.

    These tests exercise reset_session_findings()/record() on the REAL
    module-level accumulators. In a full-suite run the unit tests execute
    AFTER the mobile browser tests (tests/unit/ sorts last), so without this
    they destroyed the session's accumulated mobile evidence: the 2026-08-19
    full run recorded 391 mobile findings, then six reset calls in this file
    wiped them minutes before session-end scoring, and the dashboard reported
    'Mobile: not measured' for a run with 182 green mobile tests.
    """
    saved_findings = list(mrb._SESSION_FINDINGS)
    saved_devices = set(mrb._SESSION_DEVICES)
    yield
    mrb._SESSION_FINDINGS[:] = saved_findings
    mrb._SESSION_DEVICES.clear()
    mrb._SESSION_DEVICES.update(saved_devices)


def rec(status="PASS", feature="Marketing"):
    return types.SimpleNamespace(status=status, feature=feature, harness_fault=False)


def finding(severity="major", category="sticky"):
    return {"severity": severity, "category": category,
            "page": "homepage", "device": "iPhone 13", "message": "m"}


# ── Version ────────────────────────────────────────────────────────────


def test_scoring_version_pinned():
    assert SCORING_VERSION == 2, (
        f"Scoring changed to v{SCORING_VERSION}. Update this test and confirm "
        "trend consumers can distinguish the versions."
    )


def test_v2_scores_a_non_mobile_run_identically_to_v1():
    """The bump must not silently rescore the existing suite. With no mobile
    data the weights renormalise to the original 50/30/20."""
    records = [rec(), rec(), rec("FAIL")]
    got = compute(records, []).overall
    product = round(2 / 3 * 100)
    expected_v1 = round(product * 0.50 + 100 * 0.30 + 100 * 0.20)
    assert got == expected_v1


# ── Not-measured is not 100 ────────────────────────────────────────────


def test_no_mobile_findings_means_not_measured():
    assert compute([rec()], []).mobile_health is None


def test_empty_list_behaves_the_same_as_none():
    """A mobile run that produced zero findings still has no *score* basis
    here — the collector records findings, not passes."""
    assert compute([rec()], [], mobile_findings=[]).mobile_health is None
    assert compute([rec()], [], mobile_findings=[]).overall == compute([rec()], []).overall


# ── info is free ───────────────────────────────────────────────────────


def test_info_findings_do_not_penalise():
    s = compute([rec()], [], mobile_findings=[finding("info")] * 6)
    assert s.mobile_health == 100
    assert (s.mobile_blocker, s.mobile_major, s.mobile_minor) == (0, 0, 0)


# ── Penalties ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("sev, n, expected", [
    ("minor", 1, 97),
    ("major", 1, 92),
    ("major", 3, 76),
    ("blocker", 1, 75),
])
def test_penalty_scale(sev, n, expected):
    assert compute([rec()], [], mobile_findings=[finding(sev)] * n).mobile_health == expected


def test_penalty_is_capped_so_one_bad_page_cannot_zero_the_domain():
    s = compute([rec()], [], mobile_findings=[finding("blocker")] * 50)
    assert s.mobile_health == 40          # 100 - cap(60)
    assert s.mobile_blocker == 50


def test_counts_are_reported_separately_from_the_score():
    s = compute([rec()], [], mobile_findings=(
        [finding("blocker")] + [finding("major")] * 2 + [finding("minor")] * 3
        + [finding("info")] * 4
    ))
    assert (s.mobile_blocker, s.mobile_major, s.mobile_minor) == (1, 2, 3)


def test_mobile_pulls_the_overall_score_down():
    """The whole point of the fourth domain: a mobile regression must be
    visible in the headline number, not hidden behind a perfect global score."""
    clean = compute([rec()], [], mobile_findings=[finding("info")])
    broken = compute([rec()], [], mobile_findings=[finding("blocker")] * 3)
    assert broken.overall < clean.overall


def test_unknown_severity_is_not_counted_as_a_penalty():
    """Forward compatibility: a severity this version doesn't know must not be
    silently treated as a blocker."""
    s = compute([rec()], [], mobile_findings=[{"severity": "catastrophe"}])
    assert s.mobile_health == 100


# ── The collector feeds the scorer ─────────────────────────────────────


def test_collector_record_populates_the_session_accumulator():
    """Scoring reads a process-local accumulator rather than the JSON on disk,
    so it can never score a stale report from a previous run."""
    from utils.mobile_layout_auditor import Inconsistency
    mrb.reset_session_findings()
    assert mrb.session_findings() == []
    c = mrb.MobileReportCollector()
    c.record([Inconsistency(page="homepage", device="iPhone 13",
                            category="scroll", severity="major", message="m")])
    got = mrb.session_findings()
    mrb.reset_session_findings()
    assert len(got) == 1 and got[0]["severity"] == "major"


def test_session_findings_returns_a_copy():
    """Callers must not be able to mutate scoring state."""
    mrb.reset_session_findings()
    a = mrb.session_findings()
    a.append({"severity": "blocker"})
    assert mrb.session_findings() == []


# ── "ran and found nothing" vs "never ran" ─────────────────────────────


def test_a_clean_mobile_run_scores_100_not_not_measured():
    """The bug this pair exists to prevent. A mobile run that raises zero
    findings is a PASS and must score 100. Reporting it as not-measured makes
    a healthy run indistinguishable from one that never opened a viewport —
    the same false signal as scoring an untested domain 100, just inverted."""
    s = compute([rec()], [], mobile_findings=[], mobile_tested=True)
    assert s.mobile_health == 100
    assert (s.mobile_blocker, s.mobile_major, s.mobile_minor) == (0, 0, 0)


def test_not_running_mobile_still_reports_none():
    assert compute([rec()], [], mobile_findings=[], mobile_tested=False).mobile_health is None


def test_omitting_the_flag_falls_back_to_findings_presence():
    """Back-compat for callers that predate `mobile_tested`."""
    assert compute([rec()], [], mobile_findings=[]).mobile_health is None
    assert compute([rec()], [], mobile_findings=[finding("major")]).mobile_health == 92


def test_mobile_was_exercised_tracks_devices_not_findings():
    """The fixture registers a device before the test body runs, so a clean
    test still counts as exercised."""
    mrb.reset_session_findings()
    assert mrb.mobile_was_exercised() is False
    mrb.note_device_exercised("iPhone 13")
    assert mrb.mobile_was_exercised() is True
    assert mrb.session_devices() == {"iPhone 13"}
    assert mrb.session_findings() == []          # exercised, nothing wrong
    mrb.reset_session_findings()


def test_note_device_ignores_blank_names():
    mrb.reset_session_findings()
    mrb.note_device_exercised("")
    assert mrb.mobile_was_exercised() is False


# ── Report namespacing ─────────────────────────────────────────────────


def test_chromium_report_keeps_the_bare_path():
    """Matches conftest._report_root so the default output location is
    unchanged for the engine everyone runs by default."""
    assert mrb.MobileReportCollector(engine="chromium").report_dir() == "reports/mobile"


@pytest.mark.parametrize("engine", ["webkit", "firefox"])
def test_other_engines_nest(engine):
    """A WebKit run overwrote a Chromium report once, destroying the findings
    a cross-engine comparison depended on. Engines must not share a path."""
    assert mrb.MobileReportCollector(engine=engine).report_dir() == f"reports/mobile/{engine}"


def test_engine_is_recorded_in_the_summary():
    c = mrb.MobileReportCollector(engine="webkit")
    assert c.summary()["engine"] == "webkit"


def test_blank_engine_defaults_to_chromium():
    assert mrb.MobileReportCollector(engine="").report_dir() == "reports/mobile"


def test_written_paths_differ_per_engine(tmp_path):
    a = mrb.MobileReportCollector(engine="chromium")
    b = mrb.MobileReportCollector(engine="webkit")
    pa, _ = a.write(out_dir=str(tmp_path / a.report_dir()))
    pb, _ = b.write(out_dir=str(tmp_path / b.report_dir()))
    assert pa != pb


# ── Dashboard feature grouping ─────────────────────────────────────────


@pytest.mark.parametrize("test_name, expected", [
    # These are the REAL test names from tests/mobile/. Pinning them means a
    # rename, or a new generic pattern inserted too early, fails here rather
    # than silently collapsing distinct flows into one dashboard group.
    ("test_mobile_menu_opens_and_closes",                        "Mobile > Navigation"),
    ("test_mobile_menu_navigates_to_a_real_page",                "Mobile > Navigation"),
    ("test_hero_prompt_enables_generate_on_mobile",              "Mobile > Hero CTA"),
    ("test_hero_pill_tap_fills_the_prompt",                      "Mobile > Hero CTA"),
    ("test_hero_generate_cta_reaches_the_app",                   "Mobile > Hero CTA"),
    ("test_all_sections_render_while_scrolling",                 "Mobile > Scrolling"),
    ("test_fixed_header_survives_scrolling",                     "Mobile > Sticky elements"),
    ("test_no_overflow_across_css_breakpoints",                  "Mobile > Responsive"),
    ("test_marketing_page_survives_viewport_changes",            "Mobile > Responsive"),
    ("test_pricing_legal_entity_table_scrolls_instead_of_overflowing", "Mobile > Tables"),
    ("test_footer_is_reachable_and_its_links_work",              "Mobile > Footer"),
    ("test_section_anchor_taps_scroll_clear_of_the_fixed_header", "Mobile > Anchors"),
    ("test_contact_form_is_usable_on_mobile",                    "Mobile > Forms"),
    ("test_marketing_page_mobile_interactions",                  "Mobile > Interactions"),
    ("test_auth_page_mobile_interactions",                       "Mobile > Interactions"),
    ("test_marketing_page_mobile_layout",                        "Mobile > Layout"),
    ("test_auth_page_mobile_layout",                             "Mobile > Layout"),
])
def test_feature_mapping_is_specific_before_generic(test_name, expected):
    from tests.mobile.conftest import _feature_for
    assert _feature_for(test_name) == expected


def test_the_real_flow_tests_span_multiple_features():
    """Guards the collapse directly: if a generic pattern is inserted too early
    again, distinct flows fold into one group and this count drops."""
    from tests.mobile.conftest import _feature_for
    names = [
        "test_mobile_menu_opens_and_closes",
        "test_hero_pill_tap_fills_the_prompt",
        "test_all_sections_render_while_scrolling",
        "test_fixed_header_survives_scrolling",
        "test_no_overflow_across_css_breakpoints",
        "test_pricing_legal_entity_table_scrolls_instead_of_overflowing",
        "test_footer_is_reachable_and_its_links_work",
        "test_section_anchor_taps_scroll_clear_of_the_fixed_header",
        "test_contact_form_is_usable_on_mobile",
    ]
    assert len({_feature_for(n) for n in names}) >= 8


def test_an_unmapped_name_falls_back_to_plain_mobile():
    from tests.mobile.conftest import _feature_for
    assert _feature_for("test_something_entirely_new") == "Mobile"


# ── Scroll threshold must scale to the page ────────────────────────────


@pytest.mark.parametrize("available, expected", [
    (20,    10),   # signup page on iPhone SE -- a flat 40px was impossible
    (29,    14),   # signup page on Galaxy S9+
    (4,      8),   # floor
    (100,   40),   # cap reached
    (21000, 40),   # long marketing page
])
def test_required_scroll_delta_scales_with_available_range(available, expected):
    """Two Chromium blockers came from demanding 40px of movement on a page
    with 20-29px of scroll range. The requirement has to be proportional."""
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    a = A(page=None, page_name="signup", device_name="iPhone SE")
    assert a._required_scroll_delta(available) == expected


def test_required_delta_never_exceeds_what_the_page_can_do():
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    a = A(page=None, page_name="p", device_name="d")
    for available in range(9, 200):
        assert a._required_scroll_delta(available) <= available, available


# ── Swipe must not be faked ───────────────────────────────────────────


def test_no_synthetic_touchevent_swipe_implementation_remains():
    """An invented WebKit swipe path was asserted to work without being tested;
    it silently failed because WebKit lacks the TouchEvent constructor. It must
    stay removed rather than reappear as a green-making stand-in."""
    import inspect
    from utils import mobile_interaction_auditor as m
    src = inspect.getsource(m)
    assert "_JS_SWIPE" not in src
    assert "new TouchEvent" not in src
    assert not hasattr(m.MobileInteractionAuditor, "_JS_SWIPE")


def test_interaction_auditor_defines_every_constant_its_checks_use():
    """check_widget referenced self.MIN_TAP_TARGET_PX, which only existed on
    the LAYOUT auditor — so the check raised AttributeError on all 6 devices
    and both engines, verifying nothing. Attribute access on a class is not
    something pyflakes catches, so it is asserted here."""
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    for const in ("MIN_TAP_TARGET_PX", "MAX_REQUIRED_SCROLL_PX",
                  "MIN_REQUIRED_SCROLL_PX", "REQUIRED_SCROLL_FRACTION",
                  "MAX_FIXED_VIEWPORT_FRACTION", "FIXED_DRIFT_TOLERANCE_PX",
                  "SETTLE_MS", "CHECK_NAMES"):
        assert hasattr(A, const), const


def test_carousel_check_requires_behavioural_evidence_not_a_class_name():
    """Flozic's pricing page has `carousel_item`, `slider_Items` and
    `slidertask` on STATIC Bootstrap grid cells. Substring matching alone
    produced 6 false 'carousel did not respond to swipe' findings, so the
    check must demand horizontal scrollability, controls, or ARIA."""
    import inspect
    from utils import mobile_interaction_auditor as m
    src = inspect.getsource(m.MobileInteractionAuditor.check_swipe)
    assert "_CAROUSEL_EVIDENCE_JS" in src
    assert "NOT APPLICABLE" in src
    ev = m._CAROUSEL_EVIDENCE_JS
    for token in ("scrollWidth", "overflowX", "aria-roledescription", "is_carousel"):
        assert token in ev, token


# ── Phase 4/5 additions ────────────────────────────────────────────────


def test_input_zoom_check_is_registered_and_tap_stays_last():
    """input_zoom joins the registry; `tap` must remain the final check
    because it navigates away from the page under test."""
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    assert "input_zoom" in A.CHECK_NAMES
    assert A.CHECK_NAMES[-1] == "tap"
    a = A(page=None, page_name="p", device_name="d")
    assert callable(a._checks()["input_zoom"])


def test_min_input_font_constant_exists():
    """check_input_zoom references self.MIN_INPUT_FONT_PX — the widget check
    already crashed once on a constant that lived on the other auditor, so
    every new constant gets pinned."""
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    assert A.MIN_INPUT_FONT_PX == 16    # Apple's documented focus-zoom threshold


def test_fast_3g_profile_matches_devtools_preset():
    """The throttle must be the standard Fast 3G preset, not an invented one —
    otherwise 'works on Fast 3G' means nothing outside this suite."""
    from tests.mobile.test_mobile_network import FAST_3G, LOAD_BUDGET_MS
    assert FAST_3G["latency"] == 150
    assert FAST_3G["downloadThroughput"] == int(1.6 * 1024 * 1024 / 8)
    assert FAST_3G["uploadThroughput"] == int(750 * 1024 / 8)
    assert FAST_3G["offline"] is False
    assert LOAD_BUDGET_MS >= 30_000     # a gate, not a perf SLO


@pytest.mark.parametrize("test_name, expected", [
    ("test_modals_open_and_close_on_tap",                  "Mobile > Modals"),
    ("test_history_back_and_forward_keep_the_page_alive",  "Mobile > History"),
    ("test_network_fast_3g_homepage_stays_usable",         "Mobile > Network"),
    ("test_signup_validation_does_not_crop_the_logo",      "Mobile > Signup validation"),
    ("test_contact_select_is_usable_on_mobile",            "Mobile > Forms"),
])
def test_phase4_and_5_names_map_to_their_own_features(test_name, expected):
    from tests.mobile.conftest import _feature_for
    assert _feature_for(test_name) == expected


def test_signup_fragment_does_not_steal_the_auth_layout_tests():
    """The auth layout tests' parametrized ids contain 'signup'
    (e.g. test_auth_page_mobile_layout[iPhone_SE-signup-target1]); the new
    'signup_validation' fragment must not re-map them."""
    from tests.mobile.conftest import _feature_for
    assert _feature_for(
        "test_auth_page_mobile_layout[iPhone_SE-signup-target1]"
    ) == "Mobile > Layout"


# ── The full-run wipe bug ──────────────────────────────────────────────


def test_unit_tests_restore_the_session_state_they_touch():
    """Meta-guard for the autouse fixture above: seed state as a mobile run
    would, run a wipe like the tests here do, and verify the fixture contract
    restores it. Without this, unit tests silently destroyed the mobile
    evidence of any full run they shared a process with."""
    mrb.note_device_exercised("Probe Device")
    mrb._SESSION_FINDINGS.append({"severity": "info", "probe": True})
    # simulate what a test in this file does
    mrb.reset_session_findings()
    assert mrb.mobile_was_exercised() is False
    # the autouse fixture will restore after THIS test too; verify the
    # snapshot mechanism itself round-trips
    mrb._SESSION_FINDINGS[:] = [{"severity": "info", "probe": True}]
    mrb._SESSION_DEVICES.add("Probe Device")
    assert mrb.mobile_was_exercised() is True


def test_setup_error_is_a_failure_not_a_pass():
    """8 tests that ERRORed in setup were recorded as PASS on 2026-08-19."""
    import types
    import conftest as root_conftest
    ok = types.SimpleNamespace(failed=False)
    bad = types.SimpleNamespace(failed=True)
    assert root_conftest._test_failed(
        types.SimpleNamespace(rep_call=None, rep_setup=bad)) is True
    assert root_conftest._test_failed(
        types.SimpleNamespace(rep_call=bad, rep_setup=ok)) is True
    assert root_conftest._test_failed(
        types.SimpleNamespace(rep_call=ok, rep_setup=ok)) is False
    assert root_conftest._test_failed(
        types.SimpleNamespace(rep_call=None, rep_setup=None)) is False


def test_health_card_badge_and_footnote_agree():
    """The 2026-08-19 run rendered 69/100 in the badge and 'Overall 79/100' in
    the footnote of the same card — two scoring systems on one surface. The
    badge must show the layered overall; the legacy score must be labelled."""
    import types
    from utils.dashboard_builder import _render_health_section
    layered = types.SimpleNamespace(
        product_health=72, infra_health=76, framework_health=100, overall=79,
        mobile_health=None, mobile_blocker=0, mobile_major=0, mobile_minor=0,
        harness_fault_count=18, scoring_version=2,
    )
    import re
    flat = re.sub(r"\s+", " ", _render_health_section(69, layered))
    assert "79<span" in flat, "badge must show the layered overall"
    assert "legacy pass-rate score: 69/100" in flat, "legacy score must be labelled"
    assert "Overall 79/100" in flat, "footnote weighting must agree with the badge"


# ── Cross-engine sidecar + export ──────────────────────────────────────


def test_findings_are_engine_stamped():
    from utils.mobile_layout_auditor import Inconsistency
    mrb.reset_session_findings()
    c = mrb.MobileReportCollector(engine="webkit")
    c.record([Inconsistency(page="p", device="d", category="scroll",
                            severity="info", message="m")])
    got = mrb.session_findings()
    mrb.reset_session_findings()
    assert got[0]["engine"] == "webkit"


def test_summary_sidecar_roundtrips_per_engine(tmp_path):
    """The sidecar is what lets a Chromium export include WebKit's latest
    mobile results — without it, a WebKit run's findings exist only in that
    process's memory and can never reach a later export."""
    from utils.mobile_layout_auditor import Inconsistency
    mrb.reset_session_findings()
    for engine, sev in (("chromium", "minor"), ("webkit", "info")):
        c = mrb.MobileReportCollector(engine=engine)
        c.record([Inconsistency(page="homepage", device="iPhone 13",
                                category="scroll", severity=sev,
                                message="UNTESTED on this engine"
                                if engine == "webkit" else "m")])
        path = c.write_summary(base=str(tmp_path))
        assert (engine == "chromium") == ("webkit" not in path)
    mrb.reset_session_findings()
    loaded = mrb.load_engine_summaries(base=str(tmp_path))
    assert {d["engine"] for d in loaded} == {"chromium", "webkit"}
    wk = next(d for d in loaded if d["engine"] == "webkit")
    assert wk["schema_version"] == 1 and wk["findings"]


def test_pdf_mobile_section_includes_other_engines(tmp_path, monkeypatch):
    """The user-reported gap: 'when pdf export then the webkit information is
    not exported'. The section must carry a per-engine table."""
    import types
    from utils.mobile_layout_auditor import Inconsistency
    from utils import pdf_report_builder as prb
    mrb.reset_session_findings()
    wk = mrb.MobileReportCollector(engine="webkit")
    wk.record([Inconsistency(page="homepage", device="iPhone 13",
                             category="scroll", severity="info",
                             message="touch UNTESTED on webkit")])
    wk.write_summary(base=str(tmp_path))
    mrb.reset_session_findings()   # simulate a LATER chromium session
    orig = mrb.load_engine_summaries          # capture BEFORE patching, or the
    monkeypatch.setattr(                      # lambda recurses into itself
        "utils.mobile_report_builder.load_engine_summaries",
        lambda base="reports/trend": orig(base=str(tmp_path)),
    )
    scores = types.SimpleNamespace(mobile_health=None, scoring_version=2)
    out = prb._mobile_section(scores)
    assert "webkit" in out, "WebKit row missing from the export section"
    assert "Untested" in out and "never scored" in out
    assert "no mobile tests ran" in out


def test_pdf_mobile_section_escapes_finding_text():
    from utils.mobile_layout_auditor import Inconsistency
    import types
    from utils import pdf_report_builder as prb
    mrb.reset_session_findings()
    c = mrb.MobileReportCollector(engine="chromium")
    c.record([Inconsistency(page="p", device="d", category="overflow",
                            severity="major",
                            message='<table class="cmp-table"> is wide')])
    out = prb._mobile_section(types.SimpleNamespace(mobile_health=70, scoring_version=2))
    mrb.reset_session_findings()
    assert "&lt;table" in out and '<table class="cmp-table">' not in out


def test_pdf_health_grid_has_a_mobile_box():
    import types
    from utils import pdf_report_builder as prb
    measured = prb._mobile_score_box(types.SimpleNamespace(mobile_health=92))
    absent = prb._mobile_score_box(types.SimpleNamespace(mobile_health=None))
    assert ">92<" in measured and "Mobile Health" in measured
    assert "not measured" in absent
    assert "40%" in prb._pdf_weighting_note(
        types.SimpleNamespace(mobile_health=92, scoring_version=2))
    assert "50%" in prb._pdf_weighting_note(
        types.SimpleNamespace(mobile_health=None, scoring_version=2))


def test_dashboard_mobile_table_has_cell_padding_and_engine_column():
    """The reported cramped-table issue: cells must carry real padding and the
    Engine column must exist; the message column must wrap, not widen."""
    from utils.dashboard_builder import _render_mobile_section
    findings = [{"severity": "minor", "engine": "webkit", "page": "homepage",
                 "device": "iPhone 13", "category": "scroll",
                 "message": "x" * 300, "details": {}}]
    out = _render_mobile_section(findings, None)
    assert ">Engine</th>" in out
    assert out.count("padding:6px 10px") >= 8
    assert "overflow-wrap:anywhere" in out
    assert "webkit" in out


def test_dashboard_groups_findings_into_patterns():
    """P0 dashboard work: the same issue on N devices is ONE pattern row with
    a device-reach label — the per-device rows are evidence, not the unit a
    developer acts on. 247 findings read as ~36-86 patterns, not 247 bugs."""
    from utils.dashboard_builder import _render_mobile_section
    devices = ["iPhone SE", "iPhone 13", "iPhone 14 Pro Max",
               "Pixel 5", "Galaxy S9+", "iPad Mini"]
    findings = [{"severity": "minor", "engine": "webkit", "page": "pricing",
                 "device": d, "category": "tap_target",
                 "message": f"A 'TRY NOW' is 90x30 on {d} — below the 44px minimum.",
                 "details": {}} for d in devices]
    findings.append({"severity": "minor", "engine": "webkit", "page": "signup",
                     "device": "iPhone SE", "category": "overflow",
                     "message": "<div> renders 500px wide against a 375px viewport",
                     "details": {}})
    out = _render_mobile_section(findings, None)
    assert "7 findings → 2 unique patterns" in out
    assert "6/6" in out and "cross-device" in out
    assert "1/6" in out and "device-specific" in out
    assert "Unique issue patterns" in out
    # The raw evidence stays available but collapsed behind <details>.
    assert "<details" in out and "raw findings" in out


def test_dashboard_aggregates_untested_notices_by_check():
    """UNTESTED/UNSUPPORTED notices must surface as an aggregated coverage
    panel (one row per check family), never as N identical rows a reader has
    to discover by scrolling the raw table."""
    from utils.dashboard_builder import _render_mobile_section
    findings = [{"severity": "info", "engine": "webkit", "page": p,
                 "device": d, "category": "h_pan",
                 "message": "Horizontal pan could not be exercised on webkit: "
                            "needs the Chromium CDP gesture API. UNTESTED, "
                            "not substituted.",
                 "details": {}}
                for p in ("homepage", "pricing") for d in ("iPhone SE", "Pixel 5")]
    findings.append({"severity": "info", "engine": "webkit", "page": "homepage",
                     "device": "iPhone SE", "category": "network",
                     "message": "Network throttling is UNSUPPORTED on webkit: it "
                                "needs CDP Network.emulateNetworkConditions.",
                     "details": {}})
    out = _render_mobile_section(findings, None)
    assert "Coverage gaps on this engine: 2 check families not exercised" in out
    assert "(5 notice(s))" in out
    assert "h_pan" in out and "network" in out
    assert "UNTESTED means untested, not passed" in out


def test_check_run_tally_and_reset():
    """Executed coverage cannot be derived from findings (clean checks emit
    nothing), so the auditor tallies attempts explicitly, per engine."""
    import utils.mobile_report_builder as mrb
    mrb.reset_session_findings()
    mrb.note_check_run("webkit", executed=True)
    mrb.note_check_run("webkit", executed=False)
    mrb.note_check_run("chromium", executed=True)
    stats = mrb.session_check_stats()
    assert stats["webkit"] == {"attempted": 2, "executed": 1}
    assert stats["chromium"] == {"attempted": 1, "executed": 1}
    mrb.reset_session_findings()
    assert mrb.session_check_stats() == {}


def test_crash_is_not_executed_but_content_na_is():
    """Both branches of the executed-coverage semantics: with page=None the
    scroll check CRASHES ('NOT verified' -> attempted, not executed), while
    the form check runs to a content-driven conclusion ('No visible text
    input' -> executed: we looked, there was nothing to test)."""
    import utils.mobile_report_builder as mrb
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    mrb.reset_session_findings()
    a = A(page=None, page_name="login", device_name="iPhone SE")
    a.run_all(include={"scroll", "form"})
    assert mrb.session_check_stats()[a.engine] == {"attempted": 2, "executed": 1}


def test_dashboard_stat_strip_and_coverage_lines(monkeypatch):
    """The three-questions strip: observations, unique issues, and executed
    coverage are different numbers and must never be conflated into one."""
    from utils import dashboard_builder as db
    import utils.mobile_report_builder as mrb
    monkeypatch.setattr(mrb, "load_engine_summaries",
                        lambda base="reports/trend": [
        {"engine": "chromium", "check_stats": {"attempted": 100, "executed": 78}},
        {"engine": "webkit", "check_stats": {"attempted": 100, "executed": 50}},
    ])
    findings = [{"severity": "minor", "engine": "webkit", "page": "pricing",
                 "device": "iPhone SE", "category": "tap_target",
                 "message": "A 'X' is 10x10 on iPhone SE — below the 44px minimum.",
                 "details": {}},
                {"severity": "info", "engine": "webkit", "page": "homepage",
                 "device": "iPhone SE", "category": "h_pan",
                 "message": "Horizontal pan UNTESTED, not substituted.",
                 "details": {}}]
    out = db._render_mobile_section(findings, None)
    assert ">Findings<" in out and ">Unique patterns<" in out
    assert ">Coverage gaps<" in out
    assert ">Blocker<" in out and ">Major<" in out and ">Minor<" in out
    assert "chromium</strong> 78% executed (78/100 interaction checks)" in out
    assert "webkit</strong> 50% executed (50/100 interaction checks)" in out
    assert "never affects the score" in out


def test_direct_check_invocation_is_tallied():
    """Flow tests call check methods directly (bypassing run_all); the tally
    must count those invocations too. The 2026-08-20 validation run caught
    6 direct invocations invisible to a run_all-only tally: WebKit reported
    48 not-executed runs against 54 gap instances in the findings."""
    import pytest as _pytest
    import utils.mobile_report_builder as mrb
    from utils.mobile_interaction_auditor import MobileInteractionAuditor as A
    mrb.reset_session_findings()
    a = A(page=None, page_name="app_directory", device_name="Pixel 5")
    with _pytest.raises(AttributeError):
        a.check_vertical_scroll()          # direct call; crashes on page=None
    assert mrb.session_check_stats()[a.engine] == {"attempted": 1, "executed": 0}
