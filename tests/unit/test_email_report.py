"""
Unit tests for the run-report email: payload building, trigger policy and the
leak guard.

The leak guard is the one that matters most. `reports/failures/` is 144MB with
individual folders up to 2.9MB, and the auth-flow DOM captures contain the full
OAuth URL including `state` and `code_challenge`. Attaching or linking any of
that would mail captured page state to every recipient, so there are explicit
tests asserting it cannot reach a payload.
"""

from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path

import pytest

from utils import email_report as er
from utils import notifier as nf


def decision(label="READY", status_value="READY"):
    return types.SimpleNamespace(
        label=label, color="#16a34a", bg="#f0fdf4",
        description="ok", status=types.SimpleNamespace(value=status_value),
    )


def scores(overall=100):
    return types.SimpleNamespace(
        overall=overall, product_health=100, infra_health=100, framework_health=100,
    )


def stats(total=10, passed=10, failed=0, skipped=0, pass_rate=100.0):
    return {
        "total": total, "passed": passed, "failed": failed,
        "skipped": skipped, "pass_rate": pass_rate,
        "smoke_total": total, "smoke_passed": passed,
    }


def record(method="test_x", status="FAIL", feature="F", folder=None):
    r = types.SimpleNamespace(method=method, status=status, feature=feature)
    if folder is not None:
        r.artifact_folder = folder
    return r


# ── Subject ────────────────────────────────────────────────────────────


def test_subject_encodes_a_pass():
    s = er.build_subject(stats(), 100, when=datetime(2026, 8, 11, 12, 37))
    assert "PASS 10/10" in s
    assert "health 100" in s
    assert "11 Aug 12:37" in s


def test_subject_encodes_a_failure_count():
    s = er.build_subject(stats(total=196, passed=182, failed=14), 50)
    assert "FAIL 14 of 196" in s


def test_subject_handles_an_empty_run():
    assert "NO TESTS" in er.build_subject(stats(0, 0, 0), None)


def test_subject_omits_health_when_unknown():
    assert "health" not in er.build_subject(stats(), None)


def test_subject_is_prefixed_for_filtering():
    assert er.build_subject(stats(), 100).startswith("[FlowGuard] ")


# ── Leak guard ─────────────────────────────────────────────────────────


def test_failure_artifacts_are_never_attachable():
    """The core guarantee. reports/failures holds DOM captures with OAuth
    state; it must not be attachable even if a caller asks."""
    asked = [
        Path("reports/failures/test_x_20260811/dom.html"),
        Path("reports/failures/test_x_20260811/screenshot.png"),
        Path("reports/recordings/test_x/canvas.png"),
    ]
    assert er.existing_attachments(asked) == []


def test_attachment_allowlist_contains_no_failure_paths():
    for p in er.ATTACHMENTS:
        assert "reports/failures" not in str(p).replace("\\", "/")
        assert "recordings" not in str(p).replace("\\", "/")


def test_existing_attachments_skips_missing_files(tmp_path):
    present = tmp_path / "a.json"
    present.write_text("{}")
    got = er.existing_attachments([present, tmp_path / "gone.json"])
    assert got == [present]


@pytest.mark.parametrize("url, expected", [
    ("https://accounts.flozic.ai/login?client_id=x&state=secret&code_challenge=y",
     "https://accounts.flozic.ai/login"),
    ("https://loop.flozic.ai/connects", "https://loop.flozic.ai/connects"),
    ("", ""),
    (None, ""),
])
def test_strip_query_removes_oauth_parameters(url, expected):
    """state / code_challenge appear in this product's auth URLs. The path is
    what makes a failure legible; the query is only a leak risk."""
    assert er.strip_query(url) == expected


def test_body_contains_no_failure_artifact_paths():
    body = er.build_html_body(
        stats(total=1, passed=0, failed=1), decision("BLOCKED"),
        scores=scores(0), records=[record(folder="test_x_20260811")],
    )
    assert "reports/failures" not in body


