"""Multi-source hardening rails: age normalization + cross-source fingerprinting."""
from datetime import datetime, timezone

from services.mesa.postparse import posted_age_days
from services.mesa.runner import _fingerprint

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


def test_age_iso_date():
    # ISO dates carry no time-of-day; age is fractional days from midnight UTC
    age = posted_age_days("2026-07-07", now=NOW)
    assert age is not None and 2.0 <= age <= 3.0


def test_age_relative_formats():
    assert posted_age_days("3 days ago", now=NOW) == 3.0
    assert posted_age_days("2 weeks ago", now=NOW) == 14.0
    assert posted_age_days("just now", now=NOW) == 0.0
    assert posted_age_days("yesterday", now=NOW) == 1.0
    assert posted_age_days("30+ days ago", now=NOW) == 30.0
    assert posted_age_days("5h", now=NOW) == 5 / 24.0


def test_age_unknown_is_none():
    assert posted_age_days("", now=NOW) is None
    assert posted_age_days(None, now=NOW) is None
    assert posted_age_days("recently", now=NOW) is None


def test_fingerprint_collapses_source_variants():
    # same job seen as "Fi Money" (post) and "Fi Money Pvt Ltd" (board)
    a = _fingerprint("Fi Money", "AI Engineering Intern")
    b = _fingerprint("Fi Money Pvt Ltd", "AI Engineering Intern!!")
    assert a and a == b


def test_fingerprint_distinct_roles_differ():
    assert _fingerprint("Acme", "Backend Intern") != _fingerprint("Acme", "Frontend Intern")


def test_fingerprint_empty_safe():
    assert _fingerprint("", "Backend Intern") == ""
