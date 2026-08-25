"""
Unit tests for utils/ai_mobile_triage.py — no API calls anywhere here.

The properties that matter:
  * grouping is deterministic and collapses device/size noise,
  * the payload budget drops groups LOUDLY, never silently,
  * Python owns the classification enum — the model cannot invent a label,
  * unconfigured means inert-with-a-log, never a fabricated analysis,
  * the whole path works against a fake provider end to end.
"""

from __future__ import annotations

import json

import pytest

from utils import ai_mobile_triage as amt


@pytest.fixture(autouse=True)
def _clean_store():
    amt.reset_session_triage()
    yield
    amt.reset_session_triage()


def finding(sev="minor", cat="tap_target", msg=None, device="iPhone 13",
            page="homepage", engine="chromium"):
    return {"severity": sev, "category": cat, "device": device, "page": page,
            "engine": engine,
            "message": msg or f"BUTTON 'View all Features' is 155x30 on {device} — below the 44px minimum."}


# ── Grouping ───────────────────────────────────────────────────────────


def test_same_issue_across_devices_folds_into_one_group():
    fs = [finding(device=d) for d in
          ("iPhone SE", "iPhone 13", "Pixel 5", "Galaxy S9+", "iPad Mini")]
    groups = amt.group_findings(fs)
    assert len(groups) == 1
    assert groups[0]["count"] == 5
    assert len(groups[0]["devices"]) == 5


def test_info_findings_are_not_triaged():
    """info = the auditor's own not-applicable/untested notes. They need no
    classification and would eat the token budget."""
    assert amt.group_findings([finding(sev="info")]) == []


def test_blockers_sort_before_minors():
    fs = [finding(sev="minor")] * 3 + [finding(sev="blocker", cat="scroll",
                                               msg="Page does not scroll")]
    groups = amt.group_findings(fs)
    assert groups[0]["severity"] == "blocker"
    assert groups[0]["id"] == "G1"


def test_pattern_collapse_is_size_and_device_blind():
    a = amt._pattern("INPUT is 280x24 on iPhone SE — below the 44px minimum")
    b = amt._pattern("INPUT is 310x28 on Pixel 5 — below the 44px minimum")
    assert a == b


# ── Payload budget ─────────────────────────────────────────────────────


def test_budget_excludes_groups_but_returns_the_fact():
    groups = [{"id": f"G{i}", "severity": "minor", "category": "c",
               "count": 1, "pages": ["p"], "devices": ["d"], "engines": ["e"],
               "samples": ["x" * 200]} for i in range(200)]
    payload, included = amt.build_payload(groups)
    assert len(included) < len(groups)
    assert len(payload) <= amt.MAX_PAYLOAD_CHARS + 2048
    assert json.loads(payload)[0]["id"] == "G0"


# ── Python owns the enum ───────────────────────────────────────────────


def test_invented_classification_becomes_needs_human():
    included = [{"id": "G1", "severity": "major", "category": "overflow",
                 "count": 6, "pages": [], "devices": [], "engines": [],
                 "samples": []}]
    out = amt.validate_output(
        {"groups": [{"id": "G1", "classification": "CATASTROPHIC_FAILURE",
                     "rationale": "sounds bad"}], "summary": "s"},
        included,
    )
    assert out["groups"][0]["classification"] == "NEEDS_HUMAN"
    assert "outside the closed set" in out["groups"][0]["rationale"]


def test_missing_proposal_becomes_needs_human():
    included = [{"id": "G1", "severity": "minor", "category": "c", "count": 1,
                 "pages": [], "devices": [], "engines": [], "samples": []}]
    out = amt.validate_output({"groups": [], "summary": ""}, included)
    assert out["groups"][0]["classification"] == "NEEDS_HUMAN"
    assert "no proposal" in out["groups"][0]["rationale"]


def test_wrong_shape_is_discarded_not_coerced():
    assert amt.validate_output(["not", "a", "dict"], []) is None
    assert amt.validate_output({"no_groups_key": 1}, []) is None


def test_confidence_outside_unit_interval_is_dropped():
    included = [{"id": "G1", "severity": "minor", "category": "c", "count": 1,
                 "pages": [], "devices": [], "engines": [], "samples": []}]
    out = amt.validate_output(
        {"groups": [{"id": "G1", "classification": "PRODUCT_DEFECT",
                     "confidence": 7}], "summary": ""}, included)
    assert out["groups"][0]["confidence"] is None


# ── Inert when unconfigured ────────────────────────────────────────────


