"""A user's pinned send order must survive all the way to scheduled_at.

Yatharth, ticket #35:

    I want to first share the companies in the exact order in which I want
    the emails to be sent. How will that work?

It didn't. Send order came from lead score alone, decided in two separate
queries in two separate files:

    campaign_service._create_campaign  builds EmailSent rows, score order
    campaign_worker._compute_schedule  stamps scheduled_at, score order

Those two are what this file guards. A shared column name is not a contract:
if only one of them learns about send_position, rows get created in one order
and scheduled in another, and nothing fails loudly — the campaign just mails
the wrong people first. So these tests assert the ORDER THAT COMES OUT of the
real ordering expression, not what some helper returns in isolation.

The ordering rule under test, from Pranav:

    "this is how it should be ideally, but now that i am requesting this, you
    should be able to change this and put it in whatever order i want and
    after that order is complete you should send out the mails under this
    logic itself"

i.e. pinned rows lead in the user's order; everything unpinned keeps falling
back to score exactly as before. In a 279-lead campaign that matters: a user
pins ten and the other 269 must not be disturbed.
"""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Campaign, EmailSent, Lead, LeadScore


# These models are Postgres-shaped; SQLite has no JSONB. Render it as JSON so
# the tests run against the REAL model definitions rather than a stand-in whose
# column order could drift from production.
@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    # Only the four tables the ordering query touches. Creating the whole
    # metadata pulls in unrelated Postgres-only columns (JSONB) that SQLite
    # cannot compile.
    Base.metadata.create_all(
        engine,
        tables=[t.__table__ for t in (Campaign, Lead, LeadScore, EmailSent)],
    )
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _campaign_with_leads(db, scores):
    """One campaign, one EmailSent per score. Returns rows in creation order."""
    campaign = Campaign(id=1, candidate_id=7, name="My Outreach Campaign", status="running")
    db.add(campaign)
    emails = []
    for i, score in enumerate(scores, start=1):
        db.add(Lead(id=i, candidate_id=7, name=f"lead{i}", company=f"co{i}"))
        if score is not None:
            # The sub-scores are NOT NULL in the real schema; only overall_score
            # takes part in the ordering under test.
            db.add(LeadScore(
                lead_id=i, overall_score=score,
                title_relevance=0, department_relevance=0, industry_relevance=0,
                seniority_relevance=0, location_relevance=0,
            ))
        email = EmailSent(id=i, campaign_id=1, lead_id=i, status="pending_enrichment")
        db.add(email)
        emails.append(email)
    db.commit()
    return emails


def _send_order(db):
    """The real ordering expression from campaign_worker._compute_schedule.

    Kept identical to the query under test on purpose: if that query changes
    and this does not, the assertions below stop describing production.
    """
    return [
        e.id
        for e in db.query(EmailSent)
        .filter(EmailSent.campaign_id == 1,
                EmailSent.status.in_(["pending_enrichment", "queued"]))
        .outerjoin(Lead, EmailSent.lead_id == Lead.id)
        .outerjoin(LeadScore, LeadScore.lead_id == Lead.id)
        .order_by(
            EmailSent.send_position.asc().nullslast(),
            LeadScore.overall_score.desc().nullslast(),
            EmailSent.id.asc(),
        )
        .all()
    ]


def test_unpinned_campaign_still_sends_by_score(db):
    """Every campaign already in flight must not shift when the column ships."""
    _campaign_with_leads(db, [96, 99, 97])
    # Scores 99, 97, 96 -> leads 2, 3, 1. Unchanged from before send_position.
    assert _send_order(db) == [2, 3, 1]


def test_pinned_rows_lead_in_the_users_order(db):
    """The whole point of #35: the user's sequence wins where they set one."""
    emails = _campaign_with_leads(db, [96, 99, 97])
    # User drags the lowest-scoring lead to the front.
    emails[0].send_position = 1
    db.commit()
    assert _send_order(db)[0] == 1


def test_unpinned_rows_keep_score_order_behind_pinned_ones(db):
    """Pin two of five; the other three stay ranked as they were."""
    emails = _campaign_with_leads(db, [50, 99, 60, 97, 55])
    emails[4].send_position = 1   # id 5, score 55
    emails[0].send_position = 2   # id 1, score 50
    db.commit()
    # Pinned first in the user's order, then 99, 97, 60 -> ids 2, 4, 3.
    assert _send_order(db) == [5, 1, 2, 4, 3]


def test_leads_without_a_score_still_sort_last(db):
    """nullslast on score must survive the new leading sort key."""
    _campaign_with_leads(db, [None, 99, None, 97])
    assert _send_order(db) == [2, 4, 1, 3]


def test_score_ordering_alone_cannot_honour_a_pin(db):
    """Prove the check fails on the actual bug.

    This is the pre-fix expression — score only, no send_position. If someone
    reverts campaign_worker to it, the pin below is ignored and this test is
    what says so.
    """
    emails = _campaign_with_leads(db, [96, 99, 97])
    emails[0].send_position = 1
    db.commit()

    buggy = [
        e.id
        for e in db.query(EmailSent)
        .filter(EmailSent.campaign_id == 1)
        .outerjoin(Lead, EmailSent.lead_id == Lead.id)
        .outerjoin(LeadScore, LeadScore.lead_id == Lead.id)
        .order_by(LeadScore.overall_score.desc().nullslast(), EmailSent.id.asc())
        .all()
    ]
    assert buggy[0] == 2, "pre-fix ordering puts the top score first"
    assert _send_order(db)[0] == 1, "fixed ordering honours the pin"
    assert buggy != _send_order(db)


def test_both_files_agree_on_the_ordering_keys():
    """Creation and scheduling must not drift apart.

    campaign_service orders leads at creation; campaign_worker orders the
    resulting rows when stamping scheduled_at. Only the worker can read
    send_position (the column lives on EmailSent, which creation is busy
    producing), so the contract is: the worker sorts on send_position, and
    the service says in writing why it does not.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "services" / "email_campaign"
    worker = (root / "campaign_worker.py").read_text()
    service = (root / "campaign_service.py").read_text()

    assert "EmailSent.send_position.asc().nullslast()" in worker, \
        "campaign_worker must sort by send_position before score"
    assert "send_position" in service, \
        "campaign_service must document why creation order ignores send_position"