# ── HTML body ──────────────────────────────────────────────────────────


def test_body_is_email_safe():
    """No <style> block, no script, no flex/grid — mail clients strip or
    ignore all of those. Layout must be tables with inline styles."""
    body = er.build_html_body(stats(), decision(), scores=scores())
    assert "<style" not in body.lower()
    assert "<script" not in body.lower()
    assert "display:flex" not in body.replace(" ", "")
    assert "<table" in body.lower()


def test_body_reports_a_clean_run():
    body = er.build_html_body(stats(), decision(), scores=scores())
    assert "No failures in this run." in body


def test_body_lists_failures_with_triage():
    body = er.build_html_body(
        stats(total=2, passed=1, failed=1), decision("BLOCKED"),
        scores=scores(50), records=[record(method="test_login")],
    )
    assert "test_login" in body
    assert "Failures" in body


def test_body_escapes_html_in_test_names():
    """Test ids and diagnoses are interpolated; a stray angle bracket must not
    become markup in a mail client."""
    body = er.build_html_body(
        stats(total=1, passed=0, failed=1), decision(),
        records=[record(method="test_<script>alert(1)</script>")],
    )
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_body_includes_the_exec_summary_when_present():
    body = er.build_html_body(
        stats(), decision(), exec_summary="All tests passed cleanly.",
    )
    assert "All tests passed cleanly." in body


def test_body_truncates_a_very_long_failure_list():
    records = [record(method=f"test_{i}") for i in range(40)]
    body = er.build_html_body(
        stats(total=40, passed=0, failed=40), decision(), records=records,
    )
    assert "and 15 more" in body


def test_text_body_is_present_for_multipart():
    text = er.build_text_body(stats(), decision())
    assert "FlowGuard" in text
    assert "<" not in text.replace("<br>", "")


# ── Trigger policy ─────────────────────────────────────────────────────


@pytest.mark.parametrize("when, failed, expected", [
    ("always",     0, True),
    ("always",     3, True),
    ("on_success", 0, True),
    ("on_success", 3, False),
    ("on_failure", 0, False),
    ("on_failure", 3, True),
    # No permissive fallback: unset or unknown means do not send.
    ("",           0, False),
    ("",           3, False),
    ("on_tuesdays", 0, False),
])
def test_should_send_policy(when, failed, expected):
    assert nf.should_send(failed, when) is expected


def test_unknown_policy_is_treated_as_unset(monkeypatch):
    """Never coerce an unrecognised value into a real mailing policy."""
    monkeypatch.setenv("FLOZIC_EMAIL_WHEN", "on_tuesdays")
    monkeypatch.setenv("FLOZIC_EMAIL_TO", "a@b.com")
    cfg = nf.load_config()
    assert cfg.when == ""
    assert cfg.policy_set is False