def test_no_provider_means_none_and_no_store(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert amt.triage_mobile_findings([finding()], engine="chromium") is None
    assert amt.session_triage() is None


def test_no_findings_means_none_even_with_a_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    assert amt.triage_mobile_findings([finding(sev="info")]) is None


# ── End to end against a fake provider ─────────────────────────────────


def test_full_path_with_fake_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.chdir(tmp_path)          # sidecar writes land in tmp

    def fake_send_json(prompt, **kw):
        # Slice the payload between its known markers — the prompt's own
        # JSON-shape spec also contains brackets, so a naive index() grabs
        # a mixture of payload and instructions.
        seg = prompt[prompt.index("sample messages:"):prompt.index("For EACH group")]
        groups = json.loads(seg[seg.index("["):seg.rindex("]") + 1])
        return ({"groups": [
                    {"id": g["id"], "classification": "AUDITOR_ARTIFACT",
                     "confidence": 0.9, "rationale": "wide-in-scroller",
                     "suggested_fix": "check container",
                     "suggested_owner": "qa-framework"} for g in groups],
                 "summary": "All measurement artifacts."}, object())

    monkeypatch.setattr("utils.ai_parser.send_json", fake_send_json)
    fs = [finding(sev="major", cat="overflow",
                  msg=f"<table> renders 720px wide against a {w}px viewport")
          for w in (320, 375, 390)]
    out = amt.triage_mobile_findings(fs, engine="chromium")
    assert out is not None
    assert out["groups"][0]["classification"] == "AUDITOR_ARTIFACT"
    assert out["summary"] == "All measurement artifacts."
    assert amt.session_triage() is out


def test_unparseable_model_output_yields_no_analysis(monkeypatch):
    """A fabricated verdict is worse than an absent one."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr("utils.ai_parser.send_json",
                        lambda prompt, **kw: (None, object()))
    assert amt.triage_mobile_findings([finding(sev="major")]) is None
    assert amt.session_triage() is None


def test_sidecar_gains_the_triage(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.chdir(tmp_path)
    side = tmp_path / "reports" / "trend"
    side.mkdir(parents=True)
    (side / "mobile-summary.json").write_text(json.dumps(
        {"schema_version": 1, "engine": "chromium", "by_severity": {}}))
    monkeypatch.setattr(
        "utils.ai_parser.send_json",
        lambda prompt, **kw: ({"groups": [{"id": "G1",
                               "classification": "PRODUCT_DEFECT",
                               "rationale": "r"}], "summary": "s"}, object()))
    amt.triage_mobile_findings([finding(sev="major")], engine="chromium")
    data = json.loads((side / "mobile-summary.json").read_text())
    assert data["ai_triage"]["groups"][0]["classification"] == "PRODUCT_DEFECT"
    assert "input_groups" not in data["ai_triage"]


# ── The prompt is pinned ───────────────────────────────────────────────


def test_prompt_is_registered_v1_with_the_fallibility_clause():
    from utils.ai_prompts import get
    tpl = get("mobile_finding_triage")
    assert tpl.version == 1
    body = tpl.render(engine="e", group_count="1", groups_json="[]",
                      classifications="A")
    assert "may be wrong or may have skipped a step" in body
    assert "NEEDS_HUMAN" in body


def test_dashboard_block_states_absence_explicitly():
    from utils.dashboard_builder import _render_mobile_ai_triage
    amt.reset_session_triage()
    out = _render_mobile_ai_triage()
    assert "AI analysis unavailable" in out


def _seed_triage(monkeypatch):
    import json as _json
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "utils.ai_parser.send_json",
        lambda prompt, **kw: ({"groups": [
            {"id": "G1", "classification": "PRODUCT_DEFECT", "confidence": 0.8,
             "rationale": 'inputs <16px trigger <script>iOS</script> zoom',
             "suggested_fix": "bump font-size", "suggested_owner": "frontend"}],
            "summary": "One real defect."}, object()))
    return amt.triage_mobile_findings(
        [finding(sev="minor", cat="input_zoom",
                 msg="INPUT on signup has font-size 14px (< 16px)")],
        engine="chromium")


def test_dashboard_renders_the_triage_block_escaped(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert _seed_triage(monkeypatch) is not None
    from utils.dashboard_builder import _render_mobile_ai_triage
    out = _render_mobile_ai_triage()
    assert "PRODUCT_DEFECT" in out and "One real defect." in out
    assert "&lt;script&gt;" in out and "<script>" not in out
    assert "never affects scoring" in out


def test_pdf_export_carries_the_triage(monkeypatch, tmp_path):
    import types
    monkeypatch.chdir(tmp_path)
    assert _seed_triage(monkeypatch) is not None
    from utils import pdf_report_builder as prb
    out = prb._mobile_section(types.SimpleNamespace(mobile_health=97,
                                                    scoring_version=2))
    assert "AI triage" in out and "PRODUCT_DEFECT" in out
    assert "&lt;script&gt;" in out


# ── Output-token budget (2026-08-20 WebKit truncation regression) ───────


def test_output_budget_scales_with_group_count(monkeypatch):
    """The 24-group WebKit batch truncated at a flat max_tokens=1600: both
    live calls returned exactly 1600 output tokens, the JSON never closed,
    and the whole analysis was discarded. The budget must grow with the
    number of groups actually sent (~200 tokens each of validated output)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    seen: dict = {}

    def capture(prompt, **kw):
        seen.update(kw)
        return None, object()

    monkeypatch.setattr("utils.ai_parser.send_json", capture)
    fs = [finding(page=f"page{i}",
                  msg=f"BUTTON 'X{i}' is 10x10 — below the 44px minimum.")
          for i in range(24)]
    amt.triage_mobile_findings(fs, engine="webkit")
    assert seen["max_tokens"] == 600 + 200 * 24   # 5400 for the batch that failed live
    assert seen["max_tokens"] > 1600              # the flat cap that truncated


# ── Analysis coverage must be stated, not implied ───────────────────────


def test_ai_block_reports_analysed_coverage(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert _seed_triage(monkeypatch) is not None
    from utils.dashboard_builder import _render_mobile_ai_triage
    out = _render_mobile_ai_triage()
    assert "AI analysed 1 of 1 pattern group(s)." in out


def test_ai_absence_names_the_unanalysed_group_count():
    """A failed analysis must read as '0 of N analysed', not as a complete
    one — the 2026-08-20 WebKit run had a silent 0/86 behind a generic
    'unavailable' line."""
    from utils.dashboard_builder import _render_mobile_ai_triage
    amt.reset_session_triage()
    out = _render_mobile_ai_triage(total_groups=5)
    assert "AI analysis unavailable" in out
    assert "5 pattern group(s) awaited analysis" in out
    assert "0 were analysed" in out
