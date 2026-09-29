"""B2C open items UC-Q06 and UC-Q02 (lead discovery).

- UC-Q06: the generator filtered hiring managers by the student's target roles
  as past titles and by the student's city as the company HQ, so the original
  filters matched nobody on 6 of 6 production runs; loosening then dropped the
  currently-hiring signal first.
- UC-Q02: an Apollo 429/5xx/timeout came back as an empty page, so the student
  was told their search was "very narrow".
"""
import pathlib
import sys

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.lead_calibration import filter_generator_service as fg
from services.lead_discovery import lead_collector_service as lc
from services.shared.schemas.candidate_schema import CandidateProfile
from services.shared.schemas.filter_schema import LeadFilter
from services.shared.schemas.target_segment_schema import TargetSegment


def _profile():
    return CandidateProfile(
        user_id="u", name="Priya", location_preferences=["Bangalore"], skills=["python"],
        experience_level="student", preferred_roles=["Software Engineer Intern"],
        role_seniority_target=["manager"],
        company_preferences={"niche_keywords": ["fintech"]},
        work_preferences={"work_mode": "hybrid"},
    )


def test_generator_no_longer_sets_past_titles_or_company_hq():
    f = fg._generate_filters_from_strategy(
        {"person_seniorities": ["manager"], "keyword_strategy": ["fintech"]}, _profile())
    assert f.person_past_titles is None
    assert f.organization_locations is None
    assert f.person_locations  # the hiring manager's own location still applies
    assert f.q_organization_job_titles == ["Software Engineer Intern"]  # hiring signal kept


def _full_filter():
    return LeadFilter(
        target_segments=[TargetSegment(company_size_range="1,200", person_titles=["Engineering Manager"])],
        person_locations=["Bangalore, India"], organization_locations=["Bangalore, India"],
        q_organization_job_titles=["Software Engineer Intern"],
        organization_job_posted_at_range={"min": "2026-08-01", "max": "2026-09-29"},
        q_organization_keyword_tags=["fintech"], currently_using_any_of_technology_uids=["python"],
        person_past_titles=["x"], organization_job_locations=["Bangalore, India"],
    )


def test_hiring_signal_is_the_last_optional_filter_dropped():
    stages = lc._build_loosening_stages(_full_filter())
    first_without_hiring = next(i for i, s in enumerate(stages) if s.q_organization_job_titles is None)
    first_without_niche = next(i for i, s in enumerate(stages) if s.q_organization_keyword_tags is None)
    first_without_hq = next(i for i, s in enumerate(stages) if s.organization_locations is None)
    assert first_without_hq == 0
    assert first_without_hq < first_without_niche < first_without_hiring
    # cumulative: once dropped, stays dropped
    assert all(s.q_organization_keyword_tags is None for s in stages[first_without_niche:])
    assert all(s.organization_job_posted_at_range is None for s in stages[first_without_hiring:])


def _http_error(code):
    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(response=resp)


def test_rate_limit_retries_once_then_says_apollo_is_unavailable(monkeypatch):
    calls = []
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    monkeypatch.setattr(lc, "search_people_chunked", lambda p: calls.append(1) or (_ for _ in ()).throw(_http_error(429)))
    with pytest.raises(lc.ApolloUnavailableError):
        lc._try_collect_page({})
    assert len(calls) == 2


def test_a_retry_that_succeeds_returns_people(monkeypatch):
    seq = [_http_error(503), {"people": [{"id": "p1"}]}]
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)

    def fake(p):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(lc, "search_people_chunked", fake)
    assert lc._try_collect_page({}) == [{"id": "p1"}]


def test_a_bad_request_is_still_an_empty_page(monkeypatch):
    monkeypatch.setattr(lc, "search_people_chunked", lambda p: (_ for _ in ()).throw(_http_error(422)))
    assert lc._try_collect_page({}) == []


def test_outage_mid_run_keeps_what_was_collected(monkeypatch):
    class _Keys:
        def has_valid_key(self):
            return True
    import services.shared.apollo_key_manager as km
    monkeypatch.setattr(km, "apollo_keys", _Keys())

    def fake_paginate(filters, candidate_id, target_leads, db, collected, excluded_companies=None):
        if collected == 0:
            return 120  # original filters found some
        e = lc.ApolloTransientError("429")
        e.collected = collected + 30
        raise e
    monkeypatch.setattr(lc, "_paginate_filters", fake_paginate)
    assert lc.collect_leads(_full_filter(), 1, 800, db=None) == 150


def test_outage_before_anything_is_found_is_reported(monkeypatch):
    class _Keys:
        def has_valid_key(self):
            return True
    import services.shared.apollo_key_manager as km
    monkeypatch.setattr(km, "apollo_keys", _Keys())

    def fake_paginate(*a, **k):
        e = lc.ApolloTransientError("503")
        e.collected = 0
        raise e
    monkeypatch.setattr(lc, "_paginate_filters", fake_paginate)
    with pytest.raises(lc.ApolloUnavailableError):
        lc.collect_leads(_full_filter(), 1, 800, db=None)
