"""
pages/mobile/mobile_devices.py

Central registry of device profiles the mobile branch audits against.

The root conftest.py's `browser_instance` fixture is session-scoped and
hand-built (no `pytest-playwright` plugin, no `playwright` fixture exposed),
so we can't reach `playwright.devices[...]` the way the plugin would let
us. Instead each entry here is a plain dict of the exact kwargs
`Browser.new_context(**profile)` accepts — values are close approximations
of Playwright's built-in device presets (viewport, DPR, UA, touch).

Chosen to cover: small iOS (SE), mainstream iOS (13), large iOS (14 Pro
Max), mainstream Android (Pixel 5), a different-aspect-ratio Android
(Galaxy S9+), and one tablet as a control case.
"""

_IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
_ANDROID_PHONE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.6367.82 Mobile Safari/537.36"
)
_ANDROID_S9_UA = (
    "Mozilla/5.0 (Linux; Android 9; SM-G965F) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.6367.82 Mobile Safari/537.36"
)
_IPAD_UA = (
    "Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

MOBILE_DEVICE_PROFILES: dict[str, dict] = {
    "iPhone SE": {
        "viewport": {"width": 375, "height": 667},
        "device_scale_factor": 2,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": _IOS_UA,
    },
    "iPhone 13": {
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": _IOS_UA,
    },
    "iPhone 14 Pro Max": {
        "viewport": {"width": 430, "height": 932},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": _IOS_UA,
    },
    "Pixel 5": {
        "viewport": {"width": 393, "height": 851},
        "device_scale_factor": 2.75,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": _ANDROID_PHONE_UA,
    },
    "Galaxy S9+": {
        "viewport": {"width": 320, "height": 658},
        "device_scale_factor": 4.5,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": _ANDROID_S9_UA,
    },
}

TABLET_DEVICE_PROFILES: dict[str, dict] = {
    "iPad Mini": {
        "viewport": {"width": 768, "height": 1024},
        "device_scale_factor": 2,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": _IPAD_UA,
    },
}

ALL_DEVICE_PROFILES: dict[str, dict] = {**MOBILE_DEVICE_PROFILES, **TABLET_DEVICE_PROFILES}

MOBILE_DEVICE_NAMES = list(MOBILE_DEVICE_PROFILES.keys())
TABLET_DEVICE_NAMES = list(TABLET_DEVICE_PROFILES.keys())
ALL_DEVICE_NAMES = list(ALL_DEVICE_PROFILES.keys())


def device_id(name: str) -> str:
    """pytest test-id-friendly slug, e.g. 'iPhone 14 Pro Max' -> 'iPhone_14_Pro_Max'."""
    return name.replace(" ", "_").replace("+", "plus")