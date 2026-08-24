"""
Delivery for the post-run report.

Same shape as utils/ai_provider: pick a backend from the environment, and
degrade to a no-op when unconfigured rather than failing the run. Sending a
report must never be able to turn a green run red.

Environment
-----------
  FLOZIC_EMAIL_TO         comma-separated recipients. UNSET => no send at all.
  FLOZIC_EMAIL_FROM       sender address
  FLOZIC_EMAIL_BACKEND    smtp | dryrun   (default: dryrun)
  FLOZIC_EMAIL_DRYRUN     "1" forces dry-run even when backend=smtp
  FLOZIC_EMAIL_WHEN       always | on_success | on_failure
                          NO DEFAULT — unset means nothing is sent.

WHY THE TRIGGER HAS NO DEFAULT
------------------------------
Who gets mailed, and on which outcomes, is a policy decision about a real
mailing list — not something this module should assume. An earlier draft
defaulted to "always", which would have started mailing a distribution list the
moment recipients were configured. Unset now means inert, with a log line saying
what to set. Choosing to mail people has to be an explicit act.

  FLOZIC_SMTP_HOST        e.g. smtp.gmail.com
  FLOZIC_SMTP_PORT        default 587 (STARTTLS)
  FLOZIC_SMTP_USER        defaults to FLOZIC_EMAIL_FROM
  FLOZIC_SMTP_PASSWORD    read from env ONLY, never logged

Gmail senders need a 16-character App Password, not the account password —
Google stopped accepting the latter for SMTP in 2022, and it requires 2FA on
the account. Generate one at https://myaccount.google.com/apppasswords.

DRY RUN IS THE DEFAULT
----------------------
With the default backend the message is written to
reports/trend/email/<timestamp>.eml and nothing is transmitted. Two reasons:
nobody should discover the emailer works by accidentally mailing the team, and
the whole path stays testable with no mail server. Set
FLOZIC_EMAIL_BACKEND=smtp deliberately when you want real delivery.
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from mimetypes import guess_type
from pathlib import Path

from utils.email_report import EmailPayload

logger = logging.getLogger(__name__)

DRYRUN_DIR = Path("reports/trend/email")

VALID_WHEN = ("always", "on_success", "on_failure")


@dataclass
class MailConfig:
    to: list[str]
    sender: str
    backend: str
    when: str                 # "" when unset — see module docstring
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    dryrun_forced: bool = False

    @property
    def configured(self) -> bool:
        """True when there is somewhere to send. Recipients are the gate: with
        none set, the notifier is inert regardless of other values."""
        return bool(self.to)

    @property
    def policy_set(self) -> bool:
        """True when FLOZIC_EMAIL_WHEN names a known policy."""
        return self.when in VALID_WHEN

    @property
    def can_transmit(self) -> bool:
        if self.dryrun_forced:
            return False
        return (
            self.backend == "smtp"
            and self.configured
            and bool(self.sender and self.smtp_host and self.smtp_password)
        )


def load_config() -> MailConfig:
    def env(name: str, default: str = "") -> str:
        return (os.environ.get(name) or default).strip()

    to = [a.strip() for a in env("FLOZIC_EMAIL_TO").split(",") if a.strip()]
    sender = env("FLOZIC_EMAIL_FROM")
    backend = env("FLOZIC_EMAIL_BACKEND", "dryrun").lower()

    # No default. An unrecognised value is treated as unset rather than
    # silently coerced to a mailing policy.
    when = env("FLOZIC_EMAIL_WHEN").lower()
    if when and when not in VALID_WHEN:
        logger.warning(
            "[email] FLOZIC_EMAIL_WHEN=%r is not one of %s — treating as unset, "
            "nothing will be sent.", when, list(VALID_WHEN),
        )
        when = ""

    dryrun_forced = env("FLOZIC_EMAIL_DRYRUN").lower() in ("1", "true", "yes", "on")

    try:
        port = int(env("FLOZIC_SMTP_PORT", "587"))
    except ValueError:
        port = 587

    return MailConfig(
        to=to,
        sender=sender,
        backend=backend,
        when=when,
        smtp_host=env("FLOZIC_SMTP_HOST"),
        smtp_port=port,
        smtp_user=env("FLOZIC_SMTP_USER") or sender,
        # Google shows App Passwords as four space-separated groups
        # ("abcd efgh ijkl mnop"). Copying them verbatim is the norm, so strip
        # ALL whitespace rather than just the ends — an internal space reaches
        # login() as a wrong password and surfaces as a generic auth failure.
        smtp_password="".join(env("FLOZIC_SMTP_PASSWORD").split()),
        dryrun_forced=dryrun_forced,
    )


def should_send(failed: int, when: str) -> bool:
    """Apply the trigger policy. `failed` is the run's failure count.

    An unset or unknown policy returns False. There is deliberately no
    permissive fallback — see the module docstring.
    """
    if when == "always":
        return True
    if when == "on_success":
        return failed == 0
    if when == "on_failure":
        return failed > 0
    return False


def is_xdist_worker() -> bool:
    """True on a pytest-xdist worker process.

    Every worker runs session teardown, so without this the run sends one
    email per worker. Only the controller (where PYTEST_XDIST_WORKER is unset)
    should deliver.
    """
    return bool(os.environ.get("PYTEST_XDIST_WORKER"))


# ── Message assembly ───────────────────────────────────────────────────


def build_message(payload: EmailPayload, cfg: MailConfig) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = payload.subject
    msg["From"] = cfg.sender or "flowguard@localhost"
    msg["To"] = ", ".join(cfg.to)
    # text first, html second — multipart/alternative semantics: last part is
    # the preferred rendering.
    msg.set_content(payload.text_body)
    msg.add_alternative(payload.html_body, subtype="html")

    for path in payload.attachments:
        try:
            data = path.read_bytes()
        except Exception as e:
            logger.warning("[email] Skipping unreadable attachment %s: %s", path, e)
            continue
        ctype, _ = guess_type(path.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype or "octet-stream",
            filename=path.name,
        )
    return msg


# ── Delivery ───────────────────────────────────────────────────────────


def _write_dryrun(msg: EmailMessage) -> Path:
    DRYRUN_DIR.mkdir(parents=True, exist_ok=True)
    path = DRYRUN_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.eml"
    path.write_bytes(bytes(msg))
    return path


def send(payload: EmailPayload, failed: int, cfg: MailConfig | None = None) -> str:
    """
    Deliver the report. Returns a short status string for the session log.

    Never raises: a delivery problem is logged and swallowed, because the run's
    result must not depend on the mail server being reachable.
    """
    cfg = cfg or load_config()

    if is_xdist_worker():
        return "skipped (xdist worker — the controller sends)"

    if not cfg.configured:
        return "skipped (FLOZIC_EMAIL_TO not set)"

    if not cfg.policy_set:
        logger.info(
            "[email] FLOZIC_EMAIL_WHEN is not set — no report sent. Set it to "
            "one of %s to enable delivery.", list(VALID_WHEN),
        )
        return "skipped (FLOZIC_EMAIL_WHEN not set)"

    if not should_send(failed, cfg.when):
        return f"skipped (policy {cfg.when}, failures={failed})"

    try:
        msg = build_message(payload, cfg)
    except Exception as e:
        logger.warning("[email] Could not build the message: %s", e)
        return f"failed to build: {e}"

    if not cfg.can_transmit:
        try:
            path = _write_dryrun(msg)
        except Exception as e:
            logger.warning("[email] Dry-run write failed: %s", e)
            return f"dry-run write failed: {e}"
        # Name the ACTUAL cause. The override has to be checked first, or a
        # forced dry-run gets reported as a misconfiguration and sends whoever
        # is debugging off after a password that was never the problem.
        if cfg.dryrun_forced:
            reason = "FLOZIC_EMAIL_DRYRUN is set"
        elif cfg.backend != "smtp":
            reason = f"backend={cfg.backend}"
        else:
            missing = [
                name for name, val in (
                    ("FLOZIC_EMAIL_FROM", cfg.sender),
                    ("FLOZIC_SMTP_HOST", cfg.smtp_host),
                    ("FLOZIC_SMTP_PASSWORD", cfg.smtp_password),
                ) if not val
            ]
            reason = f"backend=smtp but {', '.join(missing)} unset"
        logger.info(
            "[email] DRY RUN (%s) — message written to %s (%d attachment(s), "
            "not sent)", reason, path, len(payload.attachments),
        )
        return f"dry-run -> {path}"

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        # The overwhelmingly common cause, worth naming explicitly.
        logger.error(
            "[email] SMTP authentication failed for %s. For a Gmail sender this "
            "usually means an App Password is required (the account password is "
            "rejected) — https://myaccount.google.com/apppasswords",
            cfg.smtp_user,
        )
        return "smtp auth failed"
    except Exception as e:
        logger.error("[email] Send failed via %s: %s", cfg.smtp_host, e)
        return f"send failed: {e}"

    logger.info(
        "[email] Sent %r to %s (%d attachment(s))",
        payload.subject, ", ".join(cfg.to), len(payload.attachments),
    )
    return f"sent to {', '.join(cfg.to)}"
