"""Emails held back while Apollo is out of credits come back once it is topped up.

- The call that first discovers the exhaustion used to raise ValueError, which
  enrichment did not recognise, so it counted toward the lead's permanent
  failure (3 prod emails died as "Enrichment error: All Apollo API keys are
  exhausted"). It must pause the email instead.
- Paused emails were requeued every cycle whether or not Apollo had credits,
  and an exhausted key was never retried until the pod restarted. Now they
  wait, the key is re-probed every 30 min, and a restore signal from any
  replica resumes them at once.
- A resumed campaign is re-spaced from now, not sent as one overdue burst.
"""
import pathlib
import sys
from datetime import datetime, timedelta
from itertools import pairwise

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
from services.email_campaign import apollo_pause, campaign_worker
from services.shared import apollo_key_manager
from services.shared.apollo_key_manager import apollo_keys


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


NOW = datetime(2026, 9, 28, 12, 0, 0)


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
    """Fake Apollo. Set .status to what People Match returns; .calls counts hits."""
    class Fake:
        status = 402
        calls = 0
        # With status 200: None finds everyone; a set finds only those first names.
        findable = None

    def request(method, url, headers=None, **kw):
        Fake.calls += 1
        if Fake.status == 200:
            first = (kw.get("json") or {}).get("first_name")
            if Fake.findable is not None and first not in Fake.findable:
                return _Resp(200, {"person": {"first_name": first, "email": None}})
            return _Resp(200, {"person": {"email": "found@co.com", "name": "Found",
                                          "email_status": "verified"}})
        return _Resp(Fake.status)

    monkeypatch.setattr(apollo_key_manager.requests, "request", request)
    monkeypatch.setattr(campaign_worker.time, "sleep", lambda s: None)
    apollo_keys.reset()
    monkeypatch.setattr(apollo_pause, "_restore_checked", False)
    monkeypatch.setattr(apollo_pause, "_seen_restore_at", None)
    monkeypatch.setattr(apollo_pause, "_canary_verdict", None)
    apollo_pause._reset_sweep_backoff()
    yield Fake
    apollo_keys.reset()


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[t.__table__ for t in (
        User, Candidate, Lead, LeadScore, Campaign, EmailAccount, EmailSent, SystemEvent,
        UserCredit, CreditLedger, SuppressedEmail)])
    session = sessionmaker(bind=engine)()
    session.add_all([
        User(id="u", email="u@x.com", name="U", email_verified=True, created_at=NOW, updated_at=NOW),
        Candidate(id=1, user_id="u", resume_text="."),
        EmailAccount(id=5, user_id="u", email_address="u@gmail.com", provider="gmail",
                     access_token="t", refresh_token="r", token_expiry=NOW + timedelta(days=9)),  # noqa: S106
        UserCredit(user_id="u", total_credits=10, used_credits=10),
        Campaign(id=10, candidate_id=1, email_account_id=5, name="c", status="running",
                 credits_reserved=10, credits_released=0),
    ])
    session.commit()
    yield session
    session.close()


def _pending(db, n=1, scheduled_at=None):
    rows = []
    for i in range(n):
        lead = Lead(candidate_id=1, name=f"Lead {i}", company="Co")
        db.add(lead)
        db.flush()
        row = EmailSent(campaign_id=10, lead_id=lead.id, status="pending_enrichment",
                        enrichment_status="pending",
                        scheduled_at=scheduled_at or datetime.utcnow() + timedelta(minutes=5))
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def _age_exhaustion_marks():
    """Pretend the 30-minute re-probe window has passed."""
    with apollo_keys._lock:
        for k in apollo_keys._exhausted:
            apollo_keys._exhausted[k] -= apollo_key_manager.EXHAUSTED_RETRY_AFTER + 1


def test_running_out_pauses_the_email_instead_of_failing_it(db, apollo):
    (email,) = _pending(db)

    campaign_worker._enrich_upcoming(db)

    db.refresh(email)
    lead = db.get(Lead, email.lead_id)
    assert email.enrichment_status == "credit_paused"
    assert email.status == "pending_enrichment"
    assert (lead.enrichment_fail_count or 0) == 0


def test_repeated_cycles_while_empty_never_fail_the_email(db, apollo):
    (email,) = _pending(db)
    for _ in range(campaign_worker.MAX_ENRICHMENT_FAILURES + 2):
        campaign_worker._enrich_upcoming(db)
        apollo_pause.requeue_credit_paused(db)
        _age_exhaustion_marks()

    db.refresh(email)
    assert email.status != "failed"
    assert (db.get(Lead, email.lead_id).enrichment_fail_count or 0) == 0


def test_paused_emails_wait_while_apollo_is_still_empty(db, apollo):
    _pending(db, 3)
    campaign_worker._enrich_upcoming(db)
    calls = apollo.calls

    for _ in range(5):
        assert apollo_pause.requeue_credit_paused(db) == 0
        campaign_worker._enrich_upcoming(db)

    assert apollo_pause.paused_count(db) == 3
    assert apollo.calls == calls  # no Apollo traffic while known empty


def test_top_up_is_picked_up_by_the_reprobe_on_its_own(db, apollo):
    (email,) = _pending(db)
    campaign_worker._enrich_upcoming(db)
    apollo.status = 200  # the account was topped up; nobody told us

    _age_exhaustion_marks()
    assert apollo_pause.requeue_credit_paused(db) == 1
    campaign_worker._enrich_upcoming(db)

    db.refresh(email)
    assert email.enrichment_status == "enriched"
    assert email.to_email == "found@co.com"


