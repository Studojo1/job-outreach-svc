"""One malformed lead in a justification batch must cost only that lead.

The batch used to be validated against the strict per-lead schema as a whole,
so a single short headline ("'oorja' is too short") failed all 12 leads, and
the retries tripped on the same field.
"""
import pathlib
import sys
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import services.lead_scoring.llm_justifier as j

GOOD = {"headline": "Your React work fits Acme's dashboard team",
        "bullets": ["Acme ships a React admin", "You built a React dashboard", "Both in Bengaluru"],
        "signal_strength": "high"}


def test_batch_schema_has_no_length_limits():
    schema = j._build_batch_schema([1])
    lead = schema["properties"]["1"]
    assert "minLength" not in str(lead) and "maxItems" not in str(lead)


def test_bad_lead_is_dropped_alone():
    leads = [{"id": 1, "company": "Acme"}, {"id": 2, "company": "Oorja"}, {"id": 3, "company": "Beta"}]
    llm_out = {
        "1": GOOD,
        "2": {**GOOD, "headline": "oorja"},            # too short
        "3": {**GOOD, "bullets": GOOD["bullets"][:2]},  # too few bullets
    }
    with mock.patch.object(j, "generate_json", return_value=llm_out), \
         mock.patch.object(j, "_build_batch_prompt", return_value="p"), \
         mock.patch.object(j, "_build_company_snapshot", return_value=""):
        out = j._justify_batch({}, leads, {})
    assert set(out) == {1}


def test_banned_phrase_leads_are_retried_once():
    """UC-Q22: a lead dropped for a banned phrase gets one more draw."""
    leads = [{"id": 1, "company": "Acme"}, {"id": 2, "company": "Beta"}]
    banned = {**GOOD, "headline": "Acme is a strong fit for your skills"}
    calls = []

    def fake(prompt, schema, **kw):
        calls.append(sorted(schema["properties"]))
        # First call: lead 2 trips the filter. Retry: clean.
        return {"1": GOOD, "2": banned} if len(calls) == 1 else {"2": GOOD}

    with mock.patch.object(j, "generate_json", side_effect=fake), \
         mock.patch.object(j, "_build_batch_prompt", return_value="p"), \
         mock.patch.object(j, "_build_company_snapshot", return_value=""):
        out = j._justify_batch({}, leads, {})
    assert set(out) == {1, 2}
    assert calls == [["1", "2"], ["2"]]  # the retry asks only for the dropped lead


def test_banned_phrase_twice_is_not_retried_forever():
    leads = [{"id": 1, "company": "Acme"}]
    banned = {**GOOD, "headline": "Acme is a strong fit for your skills"}
    with mock.patch.object(j, "generate_json", return_value={"1": banned}) as gen, \
         mock.patch.object(j, "_build_batch_prompt", return_value="p"), \
         mock.patch.object(j, "_build_company_snapshot", return_value=""):
        out = j._justify_batch({}, leads, {})
    assert out == {}
    assert gen.call_count == 2
