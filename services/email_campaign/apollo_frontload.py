"""Apollo frontload: find every recipient we will ever need while Apollo still works.

Enrichment is normally just-in-time: an email's recipient is looked up
JIT_LOOKAHEAD_HOURS before its send slot. When the Apollo plan is about to
end, that means the credits left on it expire unused, and every later email
pauses forever for lack of a key. Frontload spends them now instead.

While switched on (start(), until a deadline), each worker cycle looks up
to PER_CYCLE extra leads, in this order:

  1. unsent emails in running campaigns, soonest first;
  2. unsent emails in paused campaigns (they need it when resumed);
  3. a stock of backup leads for running campaigns (about BUFFER_SHARE of
     their unsent emails, within the replacement cap and the campaign's
     unused paid credits), so a no-match or a bounce can still be replaced
     once Apollo is gone;
  4. paid users who have not launched: their best-scored leads, as many as
     their available credits will buy, so a campaign created later starts
     with its recipients already known.

Tiers 1-2 go through campaign_worker._enrich_one, the same code the JIT
loop uses, so a no-match, replacement, credit pause or silent outage is
handled identically. Only the lookup moves earlier: an email's content is
still generated JIT_LOOKAHEAD_HOURS before its slot and it is sent on its
normal schedule. Tiers 3-4 only store the address on the lead; the existing
"lead already enriched" paths pick it up with no Apollo call.

Each lookup writes lead.email, which is kept after Apollo ends. Frontload
stops on its own when the deadline passes, when there is nothing left, or
when Apollo runs dry (the key manager / apollo_looks_empty pause it).
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.logger import get_logger
from database.models import Campaign, Candidate, EmailSent, Lead, LeadScore, SystemEvent, UserCredit
from services.credits import MIN_CAMPAIGN_CREDITS
from services.shared.apollo_key_manager import apollo_keys

logger = get_logger(__name__)

EVENT = "apollo_frontload"
PER_CYCLE = 25
BUFFER_SHARE = 0.10
REPLACEMENT_CAP_PERCENT = 0.25  # mirrors replenishment
MAX_FAILURES = 3  # mirrors campaign_worker.MAX_ENRICHMENT_FAILURES
UNSENT = ("pending_enrichment", "queued")
# Stop a cycle's lead-only tiers after this many straight no-matches and ask
# apollo_looks_empty whether it is the account, not the leads.
NO_MATCH_STREAK = 5
# Keep a batch well inside the 30s cycle. Two overlapping batches (a slow
# cycle on one replica, the next tick on another) would at worst look the
# same lead up twice; the cap keeps that from happening in practice.
MAX_BATCH_SECONDS = 15


# ── switch ──────────────────────────────────────────────────────────────────

def start(db: Session, until: datetime, note: str = "") -> None:
    """Run frontload until `until` (UTC). A past `until` switches it off. Caller commits."""
    db.add(SystemEvent(event_type=EVENT, meta={"until": until.isoformat(), "note": note}))


def deadline(db: Session) -> Optional[datetime]:
    row = (
        db.query(SystemEvent)
        .filter(SystemEvent.event_type == EVENT)
        .order_by(SystemEvent.created_at.desc())
        .first()
    )
    if row is None or not row.meta or not row.meta.get("until"):
        return None
    return datetime.fromisoformat(row.meta["until"])


def active(db: Session) -> bool:
    until = deadline(db)
    return until is not None and datetime.utcnow() < until


# ── targets ─────────────────────────────────────────────────────────────────

def _needs_apollo(q):
    return q.filter(
        (Lead.email.is_(None)) | (Lead.email_verified.isnot(True)),
        (Lead.enrichment_fail_count.is_(None)) | (Lead.enrichment_fail_count < MAX_FAILURES),
    )


def _email_targets(db: Session, campaign_status: str, limit: int):
    q = (
        db.query(EmailSent)
        .join(Campaign, Campaign.id == EmailSent.campaign_id)
        .join(Lead, Lead.id == EmailSent.lead_id)
        .filter(
            Campaign.status == campaign_status,
            EmailSent.status.in_(UNSENT),
            EmailSent.enrichment_status == "pending",
        )
    )
    return _needs_apollo(q).order_by(EmailSent.scheduled_at.asc().nullslast(), EmailSent.id).limit(limit).all()


def _unused_pool(db: Session, candidate_id: int, exclude_campaign_id: Optional[int] = None):
    """Leads of a candidate ordered as a campaign would pick them (best score first)."""
    q = (
        db.query(Lead)
        .outerjoin(LeadScore, LeadScore.lead_id == Lead.id)
        .filter(Lead.candidate_id == candidate_id,
                (Lead.enrichment_fail_count.is_(None)) | (Lead.enrichment_fail_count < MAX_FAILURES))
    )
    if exclude_campaign_id is not None:
        used = db.query(EmailSent.lead_id).filter(EmailSent.campaign_id == exclude_campaign_id)
        q = q.filter(Lead.id.notin_(used))
    return q.order_by(LeadScore.overall_score.desc().nullslast(), Lead.id.asc())


def _buffer_targets(db: Session, limit: int) -> list[Lead]:
    from services.email_campaign.campaign_worker import _COMMITTED, _paid_slots_in

    out: list[Lead] = []
    for c in db.query(Campaign).filter(Campaign.status == "running").order_by(Campaign.id):
        rows = db.query(EmailSent).filter(EmailSent.campaign_id == c.id).all()
        initial = sum(1 for r in rows if r.replacement_for_id is None and not r.is_test
                      and (r.followup_number or 0) == 0)
        cap_left = math.ceil(initial * REPLACEMENT_CAP_PERCENT) - sum(1 for r in rows if r.replacement_for_id)
        unsent = sum(1 for r in rows if r.status in UNSENT)
        want = min(max(cap_left, 0), math.ceil(unsent * BUFFER_SHARE))
        if c.credits_reserved:
            # A replacement needs a paid slot (PP-P26: add_replacement_lead
            # refuses one at the cap), so stock no more than the slots left.
            paid_left = (c.credits_reserved - (c.credits_released or 0)
                         - _paid_slots_in(db, c.id, _COMMITTED))
            want = min(want, max(paid_left, 0))
        if want <= 0:
            continue
        pool = _unused_pool(db, c.candidate_id, exclude_campaign_id=c.id).limit(want).all()
        # The pool is taken best-first, so the stock is the top `want` leads.
        out.extend(lead for lead in pool if not (lead.email and lead.email_verified))
        if len(out) >= limit:
            break
    return out[:limit]


def _prelaunch_targets(db: Session, limit: int) -> list[Lead]:
    """Paid users who can launch (MIN_CAMPAIGN_CREDITS+) with nothing running or paused."""
    live = (
        db.query(Candidate.user_id)
        .join(Campaign, Campaign.candidate_id == Candidate.id)
        .filter(Campaign.status.in_(("running", "paused")))
    )
    wallets = (
        db.query(UserCredit)
        .filter(UserCredit.total_credits - UserCredit.used_credits >= MIN_CAMPAIGN_CREDITS,
                UserCredit.user_id.notin_(live))
        .order_by(UserCredit.user_id)
        .all()
    )
    out: list[Lead] = []
    for w in wallets:
        # The candidate a new campaign would use: the user's newest one with leads.
        cand = (
            db.query(Candidate)
            .join(Lead, Lead.candidate_id == Candidate.id)
            .filter(Candidate.user_id == w.user_id)
            .order_by(Candidate.id.desc())
            .first()
        )
        if cand is None:
            continue
        want = w.total_credits - w.used_credits
        pool = _unused_pool(db, cand.id).limit(want).all()
        out.extend(lead for lead in pool if not (lead.email and lead.email_verified))
        if len(out) >= limit:
            break
    return out[:limit]


def remaining(db: Session) -> dict:
    """What is still to do, per tier (for the status endpoint)."""
    def emails(status):
        q = (db.query(func.count(EmailSent.id))
             .join(Campaign, Campaign.id == EmailSent.campaign_id)
             .join(Lead, Lead.id == EmailSent.lead_id)
             .filter(Campaign.status == status, EmailSent.status.in_(UNSENT),
                     EmailSent.enrichment_status == "pending"))
        return _needs_apollo(q).scalar() or 0
    return {
        "running_emails": emails("running"),
        "paused_emails": emails("paused"),
        "backup_leads": len(_buffer_targets(db, 10**6)),
        "prelaunch_leads": len(_prelaunch_targets(db, 10**6)),
    }


# ── one batch ───────────────────────────────────────────────────────────────

def _enrich_lead(lead: Lead) -> str:
    """Store the address on the lead. Returns the result's error_type or 'ok'."""
    from services.enrichment.enrichment_service import enrich_single_lead_classified

    result = enrich_single_lead_classified(lead)
    if result.success:
        lead.email = result.data["email"]
        if result.data.get("name"):
            lead.name = result.data["name"]
        lead.email_verified = True
        lead.status = "enriched"
        return "ok"
    if result.error_type == "no_match":
        lead.enrichment_fail_count = (lead.enrichment_fail_count or 0) + 1
    return result.error_type or "exception"


