"""
Exceptions that mean "the harness is misconfigured", not "the product is broken".

WHY THIS EXISTS
---------------
Health scoring attributes a failure by the test's declared feature, so a
harness fault inside a product-feature test scored as a product failure. One
unset env var produced `Product: 0` — a report saying the application is
critically broken when nothing had been exercised at all. That is the same
class of false verdict as the auth-host migration's 8 phantom PRODUCT_BUGs,
and it is worse than a missing signal because someone acts on it.

Raising one of these still FAILS the test — the suite goes red, as it should.
What changes is attribution: the failure is excluded from product health and
counted separately, so the dashboard says "the harness could not run" instead
of "your product is down".
"""

from __future__ import annotations


class HarnessError(Exception):
    """Base: something about the test environment is wrong, not the product."""


class HarnessConfigError(HarnessError):
    """Missing or invalid configuration — unset credentials, absent env vars.

    Deliberately not a subclass of AssertionError: pytest formats those as
    assertion failures, which is exactly the framing to avoid here.
    """


# ── Connectivity loss ──────────────────────────────────────────────────
#
# A dropped network is the same category of non-signal as an unset env var:
# the check never reached the product, so attributing the failure to the
# product is a lie. It needs its own detection because it does NOT arrive as
# one of the exceptions above -- Playwright raises its own Error carrying a
# Chromium network code.
#
# Evidenced by a real run: an overnight full-suite run lost WiFi and produced
# 15 net::ERR_INTERNET_DISCONNECTED failures across unrelated tests, which
# would have scored as a catastrophic product regression.
#
# String matching is justified here, unlike for the exceptions above, because
# these are stable Chromium error CODES rather than human-written prose. They
# do not get reworded.
CONNECTIVITY_ERROR_MARKERS = (
    "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_NETWORK_CHANGED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_ADDRESS_UNREACHABLE",
    "net::ERR_PROXY_CONNECTION_FAILED",
)


def looks_like_connectivity_loss(text: str | None) -> bool:
    """True when an error text carries a Chromium connectivity failure code.

    Deliberately narrow: it does NOT match timeouts. A timeout can equally be
    a slow product or a genuine hang, and calling that infrastructure would
    hide real defects -- the opposite mistake, and the more dangerous one.
    """
    t = text or ""
    return any(m in t for m in CONNECTIVITY_ERROR_MARKERS)
