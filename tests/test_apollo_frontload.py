"""Apollo frontload: spend the plan's last credits finding every recipient we
will need, without changing when anything is written or sent."""
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
    Base, Campaign, Candidate, CreditLedger, EmailAccount, EmailSent, Lead, LeadScore, SuppressedEmail,
    SystemEvent, User, UserCredit,
)
from services.email_campaign import apollo_frontload, apollo_pause, campaign_worker
from services.email_campaign.replenishment import add_replacement_lead
from services.shared import apollo_key_manager
from services.shared.apollo_key_manager import apollo_keys


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 29, 12, 0, 0)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}
        self.text = ""

    def json(self):
        return self._body


@pytest.fixture()
def apollo(monkeypatch):
    """Fake People Match. status 402 = out of credits; findable=None finds everyone."""
    class Fake:
        status = 200
        findable = None
        calls = 0

    def request(method, url, headers=None, **kw):
        Fake.calls += 1
        if Fake.status != 200:
            return _Resp(Fake.status)
        first = (kw.get("json") or {}).get("first_name")
        if Fake.findable is not None and first not in Fake.findable:
            return _Resp(200, {"person": {"first_name": first, "email": None}})
        return _Resp(200, {"person": {"email": f"{first.lower()}@co.com", "email_status": "verified"}})

    monkeypatch.setattr(apollo_key_manager.requests, "request", request)
    monkeypatch.setattr(campaign_worker.time, "sleep", lambda s: None)
    monkeypatch.setattr(apollo_pause, "_canary_verdict", None)
    monkeypatch.setattr(apollo_pause, "_restore_checked", False)
    apollo_keys.reset()
    yield Fake
    apollo_keys.reset()


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, SystemEvent,
        UserCredit, CreditLedger, SuppressedEmail)])
    s = sessionmaker(bind=engine)()
    for uid in ("u", "v", "w"):
        s.add(User(id=uid, email=f"{uid}@x.com", name=uid, email_verified=True, created_at=NOW, updated_at=NOW))
    s.add_all([
        Candidate(id=1, user_id="u", resume_text="."),
        Candidate(id=2, user_id="v", resume_text="."),
        Candidate(id=3, user_id="w", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r", token_expiry=NOW + timedelta(days=9)),  # noqa: S106
        UserCredit(user_id="u", total_credits=100, used_credits=100),
        UserCredit(user_id="v", total_credits=100, used_credits=100),
        Campaign(id=10, candidate_id=1, email_account_id=5, name="live", status="running",
                 credits_reserved=100, credits_released=0),
        Campaign(id=20, candidate_id=2, email_account_id=5, name="held", status="paused",
                 credits_reserved=100, credits_released=0),
    ])
    s.commit()
    yield s
    s.close()


_n = iter(range(1, 10**6))


def _lead(db, cand, score=50, **kw):
    n = next(_n)
    lead = Lead(candidate_id=cand, name=f"P{n} X", company="Co", **kw)
    db.add(lead)
    db.flush()
    db.add(LeadScore(lead_id=lead.id, overall_score=score, title_relevance=1, department_relevance=1,
                     industry_relevance=1, seniority_relevance=1, location_relevance=1))
    return lead


def _rows(db, campaign, cand, n, first_in=timedelta(days=2)):
    rows = []
    for i in range(n):
        lead = _lead(db, cand)
        row = EmailSent(campaign_id=campaign, lead_id=lead.id, status="pending_enrichment",
                        enrichment_status="pending", scheduled_at=datetime.utcnow() + first_in + timedelta(hours=i))
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def _on(db, hours=72):
    apollo_frontload.start(db, datetime.utcnow() + timedelta(hours=hours))
    db.commit()


def test_off_by_default_and_after_the_deadline(db, apollo):
    _rows(db, 10, 1, 3)
    assert apollo_frontload.run_batch(db) == {k: 0 for k in ("running", "paused", "backup", "prelaunch", "no_match")}
    _on(db, hours=-1)
    apollo_frontload.run_batch(db)
    assert apollo.calls == 0


def test_far_future_emails_get_their_recipient_now_but_keep_their_slot(db, apollo):
    rows = _rows(db, 10, 1, 3, first_in=timedelta(days=10))
    slots = [r.scheduled_at for r in rows]
    _on(db)

    apollo_frontload.run_batch(db)

    for r, slot in zip(rows, slots, strict=True):
        db.refresh(r)
        assert r.enrichment_status == "enriched" and r.to_email
        assert r.scheduled_at == slot
        assert r.subject is None  # content is still written just in time


def test_after_apollo_ends_frontloaded_emails_still_go_out(db, apollo):
    (row,) = _rows(db, 10, 1, 1, first_in=timedelta(days=5))
    _on(db)
    apollo_frontload.run_batch(db)
    apollo.status = 402  # plan expired
    row.scheduled_at = datetime.utcnow() + timedelta(hours=1)  # its day comes
    db.commit()
    calls = apollo.calls

    campaign_worker._enrich_upcoming(db)

    db.refresh(row)
    assert row.enrichment_status == "enriched"
    assert apollo.calls == calls


def test_running_campaigns_come_before_paused_ones(db, apollo):
    live = _rows(db, 10, 1, 3)
    held = _rows(db, 20, 2, 3)
    _on(db)

    apollo_frontload.run_batch(db, budget=3)

    assert all(db.get(EmailSent, r.id).enrichment_status == "enriched" for r in live)
    assert all(db.get(EmailSent, r.id).enrichment_status == "pending" for r in held)
    apollo_frontload.run_batch(db, budget=3)
    assert all(db.get(EmailSent, r.id).enrichment_status == "enriched" for r in held)


def test_backup_leads_are_stocked_so_replacements_need_no_apollo(db, apollo):
    _rows(db, 10, 1, 20)
    pool = [_lead(db, 1, score=90 - i) for i in range(5)]
    db.commit()
    _on(db)

    apollo_frontload.run_batch(db, budget=100)

    # 10% of 20 unsent: the best two unused leads.
    assert [bool(db.get(Lead, p.id).email) for p in pool] == [True, True, False, False, False]

    apollo.status = 402
    failed = db.query(EmailSent).filter_by(campaign_id=10).first()
    repl_id = add_replacement_lead(db, 10, failed.id, reason="enrichment_exhausted")
    repl = db.get(EmailSent, repl_id)
    assert repl.lead_id == pool[0].id and repl.to_email and repl.status == "queued"


def test_paid_users_who_have_not_launched_get_their_best_leads_found(db, apollo):
    db.add(UserCredit(user_id="w", total_credits=50, used_credits=0))
    best = [_lead(db, 3, score=100 - i) for i in range(60)]
    db.commit()
    _on(db)

    for _ in range(5):
        apollo_frontload.run_batch(db, budget=25)

    found = [bool(db.get(Lead, lead.id).email) for lead in best]
    assert found == [True] * 50 + [False] * 10  # exactly what 50 credits will buy


def test_users_below_a_campaign_worth_of_credit_are_skipped(db, apollo):
    db.add(UserCredit(user_id="w", total_credits=10, used_credits=0))
    _lead(db, 3)
    db.commit()
    _on(db)

    apollo_frontload.run_batch(db)

    assert apollo.calls == 0


def test_running_dry_midway_pauses_instead_of_failing(db, apollo):
    rows = _rows(db, 10, 1, 4)
    _on(db)
    apollo.status = 402

    apollo_frontload.run_batch(db)

    statuses = {db.get(EmailSent, r.id).enrichment_status for r in rows}
    assert "skipped" not in statuses and "credit_paused" in statuses
    assert all(db.get(EmailSent, r.id).status != "failed" for r in rows)


def test_silent_outage_in_the_lead_tiers_stops_and_undoes_attempts(db, apollo):
    # Something Apollo found recently, for the canary to re-ask.
    canary = _lead(db, 1, email="c@co.com", email_verified=True)
    db.add(EmailSent(campaign_id=10, lead_id=canary.id, status="sent", enrichment_status="enriched",
                     scheduled_at=datetime.utcnow() - timedelta(hours=3)))
    db.add(UserCredit(user_id="w", total_credits=50, used_credits=0))
    leads = [_lead(db, 3, score=100 - i) for i in range(10)]
    db.commit()
    _on(db)
    apollo.findable = set()  # out of credits, answering "no email" for everyone

    apollo_frontload.run_batch(db)

    assert all((db.get(Lead, lead.id).enrichment_fail_count or 0) == 0 for lead in leads)
    assert not apollo_keys.has_valid_key()


def test_status_counts_what_is_left(db, apollo):
    _rows(db, 10, 1, 2)
    _rows(db, 20, 2, 3)
    left = apollo_frontload.remaining(db)
    assert (left["running_emails"], left["paused_emails"]) == (2, 3)