def run_batch(db: Session, budget: int = PER_CYCLE) -> dict:
    """One cycle's worth of frontload. Returns counts per tier."""
    from services.email_campaign.apollo_pause import apollo_looks_empty
    from services.email_campaign.campaign_worker import _enrich_one

    done = {"running": 0, "paused": 0, "backup": 0, "prelaunch": 0, "no_match": 0}
    if not active(db) or not apollo_keys.has_valid_key():
        return done
    started = time.monotonic()

    def go_on() -> bool:
        return (budget > 0 and apollo_keys.has_valid_key()
                and time.monotonic() - started < MAX_BATCH_SECONDS)

    for tier in ("running", "paused"):
        for email in _email_targets(db, tier, budget):
            if not go_on():
                return done
            budget -= 1
            if _enrich_one(db, email):
                done[tier] += 1

    streak: list[Lead] = []
    for tier, pick in (("backup", _buffer_targets), ("prelaunch", _prelaunch_targets)):
        if not go_on():
            break
        for lead in pick(db, budget):
            if not go_on():
                break
            budget -= 1
            outcome = _enrich_lead(lead)
            if outcome == "ok":
                done[tier] += 1
                streak = []
            elif outcome == "no_match":
                done["no_match"] += 1
                streak.append(lead)
                if len(streak) >= NO_MATCH_STREAK and apollo_looks_empty(db, exclude_lead_id=lead.id):
                    # Not these leads: the account is empty. Give them their attempts back.
                    for s in streak:
                        s.enrichment_fail_count = max(0, (s.enrichment_fail_count or 1) - 1)
                    db.commit()
                    return done
            else:
                db.commit()
                return done  # credit / rate / outage: stop, the pause path takes over
        db.commit()
    return done


def maybe_run(db: Session) -> dict:
    """Worker-cycle hook. Never lets a frontload problem break the send cycle."""
    try:
        done = run_batch(db)
    except Exception:  # noqa: BLE001 - logged; the cycle must go on
        db.rollback()
        logger.exception("[FRONTLOAD] batch failed")
        return {}
    if any(done.values()):
        logger.info("[FRONTLOAD] %s", done)
    return done
