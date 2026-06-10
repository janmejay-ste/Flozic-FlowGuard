"""
JavaScript console monitor + classifier. Mirrors the Java
`JsConsoleMonitor` (src/test/java/utils/JsConsoleMonitor.java) so the
Python smoke tests are apples-to-apples with the Java ones.

Classification policy (matches Java exactly):
  FAIL   — uncaught exceptions, TypeError/ReferenceError/SyntaxError,
            chunk-load failures, etc., when the source origin is a
            known first-party domain (flozic.ai, appypie.com, etc.).
  WARN   — same patterns from unknown origins, generic warnings.
  IGNORE — explicit message-level noise (favicon, Zaraz, Swiper, CSP,
            extension errors), known third-party origins (Google
            Analytics, Cloudflare Zaraz, Intercom, Hotjar, etc.).

The IGNORE lists exist because real production sites emit a long tail
of third-party noise (analytics SDKs, tag managers, ad blockers) that
would otherwise drown out real product breakage in smoke tests.

Usage:
    monitor = JsConsoleMonitor(page)
    page.goto("...")
    assert monitor.fatal_count() == 0, monitor.fatal_events
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from playwright.sync_api import ConsoleMessage, Error, Page

logger = logging.getLogger(__name__)


class Severity(Enum):
    FAIL = "FAIL"
    WARN = "WARN"
    IGNORE = "IGNORE"


# ── Classification policy (mirrors Java JsConsoleMonitor constants) ──

FIRST_PARTY_DOMAINS = [
    "appypieautomate.ai",
    "flozic.ai",
    "appypie.com",
    "connectcloud.appypie.com",
    "accounts.appypie.com",
]

IGNORE_ORIGINS = [
    "googletagmanager.com", "google-analytics.com", "analytics",
    "googleads.g.doubleclick.net", "googlesyndication.com",
    "hotjar.com", "hj.js",
    "intercom", "crisp.chat",
    "clarity.ms",
    "facebook.net", "connect.facebook",
    "twitter.com",
    "cdn.segment", "cdn.amplitude",
    "zaraz",
    "extension://",
    "moz-extension://",
    "chrome-extension://",
]

IGNORE_MESSAGES = [
    "favicon",
    "net::err_blocked_by_client",
    "net::err_blocked_by_administrator",
    "net::err_aborted",
    "frame-ancestors",
    "content security policy",
    "deprecated",
    "swiper is not defined",
    "fedcm",
    "identity provider",
    "zaraz is loaded twice",
    "zaraz",
]

FAIL_MESSAGES = [
    "TypeError",
    "ReferenceError",
    "SyntaxError",
    "RangeError",
    "Uncaught",
    "Cannot read propert",
    "is not a function",
    "is not defined",
    "is not a constructor",
    "ChunkLoadError",
    "Loading chunk",
    "hydration",
    "Invariant Violation",
    "ExpressionChangedAfterItHasBeenChecked",
    "Cannot match any routes",
    " 500 ",
    " 503 ",
    "Internal Server Error",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_FAILED",
]

# Match Java's source-URL extraction (lifted from Chrome BROWSER log
# format). Playwright's ConsoleMessage gives us the URL directly via
# msg.location, so we mainly use this for the pageerror path.
_SOURCE_URL_RE = re.compile(r"^(https?://[^\s]+)\s+\d+:\d+")


@dataclass
class ConsoleEvent:
    type: str
    text: str
    location: str | None = None  # "url:line:col" or None
    source_origin: str | None = None  # hostname only


class JsConsoleMonitor:
    """
    Captures console + pageerror events for the lifetime of a Page.
    Attach in a fixture BEFORE the test navigates.
    """

    def __init__(self, page: Page) -> None:
        self.page = page
        self.events: list[ConsoleEvent] = []
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)

    # ── Event handlers ─────────────────────────────────────────────────

    def _on_console(self, msg: ConsoleMessage) -> None:
        loc = msg.location
        location_str: str | None = None
        origin: str | None = None
        if loc and loc.get("url"):
            url = str(loc["url"])
            location_str = (
                f"{url}:{loc.get('lineNumber', 0)}:{loc.get('columnNumber', 0)}"
            )
            origin = _extract_hostname(url)
        self.events.append(ConsoleEvent(
            type=msg.type, text=msg.text,
            location=location_str, source_origin=origin,
        ))

    def _on_page_error(self, err: Error) -> None:
        text = str(err)
        # pageerror events don't carry source URL directly. Try to extract
        # from text (Java's leading-URL pattern); else origin is None and
        # the classifier treats it as "unknown origin → WARN".
        origin = None
        m = _SOURCE_URL_RE.match(text)
        if m:
            origin = _extract_hostname(m.group(1))
        self.events.append(ConsoleEvent(
            type="pageerror", text=text,
            location=None, source_origin=origin,
        ))

    # ── Classification (mirrors Java JsConsoleMonitor.classify) ──────

    @staticmethod
    def classify(event: ConsoleEvent) -> Severity:
        text = event.text or ""
        lower = text.lower()

        # 1. Console types other than error / pageerror → not FAIL.
        # Warnings and info messages are always IGNORE for the
        # "fatal_count" purpose, even if their text matches a FAIL pattern.
        # (Java only inspects SEVERE-level entries.)
        if event.type not in ("error", "pageerror"):
            return Severity.IGNORE

        # 2. Message-level ignore patterns
        for pattern in IGNORE_MESSAGES:
            if pattern.lower() in lower:
                return Severity.IGNORE

        # 3. Third-party origin → always IGNORE
        if event.source_origin and _is_third_party(event.source_origin):
            return Severity.IGNORE

        # 4. Check FAIL patterns
        has_fail = any(p.lower() in lower for p in FAIL_MESSAGES)

        if has_fail:
            # Promote to FAIL only when origin is confirmed first-party.
            # Unknown origin → WARN (mirrors Java's "false positives from
            # third-party assets are more costly than missed warnings").
            confirmed_1p = (
                event.source_origin is not None
                and _is_first_party(event.source_origin)
            )
            return Severity.FAIL if confirmed_1p else Severity.WARN

        return Severity.WARN

    # ── Queries ───────────────────────────────────────────────────────

    def fatal_count(self) -> int:
        """Count of events classified as FAIL (apples-to-apples with Java)."""
        return sum(1 for e in self.events if self.classify(e) == Severity.FAIL)

    @property
    def fatal_events(self) -> list[ConsoleEvent]:
        return [e for e in self.events if self.classify(e) == Severity.FAIL]

    @property
    def errors(self) -> list[dict[str, Any]]:
        """All events as dicts, with classification — for assertion messages."""
        return [
            {
                "type": e.type,
                "text": e.text,
                "location": e.location,
                "origin": e.source_origin,
                "severity": self.classify(e).value,
            }
            for e in self.events
        ]

    def reset(self) -> None:
        self.events.clear()


# ── Origin helpers (mirror Java private methods) ─────────────────────

def _extract_hostname(url: str) -> str:
    """Strip protocol + path, return hostname only."""
    try:
        s = re.sub(r"^https?://", "", url)
        slash = s.find("/")
        return s[:slash] if slash >= 0 else s
    except Exception:
        return url


def _is_first_party(hostname: str) -> bool:
    if not hostname:
        return False
    lower = hostname.lower()
    return any(d.lower() in lower for d in FIRST_PARTY_DOMAINS)


def _is_third_party(hostname: str) -> bool:
    if not hostname:
        return False
    if _is_first_party(hostname):
        return False
    lower = hostname.lower()
    return any(o.lower() in lower for o in IGNORE_ORIGINS)
