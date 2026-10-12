"""Reads shared by the agent's tools and the workspace routes.

One function per question, each returning a `schemas` model, so the chat and
the tabs cannot disagree. Everything is scoped through
`Candidate.user_id == user_id`. No lead address or phone leaves this module:
a lead's `email` is read only to compute `has_address`, and free text written
by third parties (reply text, scraped company descriptions) goes through
`status_words.redact_contact` first.

Every public function takes `(db, user, ...)` where `user` is a User row or
the user id. Missing or not yours raises `context.ActionError("not_found")`.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from core.config import settings as app_settings
from core.logger import get_logger
from database.models import (
    AgentDraft,
    Campaign,
    Candidate,
    CompanyProfile,
    EmailSent,
    Lead,
    LeadScore,
)
from services.agent import status_words as sw
from services.agent import store
from services.agent.context import not_found, resolve_agent_candidate, user_id_of
from services.agent.schemas import (
    STATUS_WORDS,
    AgentSettings,
    CompanyFacts,
    Counts,
    MatchCard,
    MatchDetail,
    MatchList,
    MatchTotals,
    MemoryList,
    NeverContact,
    OutreachDetail,
    OutreachList,
    OutreachRow,
    ReplyCounts,
    ReplyItem,
    ReplyList,
    Why,
)

logger = get_logger(__name__)

MAX_MESSAGE_CHARS = 4000
NOTE_KEYS = ("best_project", "outcome", "why_now", "credential")


def cap(name: str, default: int) -> int:
    return int(getattr(app_settings, name, default))


def usage_today(db: Session, user: Any) -> dict:
    return dict(store.usage_today(db, user_id_of(user)))


def reap_stale_drafts(db: Session, user: Any) -> int:
    """`drafting` rows silent for 5 minutes become failed. Lazy, on reads; commits when any did."""
    return store.reap_stale_drafts(db, user_id_of(user))


# ---------------------------------------------------------------- settings and memories

def settings(db: Session, user: Any) -> AgentSettings:
    return store.get_settings(db, user_id_of(user))


def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").casefold().split())


def never_contact_hit(rules: list[NeverContact], name: Optional[str], company: Optional[str]) -> bool:
    """A company rule matches the company name or its leading words
    ("Acme" blocks "Acme India"); a person rule matches the full name."""
    n, c = _norm(name), _norm(company)
    for r in rules:
        v = _norm(r.value)
        if not v:
            continue
        if r.kind == "company" and c and (c == v or c.startswith(v + " ")):
            return True
        if r.kind == "person" and n and n == v:
            return True
    return False


def memories(db: Session, user: Any) -> MemoryList:
    rows = store.list_memories(db, user_id_of(user))
    return MemoryList(items=[store.memory_out(m) for m in rows], cap=store.MEMORY_CAP)


# ---------------------------------------------------------------- matches

def _score_int(overall: Optional[float]) -> Optional[int]:
    if overall is None:
        return None
    return max(0, min(100, int(round(float(overall)))))


def _justification(j: Any) -> dict:
    return j if isinstance(j, dict) else {}


def _signal(j: dict) -> Optional[str]:
    s = j.get("signal_strength")
    return s if s in ("high", "medium", "low") else None


def _ranked_rows(db: Session, candidate_id: int) -> list[dict]:
    """The candidate's leads in the order of GET /candidate/{id}/leads: newest
    score row per lead, best first, unscored at the median, then lead id."""
    from api.routes_candidate import _is_broader

    leads = (
        db.query(Lead.id, Lead.name, Lead.title, Lead.company, Lead.company_domain, Lead.location,
                 Lead.email, Lead.email_verified)
        .filter(Lead.candidate_id == candidate_id).order_by(Lead.id).all()
    )
    scores = {}
    if leads:
        for s in (db.query(LeadScore.lead_id, LeadScore.overall_score, LeadScore.justification_json,
                           LeadScore.title_relevance)
                  .join(Lead, Lead.id == LeadScore.lead_id)
                  .filter(Lead.candidate_id == candidate_id).order_by(LeadScore.id).all()):
            scores[s.lead_id] = s
    out = []
    for lead in leads:
        s = scores.get(lead.id)
        j = _justification(s.justification_json if s else None)
        out.append({
            "lead_id": lead.id, "name": lead.name or "", "title": lead.title, "company": lead.company,
            "company_domain": lead.company_domain, "city": lead.location,
            "overall": s.overall_score if s else None,
            "reason": (j.get("headline") or None) if j else None,
            "signal_strength": _signal(j),
            "strong": not _is_broader(s, lead.title),
            "has_address": bool(lead.email) and bool(lead.email_verified),
        })
    scored = sorted(r["overall"] for r in out if r["overall"] is not None)
    median = scored[len(scored) // 2] if scored else 0
    out.sort(key=lambda r: (-(r["overall"] if r["overall"] is not None else median), r["lead_id"]))
    return out


def _lead_states(db: Session, uid: str, candidate_id: int) -> tuple[dict, dict]:
    """({lead_id: (state, outreach_id)} from drafts, {lead_id: e_id} from Touch 1 emails)."""
    drafts: dict[int, tuple[str, str]] = {}
    for d_id, lead_id, status in (db.query(AgentDraft.id, AgentDraft.lead_id, AgentDraft.status)
                                  .filter(AgentDraft.user_id == uid, AgentDraft.candidate_id == candidate_id,
                                          AgentDraft.status.in_(sw.LIVE_DRAFT_STATUSES + ("skipped",)))
                                  .order_by(AgentDraft.id).all()):
        drafts[lead_id] = ("skipped" if status == "skipped" else "drafted", f"d_{d_id}")
    emails: dict[int, str] = {}
    lead_ids = db.query(Lead.id).filter(Lead.candidate_id == candidate_id)
    for e_id, lead_id, status in (db.query(EmailSent.id, EmailSent.lead_id, EmailSent.status)
                                  .join(Campaign, Campaign.id == EmailSent.campaign_id)
                                  .join(Candidate, Candidate.id == Campaign.candidate_id)
                                  .filter(Candidate.user_id == uid, EmailSent.lead_id.in_(lead_ids),
                                          EmailSent.followup_number == 0, EmailSent.is_test.isnot(True))
                                  .order_by(EmailSent.id).all()):
        if sw.email_word(status) is not None:
            emails[lead_id] = f"e_{e_id}"
    return drafts, emails


def _cards(db: Session, uid: str, candidate_id: int) -> list[tuple[MatchCard, bool]]:
    """(MatchCard, blocked by never-contact) for every lead, ranked."""
    rules = settings(db, uid).never_contact
    drafts, emails = _lead_states(db, uid, candidate_id)
    out = []
    for r in _ranked_rows(db, candidate_id):
        lid = r["lead_id"]
        if lid in drafts:
            state, oid = drafts[lid]
        elif lid in emails:
            state, oid = "contacted", emails[lid]
        else:
            state, oid = "new", None
        card = MatchCard(
            lead_id=lid, name=r["name"] or "Unknown", title=r["title"], company=r["company"],
            company_domain=r["company_domain"], city=r["city"], score=_score_int(r["overall"]),
            reason=r["reason"], signal_strength=r["signal_strength"], strong=r["strong"],
            has_address=r["has_address"], state=state, outreach_id=oid,
        )
        out.append((card, never_contact_hit(rules, card.name, card.company)))
    return out


def _candidate_or_none(db: Session, user: Any, candidate: Any = None):
    return candidate if candidate is not None else resolve_agent_candidate(db, user_id_of(user))


def ranked_matches(db: Session, user: Any, *, q: Optional[str] = None, company: Optional[str] = None,
                   city: Optional[str] = None, title: Optional[str] = None, min_score: Optional[float] = None,
                   state: Optional[str] = None, strong_only: bool = False, exclude_contacted: bool = False,
                   limit: int = 25, offset: int = 0, candidate: Any = None) -> MatchList:
    """GET /matches and list_matches. Without `state`, skipped and
    never-contact people are left out; `exclude_contacted` also hides people
    with a draft or an email."""
    uid = user_id_of(user)
    limit, offset = max(1, min(int(limit or 25), 50)), max(0, int(offset or 0))
    cand = _candidate_or_none(db, uid, candidate)
    if cand is None:
        return MatchList(items=[], total=0, strong_total=0, offset=offset, limit=limit)

    def has(value: Optional[str], needle: Optional[str]) -> bool:
        return not needle or _norm(needle) in _norm(value)

    picked = []
    for card, blocked in _cards(db, uid, cand.id):
        if state:
            if card.state != state:
                continue
        elif card.state == "skipped" or blocked:
            continue
        if exclude_contacted and card.state in ("drafted", "contacted"):
            continue
        if strong_only and not card.strong:
            continue
        if min_score is not None and (card.score is None or card.score < min_score):
            continue
        if q and not any(has(v, q) for v in (card.name, card.title, card.company)):
            continue
        if not (has(card.company, company) and has(card.city, city) and has(card.title, title)):
            continue
        picked.append(card)
    return MatchList(items=picked[offset:offset + limit], total=len(picked),
                     strong_total=sum(1 for c in picked if c.strong), offset=offset, limit=limit)


def top_matches(db: Session, user: Any, n: int = 5, candidate: Any = None) -> list[MatchCard]:
    """Best `n` people with state new and not on the never-contact list."""
    uid = user_id_of(user)
    cand = _candidate_or_none(db, uid, candidate)
    if cand is None or n <= 0:
        return []
    return [c for c, blocked in _cards(db, uid, cand.id) if c.state == "new" and not blocked][:n]


def match_totals(db: Session, user: Any, candidate: Any = None) -> MatchTotals:
    uid = user_id_of(user)
    cand = _candidate_or_none(db, uid, candidate)
    if cand is None:
        return MatchTotals(total=0, strong=0, new=0)
    cards = _cards(db, uid, cand.id)
    return MatchTotals(total=len(cards), strong=sum(1 for c, _ in cards if c.strong),
                       new=sum(1 for c, b in cards if c.state == "new" and not b))


def _score_why(s: Any) -> list[str]:
    """Bullets built only from the stored relevance numbers, so the model is
    never handed a reason nobody computed."""
    if s is None:
        return []
    out = []
    if (s.title_relevance or 0) > 0:
        out.append("Their title lines up with the roles you are targeting.")
    if (s.department_relevance or 0) > 0:
        out.append("They work in the team closest to the work you want.")
    if (s.seniority_relevance or 0) > 0:
        out.append("They are senior enough to hire for your role.")
    if (s.location_relevance or 0) > 0:
        out.append("They are based where you said you want to work.")
    if (s.title_relevance or 0) <= -25:
        out.append("Their title is further from your target roles, so this is a broader match.")
    return out


def match_detail(db: Session, user: Any, lead_id: Any) -> MatchDetail:
    from services.agent.context import owned_lead

    uid = user_id_of(user)
    lead = owned_lead(db, uid, lead_id)
    if lead is None:
        raise not_found("I couldn't find that person in your matches.")
    card = next((c for c, _ in _cards(db, uid, lead.candidate_id) if c.lead_id == lead.id), None)
    if card is None:
        raise not_found("I couldn't find that person in your matches.")
    s = (db.query(LeadScore).filter(LeadScore.lead_id == lead.id).order_by(LeadScore.id.desc()).first())
    j = _justification(s.justification_json if s else None)
    bullets = [b for b in (j.get("bullets") or []) if isinstance(b, str) and b.strip()]
    if j.get("headline") or bullets:
        why = Why(headline=j.get("headline") or None, bullets=bullets[:3], signal_strength=_signal(j), source="ai")
    else:
        why = Why(headline=None, bullets=_score_why(s), signal_strength=None, source="score")
    facts = None
    prof = (db.query(CompanyProfile).filter(CompanyProfile.domain == lead.company_domain.lower()).first()
            if lead.company_domain else None)
    if prof is not None:
        ef = prof.extracted_facts if isinstance(prof.extracted_facts, dict) else {}
        facts = CompanyFacts(
            summary=sw.redact_contact(prof.short_description) or None, what_they_build=ef.get("what_they_build") or None,
            core_tech=[t for t in (ef.get("core_tech") or []) if isinstance(t, str)][:8],
            primary_market=ef.get("primary_market") or None, recent_momentum=ef.get("recent_momentum") or None,
            hiring_signal=ef.get("hiring_signal") or None)
    elif lead.company_description:
        facts = CompanyFacts(summary=sw.redact_contact(lead.company_description.strip()[:600]) or None)
    return MatchDetail(**card.model_dump(), industry=lead.industry, company_size=lead.company_size,
                       linkedin_url=lead.linkedin_url, why=why, company_facts=facts,
                       outreach=_outreach_rows(db, uid, lead_id=lead.id))


# ---------------------------------------------------------------- outreach

_VISIBLE_DRAFT = tuple(k for k, v in sw.DRAFT_WORDS.items() if v is not None)
_VISIBLE_EMAIL = tuple(sw.EMAIL_WORDS)


def _touch1_query(db: Session, uid: str):
    return (db.query(EmailSent, Lead)
            .join(Campaign, Campaign.id == EmailSent.campaign_id)
            .join(Candidate, Candidate.id == Campaign.candidate_id)
            .outerjoin(Lead, Lead.id == EmailSent.lead_id)
            .filter(Candidate.user_id == uid, EmailSent.followup_number == 0, EmailSent.is_test.isnot(True)))


def _followups(db: Session, email_ids: list[int]) -> dict[int, list]:
    out: dict[int, list] = {}
    if email_ids:
        for f in (db.query(EmailSent).filter(EmailSent.parent_email_id.in_(email_ids),
                                             EmailSent.followup_number > 0).all()):
            out.setdefault(f.parent_email_id, []).append(f)
    return out


def _outreach_rows(db: Session, uid: str, lead_id: Optional[int] = None) -> list[OutreachRow]:
    reap_stale_drafts(db, uid)
    dq = (db.query(AgentDraft, Lead).join(Lead, Lead.id == AgentDraft.lead_id)
          .join(Candidate, Candidate.id == AgentDraft.candidate_id)
          .filter(AgentDraft.user_id == uid, Candidate.user_id == uid, AgentDraft.status.in_(_VISIBLE_DRAFT)))
    eq = _touch1_query(db, uid).filter(EmailSent.status.in_(_VISIBLE_EMAIL))
    if lead_id is not None:
        dq, eq = dq.filter(AgentDraft.lead_id == lead_id), eq.filter(EmailSent.lead_id == lead_id)
    rows = [r for r in (sw.draft_row(d, lead) for d, lead in dq.all()) if r is not None]
    emails = eq.all()
    fus = _followups(db, [e.id for e, _ in emails])
    rows += [r for r in (sw.email_row(e, lead, fus.get(e.id, [])) for e, lead in emails) if r is not None]
    rows.sort(key=sw.sort_key, reverse=True)
    return rows


def _counts(rows: list[OutreachRow]) -> Counts:
    by = {w: 0 for w in STATUS_WORDS}
    for r in rows:
        by[r.status_word] += 1
    return Counts(all=len(rows), **by)


def outreach_list(db: Session, user: Any, status: Optional[str] = None, lead_id: Optional[int] = None,
                  limit: int = 20, offset: int = 0) -> OutreachList:
    """GET /outreach and list_outreach. `counts` are unfiltered, for the chips."""
    uid = user_id_of(user)
    limit, offset = max(1, min(int(limit or 20), 50)), max(0, int(offset or 0))
    rows = _outreach_rows(db, uid)
    picked = rows
    if status and status != "all":
        picked = [r for r in picked if r.status_word == status]
    if lead_id is not None:
        picked = [r for r in picked if r.lead.id == int(lead_id)]
    return OutreachList(items=picked[offset:offset + limit], counts=_counts(rows), total=len(picked),
                        offset=offset, limit=limit)


def outreach_detail(db: Session, user: Any, outreach_id: Any) -> OutreachDetail:
    from services.agent.context import owned_outreach

    uid = user_id_of(user)
    reap_stale_drafts(db, uid)
    found = owned_outreach(db, uid, outreach_id)
    if found is None:
        raise not_found()
    kind, row = found
    lead = db.query(Lead).filter(Lead.id == row.lead_id).first()
    if kind == "d":
        if sw.draft_word(row.status) is None:
            raise not_found()
        return sw.draft_detail(row, lead)
    from services.email_campaign.campaign_service import customer_failure_reason

    detail = sw.email_detail(row, lead, _followups(db, [row.id]).get(row.id, []),
                             customer_failure_reason(row.status, row.error_message))
    if detail is None:
        raise not_found()
    return detail


def replies(db: Session, user: Any, group: Optional[str] = None, limit: int = 20, offset: int = 0) -> ReplyList:
    uid = user_id_of(user)
    limit, offset = max(1, min(int(limit or 20), 50)), max(0, int(offset or 0))
    items = []
    for e, lead in _touch1_query(db, uid).filter(EmailSent.status.in_(("replied", "bounced"))).all():
        g = sw.reply_group(e)
        if g is None:
            continue
        items.append(ReplyItem(
            outreach_id=f"e_{e.id}", lead=sw.lead_ref(lead, e.lead_id), subject=e.subject,
            reply_text=None if g == "bounced" else (sw.redact_contact(e.reply_text) or None),
            sentiment=e.reply_sentiment if e.reply_sentiment in ("positive", "negative", "neutral") else None,
            group=g, received_at=sw.iso(e.reply_received_at), sent_at=sw.iso(e.sent_at)))
    items.sort(key=lambda i: (i.received_at or i.sent_at or "", int(i.outreach_id[2:])), reverse=True)
    counts = ReplyCounts(**{k: sum(1 for i in items if i.group == k)
                            for k in ("positive", "question", "not_now", "bounced")})
    picked = [i for i in items if not group or i.group == group]
    return ReplyList(items=picked[offset:offset + limit], counts=counts, total=len(picked),
                     offset=offset, limit=limit)


# Profile, status, greeting and chips live in reads_profile.py to keep files short.
from services.agent import reads_profile as _rp  # noqa: E402

profile, status, greeting, default_suggestions = _rp.profile, _rp.status, _rp.greeting, _rp.default_suggestions
