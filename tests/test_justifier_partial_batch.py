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
