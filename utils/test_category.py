"""
Python equivalent of the Java @TestCategory annotation. Stores
metadata about a test (suite type, login requirement, feature label)
and applies pytest marks so the same -m / suite-filtering UX works.

Usage:
    @test_category(type="FULL", requires_login=True, feature="Sanity Journey")
    class TestAuthenticatedSanityJourney:
        def test_sanity_journey(self, page):
            ...

The metadata is attached to the class as `_test_category` so conftest.py
fixtures can read it during teardown for analytics.
"""

from dataclasses import dataclass
from typing import Callable, TypeVar

import pytest

T = TypeVar("T")


@dataclass(frozen=True)
class TestCategoryMeta:
    """Mirror of the Java TestCategory annotation fields."""
    type: str           # "SMOKE" | "SANITY" | "REGRESSION" | "FULL"
    requires_login: bool = False
    feature: str = "General"
    owner: str = ""
    severity: str = "NORMAL"


def test_category(
    type: str,
    requires_login: bool = False,
    feature: str = "General",
    owner: str = "",
    severity: str = "NORMAL",
) -> Callable[[T], T]:
    """
    Class decorator. Stores TestCategoryMeta on the class and applies
    the corresponding pytest mark so -m smoke / -m sanity / etc. filters
    work as expected.
    """
    meta = TestCategoryMeta(
        type=type,
        requires_login=requires_login,
        feature=feature,
        owner=owner,
        severity=severity,
    )

    def decorator(cls: T) -> T:
        # Attach metadata for conftest.py fixtures to read.
        cls._test_category = meta  # type: ignore[attr-defined]

        # Apply pytest mark matching the suite type so -m filtering works.
        mark_name = type.lower()
        mark = getattr(pytest.mark, mark_name)
        cls = mark(cls)

        if requires_login:
            cls = pytest.mark.requires_login(cls)

        return cls

    return decorator


# Tell pytest this function is NOT a test itself, despite the test_ prefix.
# The name pairs intentionally with Java's @TestCategory annotation —
# renaming would make every Python test diverge from its Java equivalent
# for no real gain.
test_category.__test__ = False  # type: ignore[attr-defined]
