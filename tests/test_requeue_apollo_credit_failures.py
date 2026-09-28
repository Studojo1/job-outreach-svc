"""scripts/requeue_apollo_credit_failures: emails killed by the Apollo credit
outage go back in the queue without handing anyone extra emails or credits."""
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import (
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore, User, UserCredit,
)
from scripts import requeue_apollo_credit_failures as job


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 28, 12, 0, 0)
NO_MATCH = "Apollo could not find email for this contact"
EXHAUSTED = "Enrichment error: All Apollo API keys are exhausted. Add more credits or a new key."
OUTAGE = datetime(2026, 9, 27, 10, 0)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, UserCredit, CreditLedger)])
    s = sessionmaker(bind=engine)()
    s.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r", token_expiry=NOW + timedelta(days=9)),  # noqa: S106
        # 10 reserved, 1 returned for the failed email below.
        UserCredit(user_id="u", total_credits=10, used_credits=9),
        Campaign(id=10, candidate_id=1, email_account_id=5, name="live", status="running",
                 credits_reserved=10, credits_released=1),
        Campaign(id=11, candidate_id=1, email_account_id=5, name="done", status="completed",
                 credits_reserved=10, credits_released=1),
    ])
    s.commit()
    yield s
    s.close()


def _row(db, campaign=10, status="failed", msg=NO_MATCH, scheduled=OUTAGE, **kw):
    lead = Lead(candidate_id=1, name="L", company="C", enrichment_fail_count=3)
    db.add(lead)
    db.flush()
    row = EmailSent(campaign_id=campaign, lead_id=lead.id, status=status, error_message=msg,
                    enrichment_status="skipped" if status == "failed" else "pending",
                    scheduled_at=scheduled, **kw)
    db.add(row)
    db.commit()
    return row


def _wallet(db):
    return db.query(UserCredit).filter_by(user_id="u").one()


def test_refunded_email_is_requeued_and_its_credit_taken_back(db):
    e = _row(db, msg=EXHAUSTED)

    job.run(db, apply=True)

    db.refresh(e)
    assert (e.status, e.enrichment_status, e.error_message) == ("pending_enrichment", "pending", None)
    assert db.get(Lead, e.lead_id).enrichment_fail_count == 0
    assert _wallet(db).used_credits == 10
    assert e.scheduled_at > datetime.utcnow() - timedelta(minutes=1)  # re-planned, not overdue


def test_replaced_email_takes_its_slot_back_from_the_unsent_replacement(db):
    e = _row(db)
    repl = _row(db, status="pending_enrichment", msg=None, replacement_for_id=e.id,
                replacement_reason="enrichment_exhausted")
    repl_id = repl.id

    job.run(db, apply=True)

    db.refresh(e)
    assert e.status == "pending_enrichment"
    assert db.get(EmailSent, repl_id) is None
    assert _wallet(db).used_credits == 9  # no credit was returned for it, none is taken


def test_slot_already_used_by_a_sent_replacement_is_left_alone(db):
    e = _row(db)
    _row(db, status="sent", msg=None, replacement_for_id=e.id, replacement_reason="enrichment_exhausted")

    job.run(db, apply=True)

    db.refresh(e)
    assert e.status == "failed"


def test_ordinary_failures_and_finished_campaigns_are_not_touched(db):
    before_outage = _row(db, scheduled=OUTAGE - timedelta(days=3))
    other_error = _row(db, msg="Gmail send failed: 404")
    finished = _row(db, campaign=11, msg=EXHAUSTED)

    job.run(db, apply=True)

    for e in (before_outage, other_error, finished):
        db.refresh(e)
        assert e.status == "failed"
    assert _wallet(db).used_credits == 9


def test_no_credit_left_means_no_free_email(db):
    e = _row(db, msg=EXHAUSTED)
    _wallet(db).used_credits = 10  # the returned credit was already spent elsewhere
    db.commit()

    report = job.run(db, apply=True)

    db.refresh(e)
    assert e.status == "failed"
    assert report[10]["skipped_no_credit"] == 1


def test_dry_run_changes_nothing(db):
    e = _row(db, msg=EXHAUSTED)
    repl = _row(db, status="queued", msg=None, replacement_for_id=_row(db).id)

    report = job.run(db, apply=False)

    db.refresh(e)
    assert e.status == "failed"
    assert db.get(EmailSent, repl.id) is not None
    assert _wallet(db).used_credits == 9
    assert report[10]["requeued"] == 2
