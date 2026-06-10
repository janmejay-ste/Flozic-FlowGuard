"""
Brand + URL constants. Mirrors the Java `BrandText` + `UrlRegistry`
classes minimally — only the values that the migrated tests need.

The values match the Java defaults (Java sources of truth):
  - BrandText.PRODUCT_NAME       = "Flozic"
  - UrlRegistry.MARKETING_BASE   = "https://www.flozic.ai"
  - UrlRegistry.AUTH_BASE        = "https://accounts.appypie.com"
  - UrlRegistry.CONNECT_BASE     = "https://connectcloud.appypie.com"

If any of these change on the Java side, this file must change too.
The cross-stack contract is documented in
docs/snapshot-contract.md (for the snapshot schema) and in the Java
config classes (for these URLs).
"""

from __future__ import annotations

import os

# ── Brand ─────────────────────────────────────────────────────────────
PRODUCT_NAME = os.environ.get("BRAND_PRODUCT_NAME", "Flozic")
PRODUCT_NAME_LEGACY = "Appy Pie Automate"

# ── Surface URLs ──────────────────────────────────────────────────────
MARKETING_BASE = os.environ.get("MARKETING_BASE_URL", "https://www.flozic.ai")
AUTH_BASE = "https://accounts.appypie.com"
CONNECT_BASE = "https://connectcloud.appypie.com"


def integrate_path(slug: str) -> str:
    """
    Build a marketing sub-page URL. All flozic.ai sub-pages live under
    /integrate/{slug} (pricing-plan, app-directory, features, etc.).
    Mirrors Java UrlRegistry.integratePath().
    """
    slug = slug.strip().lstrip("/")
    return f"{MARKETING_BASE}/integrate/{slug}"


def marketing_hostname() -> str:
    """Return the bare hostname from MARKETING_BASE (e.g. 'www.flozic.ai')."""
    import re
    return re.sub(r"^https?://", "", MARKETING_BASE).rstrip("/")


def is_current_marketing_host(url: str) -> bool:
    """Strict: only accepts the CURRENT marketing host. Mirrors Java UrlRegistry.isCurrentMarketingHost()."""
    if not url:
        return False
    return marketing_hostname().lower() in url.lower()


def is_owned_marketing_host(url: str) -> bool:
    """
    Accepts both flozic.ai (current) and appypieautomate.ai (legacy).
    Mirrors Java UrlRegistry.isOwnedMarketingHost() — used during the
    rebrand transition window.
    """
    if not url:
        return False
    lower = url.lower()
    return "flozic.ai" in lower or "appypieautomate.ai" in lower