def test_unset_policy_blocks_delivery(monkeypatch):
    """Configuring recipients must not by itself start mailing anyone."""
    monkeypatch.setenv("FLOZIC_EMAIL_TO", "a@b.com")
    monkeypatch.delenv("FLOZIC_EMAIL_WHEN", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    payload = er.EmailPayload(subject="s", html_body="", text_body="")
    assert "FLOZIC_EMAIL_WHEN not set" in nf.send(payload, failed=0)


def test_dryrun_env_var_overrides_smtp_backend(monkeypatch):
    """FLOZIC_EMAIL_DRYRUN=1 must win even with SMTP fully configured."""
    for k, v in {
        "FLOZIC_EMAIL_TO": "a@b.com", "FLOZIC_EMAIL_FROM": "s@g.com",
        "FLOZIC_EMAIL_BACKEND": "smtp", "FLOZIC_SMTP_HOST": "smtp.gmail.com",
        "FLOZIC_SMTP_PASSWORD": "x", "FLOZIC_EMAIL_DRYRUN": "1",
    }.items():
        monkeypatch.setenv(k, v)
    assert nf.load_config().can_transmit is False


# ── Config gating ──────────────────────────────────────────────────────


def test_no_recipients_means_inert(monkeypatch):
    monkeypatch.delenv("FLOZIC_EMAIL_TO", raising=False)
    assert nf.load_config().configured is False


def test_recipients_are_split_and_trimmed(monkeypatch):
    monkeypatch.setenv("FLOZIC_EMAIL_TO", " a@b.com , c@d.com ,, ")
    assert nf.load_config().to == ["a@b.com", "c@d.com"]


def test_backend_defaults_to_dryrun(monkeypatch):
    """Nobody should discover the emailer works by mailing the team."""
    monkeypatch.delenv("FLOZIC_EMAIL_BACKEND", raising=False)
    monkeypatch.setenv("FLOZIC_EMAIL_TO", "a@b.com")
    cfg = nf.load_config()
    assert cfg.backend == "dryrun"
    assert cfg.can_transmit is False


def test_smtp_without_password_cannot_transmit(monkeypatch):
    """A half-configured SMTP setup must fall back to dry-run, not crash or
    silently claim success."""
    for k, v in {
        "FLOZIC_EMAIL_TO": "a@b.com", "FLOZIC_EMAIL_FROM": "s@g.com",
        "FLOZIC_EMAIL_BACKEND": "smtp", "FLOZIC_SMTP_HOST": "smtp.gmail.com",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FLOZIC_SMTP_PASSWORD", raising=False)
    assert nf.load_config().can_transmit is False


def test_smtp_user_defaults_to_sender(monkeypatch):
    monkeypatch.setenv("FLOZIC_EMAIL_TO", "a@b.com")
    monkeypatch.setenv("FLOZIC_EMAIL_FROM", "sender@gmail.com")
    monkeypatch.delenv("FLOZIC_SMTP_USER", raising=False)
    assert nf.load_config().smtp_user == "sender@gmail.com"


def test_app_password_whitespace_is_removed(monkeypatch):
    """Google renders App Passwords as four space-separated groups. Pasting
    them as shown is the normal case, and an internal space reaches login() as
    a wrong password — surfacing as a generic auth failure that looks like a
    revoked credential rather than a copy-paste artefact."""
    monkeypatch.setenv("FLOZIC_EMAIL_TO", "a@b.com")
    monkeypatch.setenv("FLOZIC_SMTP_PASSWORD", " abcd efgh ijkl mnop ")
    assert nf.load_config().smtp_password == "abcdefghijklmnop"


def test_xdist_worker_does_not_send(monkeypatch):
    """Every worker runs session teardown; without this the run sends one
    email per worker."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    assert nf.is_xdist_worker() is True
    payload = er.EmailPayload(subject="s", html_body="<p></p>", text_body="t")
    assert "xdist" in nf.send(payload, failed=0)


# ── Message assembly + dry run ─────────────────────────────────────────


def test_message_is_multipart_with_both_bodies(monkeypatch):
    monkeypatch.setenv("FLOZIC_EMAIL_TO", "a@b.com")
    monkeypatch.setenv("FLOZIC_EMAIL_FROM", "s@g.com")
    payload = er.EmailPayload(
        subject="subj", html_body="<p>hi</p>", text_body="hi",
    )
    msg = nf.build_message(payload, nf.load_config())
    assert msg["Subject"] == "subj"
    assert msg["To"] == "a@b.com"
    assert msg.is_multipart()
    types_present = {p.get_content_type() for p in msg.walk()}
    assert "text/plain" in types_present
    assert "text/html" in types_present


def test_attachments_are_included(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOZIC_EMAIL_TO", "a@b.com")
    f = tmp_path / "dashboard.html"
    f.write_text("<html></html>")
    payload = er.EmailPayload(
        subject="s", html_body="<p></p>", text_body="t", attachments=[f],
    )
    msg = nf.build_message(payload, nf.load_config())
    assert "dashboard.html" in [
        p.get_filename() for p in msg.walk() if p.get_filename()
    ]


def test_unreadable_attachment_is_skipped_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOZIC_EMAIL_TO", "a@b.com")
    payload = er.EmailPayload(
        subject="s", html_body="<p></p>", text_body="t",
        attachments=[tmp_path / "missing.json"],
    )
    msg = nf.build_message(payload, nf.load_config())      # must not raise
    assert msg["Subject"] == "s"


def test_dry_run_writes_a_parseable_eml(monkeypatch, tmp_path):
    import email as email_mod
    monkeypatch.setenv("FLOZIC_EMAIL_TO", "a@b.com")
    monkeypatch.setenv("FLOZIC_EMAIL_FROM", "s@g.com")
    monkeypatch.setenv("FLOZIC_EMAIL_BACKEND", "dryrun")
    monkeypatch.setenv("FLOZIC_EMAIL_WHEN", "always")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(nf, "DRYRUN_DIR", tmp_path / "email")

    payload = er.EmailPayload(
        subject="dry", html_body="<p>body</p>", text_body="body",
    )
    status = nf.send(payload, failed=0)
    assert status.startswith("dry-run ->")

    written = list((tmp_path / "email").glob("*.eml"))
    assert len(written) == 1
    parsed = email_mod.message_from_bytes(written[0].read_bytes())
    assert parsed["Subject"] == "dry"


def test_send_never_raises_when_unconfigured(monkeypatch):
    """A mail problem must never be able to fail a test run."""
    monkeypatch.delenv("FLOZIC_EMAIL_TO", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    payload = er.EmailPayload(subject="s", html_body="", text_body="")
    assert "skipped" in nf.send(payload, failed=0)


# ── End-to-end payload ─────────────────────────────────────────────────


def test_build_assembles_a_complete_payload():
    payload = er.build(
        stats=stats(total=196, passed=182, failed=14),
        decision=decision("BLOCKED"),
        scores=scores(50),
        records=[record(method="test_a"), record(method="test_b")],
        exec_summary="Fourteen tests failed.",
        when=datetime(2026, 8, 11, 10, 45),
    )
    assert "FAIL 14 of 196" in payload.subject
    assert "Fourteen tests failed." in payload.html_body
    assert "test_a" in payload.html_body
    assert payload.text_body
    assert payload.schema_version == er.SCHEMA_VERSION
    for p in payload.attachments:
        assert "reports/failures" not in str(p).replace("\\", "/")


def test_dotenv_loader_flags_merged_variable_lines(tmp_path, caplog, monkeypatch):
    """OPENAI_API_KEY once shipped with 'OPENAI_MODEL=gpt-4o' glued to its
    tail because two vars shared one line. The loader must name that at load
    time — the alternative was diagnosing it from four masked characters in
    an HTTP 401."""
    import logging
    import conftest as root
    monkeypatch.delenv("MERGED_TEST_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_text("MERGED_TEST_KEY=sk-proj-abcOPENAI_MODEL=gpt-4o\n")
    with caplog.at_level(logging.WARNING):
        loaded = root._load_dotenv(str(f))
    monkeypatch.delenv("MERGED_TEST_KEY", raising=False)
    assert "MERGED_TEST_KEY" in loaded
    assert any("TWO merged variables" in r.message for r in caplog.records)


def test_dotenv_loader_does_not_flag_clean_values(tmp_path, caplog, monkeypatch):
    import logging
    import conftest as root
    monkeypatch.delenv("CLEAN_TEST_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_text("CLEAN_TEST_KEY=sk-proj-a1b2c3d4e5\n")
    with caplog.at_level(logging.WARNING):
        root._load_dotenv(str(f))
    monkeypatch.delenv("CLEAN_TEST_KEY", raising=False)
    assert not any("TWO merged" in r.message for r in caplog.records)
