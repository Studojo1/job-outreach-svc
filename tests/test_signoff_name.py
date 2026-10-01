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


# Ticket #40, again: two emails written on 30 Sep, before the fix, were stored
# and sent on 1 Oct still signed "A". The sign-off is now repaired at send time.

NIHAL_BODY = (
    "Hi Meghna,\n\nI built two platforms recently.\n\n"
    "Would you know if there's an opening, or who on the product side to talk to?\n\nA"
)


@pytest.mark.parametrize("body,expected_last", [
    (NIHAL_BODY, "A J Mohamed Nihal"),
    ("Hi,\n\nNote.\n\nCheers,\nMe", "A J Mohamed Nihal"),
    ("Hi,\n\nNote.\n\nthere", "A J Mohamed Nihal"),
    ("Hi,\n\nNote.\n\nBest,\nA.", "A J Mohamed Nihal"),
])
def test_repair_signoff_fixes_names_that_name_nobody(db, body, expected_last):
    db.get(Candidate, 1).parsed_json = {"personal_info": {"name": "A J MOHAMED NIHAL"}}
    db.commit()
    out = gen.repair_signoff(body, db.get(Candidate, 1))
    assert out.splitlines()[-1] == expected_last
    assert out.splitlines()[:-1] == body.rstrip().splitlines()[:-1]


@pytest.mark.parametrize("body", [
    "Hi,\n\nNote.\n\nBest,\nRuchika",
    "Hi,\n\nThanks again for your time, really appreciate it.",
    "",
])
def test_repair_signoff_leaves_good_bodies_alone(db, body):
    assert gen.repair_signoff(body, db.get(Candidate, 1)) == body


def test_prewritten_body_is_repaired_before_it_is_sent(db, monkeypatch):
    """Drive the real send loop with his stored 30 Sep body."""
    from datetime import timedelta

    from database.models import Campaign, CreditLedger, EmailAccount, EmailSent, OutreachOrder, PaymentOrder
    from database.models import LeadScore, SuppressedEmail, UserCredit
    from services.email_campaign import campaign_worker

    engine = db.get_bind()
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        LeadScore, Campaign, EmailAccount, EmailSent, OutreachOrder, PaymentOrder,
        UserCredit, CreditLedger, SuppressedEmail)])
    db.get(Candidate, 1).parsed_json = {"personal_info": {"name": "A J MOHAMED NIHAL"}}
    db.add_all([
        EmailAccount(id=5, user_id="u", email_address="aj@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r",  # noqa: S106
                     token_expiry=datetime.utcnow() + timedelta(days=1)),
        UserCredit(user_id="u", total_credits=200, used_credits=200),
        Campaign(id=142, candidate_id=1, email_account_id=5, name="c", status="running", daily_limit=20,
                 user_timezone="Asia/Kolkata", credits_reserved=3, credits_released=0),
        EmailSent(campaign_id=142, lead_id=1, to_email="meghna@x.ai", subject="quick question Meghna",
                  body=NIHAL_BODY, status="queued", scheduled_at=datetime.utcnow() - timedelta(minutes=1)),
    ])
    db.commit()
    sent = []
    monkeypatch.setattr(campaign_worker, "send_gmail_email", lambda **kw: sent.append(kw) or {"id": "m", "threadId": "t"})
    monkeypatch.setattr(campaign_worker, "ph_capture", lambda *a, **k: None)
    monkeypatch.setattr(campaign_worker, "_deferred_to_send_window", lambda c, now: None)

    campaign_worker._send_ready(db)

    assert len(sent) == 1
    assert sent[0]["body"].splitlines()[-1] == "A J Mohamed Nihal"


# Found in a dry run over every production email: one account name is a
# username, so "there" would have been "repaired" to "faizannmohammad0016".

def test_username_account_name_is_never_used_to_sign(db, prompts):
    db.get(User, "u").name = "faizannmohammad0016"
    db.commit()
    gen.generate_email_for_lead(db.get(Lead, 1), db.get(Candidate, 1), "warm_intro")
    assert "faizannmohammad0016" not in prompts[0]
    assert 'sign off with "Best,"' in prompts[0]


def test_repair_drops_the_line_when_there_is_no_real_name(db):
    db.get(User, "u").name = "faizannmohammad0016"
    db.commit()
    body = "Hi Pratistha,\n\nJust bumping this up.\n\nWould you know who I should reach out to?\nthere"
    out = gen.repair_signoff(body, db.get(Candidate, 1))
    assert out == "Hi Pratistha,\n\nJust bumping this up.\n\nWould you know who I should reach out to?"


def test_repair_never_empties_a_body(db):
    db.get(User, "u").name = ""
    db.commit()
    assert gen.repair_signoff("Me", db.get(Candidate, 1)) == "Me"


@pytest.mark.parametrize("touch", [1, 2])
def test_followups_never_sign_with_a_username(db, prompts, touch):
    db.get(User, "u").name = "faizannmohammad0016"
    db.commit()
    gen.generate_followup_email(db.get(Lead, 1), db.get(Candidate, 1), "Hi Pratistha, earlier note.", touch)
    assert "faizannmohammad0016" not in prompts[0]
