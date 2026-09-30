"""Outreach mail must never be signed "Me".

Ruchika Goel, ticket #39 (campaign 141): a cold email went to a recruiter
from her own Gmail signed

    Best,
    Me

The sign-off takes the first name from the parsed resume, falling back to the
account name, then to the literal "Me". Test launch and the launch preview
called the generator without the account name, so a resume whose name the
parser missed (or rejected as a heading) went straight to "Me". Follow-ups had
the same shape, falling back to "there" and never consulting the account.

These tests run the real generator and follow-up code with only the LLM call
stubbed, and assert on the sign-off the model is told to write.
"""
import pathlib
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Candidate, Lead, User
from services.email_campaign import email_generator_service as gen

NOW = datetime(2026, 9, 28)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (User, Candidate, Lead)])
    session = sessionmaker(bind=engine)()
    session.add_all([
        User(id="u", email="ruchikagoel97@gmail.com", name="Ruchika Goel",
             email_verified=True, created_at=NOW, updated_at=NOW),
        # The parser picked a heading, which is rejected as a name.
        Candidate(id=1, user_id="u", resume_text=".",
                  parsed_json={"personal_info": {"name": "PRODUCT MANAGEMENT STUDENT"}}),
        Lead(id=1, candidate_id=1, name="Pratistha Sharma", company="Redrob",
             title="Brand Marketing Lead"),
    ])
    session.commit()
    yield session
    session.close()


@pytest.fixture()
def prompts(monkeypatch):
    seen = []

    def fake_generate_json(prompt, schema, **kw):
        seen.append(prompt)
        if "subject" in schema["properties"]:
            return {"subject": "quick question", "body": "Hi Pratistha,\n\nA note.\n\nBest,\nRuchika"}
        return {"body": "Hi Pratistha,\n\nJust bumping this up.\n\nRuchika"}

    monkeypatch.setattr(gen, "generate_json", fake_generate_json)
    return seen


def test_test_launch_path_signs_with_account_name(db, prompts):
    """Test launch calls the generator with no user_name, as in the ticket."""
    cand, lead = db.get(Candidate, 1), db.get(Lead, 1)
    gen.generate_email_for_lead(lead, cand, "warm_intro")
    assert "Ruchika" in prompts[0]
    assert '\nMe"' not in prompts[0] and '"Me"' not in prompts[0]


def test_no_name_anywhere_drops_the_name_instead_of_me(db, prompts):
    db.get(User, "u").name = ""
    db.commit()
    cand, lead = db.get(Candidate, 1), db.get(Lead, 1)
    gen.generate_email_for_lead(lead, cand, "warm_intro")
    assert '\nMe"' not in prompts[0] and '"Me"' not in prompts[0]
    assert 'sign off with "Best,"' in prompts[0]


@pytest.mark.parametrize("touch", [1, 2])
def test_followups_sign_with_account_name(db, prompts, touch):
    cand, lead = db.get(Candidate, 1), db.get(Lead, 1)
    gen.generate_followup_email(lead, cand, "Hi Pratistha, earlier note.", touch)
    assert 'Sign off: "Ruchika"' in prompts[0]
    assert '"there"' not in prompts[0]


def test_resume_name_still_wins_over_account_name(db, prompts):
    cand = db.get(Candidate, 1)
    cand.parsed_json = {"personal_info": {"name": "Ruchika Goel R"}}
    db.get(User, "u").name = "Goel Ruchika"
    db.commit()
    gen.generate_email_for_lead(db.get(Lead, 1), cand, "warm_intro")
    assert "Ruchika" in prompts[0] and "Goel," not in prompts[0]


# Ticket #40: "A J Mohamed Nihal" signed his emails as "A".

@pytest.mark.parametrize("name,expected", [
    ("A J Mohamed Nihal", "A J Mohamed Nihal"),
    ("A. J. Mohamed Nihal", "A. J. Mohamed Nihal"),
    ("Ruchika Goel R", "Ruchika"),
    ("Priya", "Priya"),
    ("", ""),
])
def test_signoff_name_keeps_names_that_start_with_an_initial(name, expected):
    assert gen._signoff_name(name) == expected


def test_initial_first_name_signs_email_with_full_name(db, prompts):
    db.get(Candidate, 1).parsed_json = {"personal_info": {"name": "A J MOHAMED NIHAL"}}
    db.commit()
    gen.generate_email_for_lead(db.get(Lead, 1), db.get(Candidate, 1), "warm_intro")
    assert "A J Mohamed Nihal" in prompts[0]
    assert '"Best,\nA"' not in prompts[0] and '"A"' not in prompts[0]


@pytest.mark.parametrize("touch", [1, 2])
def test_initial_first_name_signs_followups_with_full_name(db, prompts, touch):
    db.get(Candidate, 1).parsed_json = {"personal_info": {"name": "A J Mohamed Nihal"}}
    db.commit()
    gen.generate_followup_email(db.get(Lead, 1), db.get(Candidate, 1), "Hi Pratistha, earlier note.", touch)
    assert 'Sign off: "A J Mohamed Nihal"' in prompts[0]