def test_restore_signal_from_another_replica_resumes_at_once(db, apollo):
    (email,) = _pending(db)
    campaign_worker._enrich_upcoming(db)
    assert apollo_pause.requeue_credit_paused(db) == 0  # this replica has now looked once

    apollo.status = 200
    # Another replica handled the admin call: only the DB row reaches this one.
    db.add(SystemEvent(event_type=apollo_pause.CREDITS_RESTORED_EVENT))
    db.commit()

    assert apollo_pause.requeue_credit_paused(db) == 1
    campaign_worker._enrich_upcoming(db)
    db.refresh(email)
    assert email.enrichment_status == "enriched"


def test_resumed_backlog_is_respaced_not_sent_in_one_burst(db, apollo):
    overdue = datetime.utcnow() - timedelta(days=2)
    rows = _pending(db, 6, scheduled_at=overdue)
    for r in rows:
        r.enrichment_status = "credit_paused"
    db.commit()

    apollo_pause.signal_credits_restored(db)
    db.commit()
    assert apollo_pause.requeue_credit_paused(db) == 6

    now = datetime.utcnow()
    times = sorted(db.get(EmailSent, r.id).scheduled_at for r in rows)
    assert all(t > now for t in times)
    # The campaign's normal daily pace (minutes apart), not six sends at once.
    assert all(b - a >= timedelta(minutes=10) for a, b in pairwise(times))
    assert sum(1 for t in times if t <= now + timedelta(minutes=10)) <= 1


def test_paused_rows_in_a_paused_campaign_are_not_rescheduled(db, apollo):
    rows = _pending(db, 2, scheduled_at=datetime.utcnow() - timedelta(days=1))
    for r in rows:
        r.enrichment_status = "credit_paused"
    db.get(Campaign, 10).status = "paused"
    db.commit()
    before = [r.scheduled_at for r in rows]

    apollo_pause.requeue_credit_paused(db)

    assert [db.get(EmailSent, r.id).scheduled_at for r in rows] == before


# ── Apollo out of credits but answering "no email" (the 2026-09-27/28 outage) ──

def _found_recently(db, n=2):
    """Leads Apollo found in the last few days: the canaries."""
    for i in range(n):
        lead = Lead(candidate_id=1, name=f"Canary{i} X", company="Co", email=f"c{i}@co.com",
                    email_verified=True)
        db.add(lead)
        db.flush()
        db.add(EmailSent(campaign_id=10, lead_id=lead.id, status="sent", enrichment_status="enriched",
                         scheduled_at=datetime.utcnow() - timedelta(hours=5)))
    db.commit()


def _run_until_decided(db, email):
    for _ in range(campaign_worker.MAX_ENRICHMENT_FAILURES + 1):
        campaign_worker._enrich_upcoming(db)
        db.refresh(email)
        if email.enrichment_status != "pending":
            return


def test_silent_outage_pauses_instead_of_failing(db, apollo):
    _found_recently(db)
    (email,) = _pending(db)
    apollo.status, apollo.findable = 200, set()  # nobody has an email any more

    _run_until_decided(db, email)

    assert email.enrichment_status == "credit_paused"
    assert email.status == "pending_enrichment"
    assert db.get(Lead, email.lead_id).enrichment_fail_count == 0
    assert not apollo_keys.has_valid_key()  # the rest of the queue waits too
    assert db.query(EmailSent).filter(EmailSent.replacement_for_id == email.id).count() == 0


def test_genuine_no_match_still_fails_when_apollo_finds_others(db, apollo):
    _found_recently(db)
    (email,) = _pending(db)
    apollo.status, apollo.findable = 200, {"Canary0", "Canary1"}

    _run_until_decided(db, email)

    assert email.status == "failed"
    assert email.error_message == "Apollo could not find email for this contact"
    assert apollo_keys.has_valid_key()


def test_no_match_fails_normally_when_there_is_nothing_to_compare(db, apollo):
    (email,) = _pending(db)
    apollo.status, apollo.findable = 200, set()

    _run_until_decided(db, email)

    assert email.status == "failed"


# ── PP-P34: the sweep backs off while Apollo keeps pausing the same rows ──

def test_sweep_backs_off_while_rows_keep_getting_paused_again(db, apollo, monkeypatch):
    apollo.status = 429  # rate limited: pauses the row, key stays usable
    (email,) = _pending(db)
    clock = [datetime.utcnow()]

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return clock[0]
    monkeypatch.setattr(apollo_pause, "datetime", _DT)

    requeued_at = []
    for second in range(0, 600, 30):  # 10 minutes of 30s worker cycles
        clock[0] = _DT.utcnow() + timedelta(seconds=30) if second else clock[0]
        campaign_worker._enrich_upcoming(db)
        if apollo_pause.requeue_credit_paused(db):
            requeued_at.append(second)
    # Without backoff: requeued (and Apollo called) on all 20 cycles.
    assert len(requeued_at) <= 6
    gaps = [b - a for a, b in pairwise(requeued_at)]
    assert gaps == sorted(gaps) and gaps[-1] > gaps[0]

    # A scoped call (someone pressing retry) is not held back.
    campaign_worker._enrich_upcoming(db)
    assert apollo_pause.requeue_credit_paused(db, campaign_id=10) == 1
