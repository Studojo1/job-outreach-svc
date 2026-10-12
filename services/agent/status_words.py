"""Outreach status words: contract section 1, as pure functions.

An outreach is one named hiring manager plus the email to them. It comes from
an `agent_drafts` row (id `d_<id>`) or a Touch 1 `emails_sent` row (id
`e_<id>`). Both map onto one `OutreachRow` with one of six status words.
Rows whose status maps to nothing are hidden: the builders return None.

No database access here; tests/test_agent_status_words.py is the section 1
table.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Optional

from services.agent.schemas import (
    DraftVersion,
    EmailThread,
    Fact,
    Flag,
    Followup,
    LeadRef,
    OutreachDetail,
    OutreachRow,
    SentTouch,
)

# ---------------------------------------------------------------- notes (exact copy is contract)

NOTES: dict[str, str] = {
    "drafting": "Writing your email.",
    "needs_review": "Draft ready. Review it.",
    "needs_review_flagged": "Draft ready. Check the marked line.",
    "approved": "Approved. Sending comes next, so nothing has gone out.",
    "skipped": "You passed on this person.",
    "draft_failed": "I couldn't write this one. Try again.",
    "email_scheduled": "Waiting for its send slot.",
    "sent": "Sent.",
    "sent_followup": "Sent. Follow-up planned.",
    "replied": "They wrote back.",
    "bounced": "This address did not accept the email.",
    "email_failed": "This one could not be sent.",
    "expired": "Not sent before the campaign ended. The credit came back to you.",
    # Not in the section 1 table: a discarded draft is hidden from lists, but
    # the discard route still returns the row once, so it needs a line.
    "discarded": "Discarded. This person is back in your matches.",
}

DRAFT_WORDS: dict[str, Optional[str]] = {
    "drafting": "needs_you",
    "needs_review": "needs_you",
    "approved": "scheduled",
    "skipped": "skipped",
    "failed": "failed",
    "discarded": None,
    "superseded": None,
    "sent": None,
}

EMAIL_WORDS: dict[str, str] = {
    "pending_enrichment": "scheduled",
    "queued": "scheduled",
    "sending": "scheduled",
    "sent": "sent",
    "replied": "replied",
    "bounced": "failed",
    "failed": "failed",
    "generation_failed": "failed",
    "expired": "failed",
}

LIVE_DRAFT_STATUSES = ("drafting", "needs_review", "approved")
FOLLOWUP_PLANNED = ("followup_pending", "queued")


def iso(dt: Optional[datetime]) -> Optional[str]:
    """Naive UTC ISO string without microseconds, or None."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.replace(microsecond=0, tzinfo=None).isoformat()


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------- reply text (invariant 3)

REDACTED = "[removed]"
# Where quoted history starts: Gmail's "On <date>, Name <address> wrote:" (it
# may wrap onto a second line), Outlook's "-----Original Message-----" or a
# "From: ... / Sent: ..." block, a forwarded block, or the first ">" line.
_QUOTE_STARTS = (
    re.compile(r"^[ \t]*On [^\n]*(?:\n[^\n]*)?\bwrote:[ \t]*$", re.M),
    re.compile(r"^[ \t]*-{2,}[ \t]*(?:Original Message|Forwarded message)[ \t]*-{2,}", re.M | re.I),
    re.compile(r"^[ \t]*From:[^\n]*\n[ \t]*(?:Sent|Date|To):", re.M | re.I),
    re.compile(r"^[ \t]*>", re.M),
)
_ADDRESS = re.compile(r"(?:mailto:)?[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", re.I)
# A run of digits, spaces, dots, dashes and parentheses (one line only), with an optional "+".
_DIGIT_RUN = re.compile(r"(?<![\w+])\+?\(?\d[\d \t().-]*\d\)?(?!\w)")
_DATE = re.compile(r"\d{4}([-./])\d{1,2}\1\d{1,2}|\d{1,2}([-./])\d{1,2}\2\d{2,4}")
PHONE_MIN_DIGITS = 7


def _phone(m: re.Match) -> str:
    token = m.group(0)
    digits = sum(c.isdigit() for c in token)
    if digits < PHONE_MIN_DIGITS or _DATE.fullmatch(token.strip("()")):
        return token
    return REDACTED


def redact_contact(text: Optional[str]) -> Optional[str]:
    """A hiring manager's reply made safe to show and to give the model: the
    quoted history is cut, and email addresses and phone-like digit runs (7 or
    more digits) become "[removed]". `emails_sent.reply_text` is the raw Gmail
    body, signature and quoted header included, so every read of it goes
    through here (contract invariant 3)."""
    if text is None:
        return None
    cut = len(text)
    for pattern in _QUOTE_STARTS:
        m = pattern.search(text)
        if m is not None:
            cut = min(cut, m.start())
    out = _ADDRESS.sub(REDACTED, text[:cut])
    out = _DIGIT_RUN.sub(_phone, out)
    return out.rstrip()


def preview(body: Optional[str]) -> str:
    """First 140 characters of the body, whitespace collapsed."""
    return " ".join((body or "").split())[:140]


def lead_ref(lead: Any, lead_id: Optional[int] = None) -> LeadRef:
    return LeadRef(
        id=int(_get(lead, "id", lead_id) or lead_id or 0),
        name=(_get(lead, "name") or "").strip() or "Unknown",
        title=_get(lead, "title"),
        company=_get(lead, "company"),
        company_domain=_get(lead, "company_domain"),
    )


# ---------------------------------------------------------------- drafts

def draft_word(status: Optional[str]) -> Optional[str]:
    return DRAFT_WORDS.get(status or "")


def draft_note(status: Optional[str], flags: Optional[list] = None) -> str:
    if status == "needs_review":
        return NOTES["needs_review_flagged"] if flags else NOTES["needs_review"]
    if status == "failed":
        return NOTES["draft_failed"]
    if status in ("discarded", "superseded"):
        return NOTES["discarded"]
    return NOTES.get(status or "", "")


def draft_row(draft: Any, lead: Any) -> Optional[OutreachRow]:
    """The OutreachRow for an agent_drafts row, or None when it is hidden."""
    status = _get(draft, "status")
    word = draft_word(status)
    if word is None:
        return None
    return OutreachRow(
        id=f"d_{_get(draft, 'id')}",
        kind="draft",
        lead=lead_ref(lead, _get(draft, "lead_id")),
        subject=_get(draft, "subject") or "",
        preview="" if status == "drafting" else preview(_get(draft, "body")),
        status_word=word,
        server_status=status,
        note=draft_note(status, _get(draft, "flags")),
        busy=status == "drafting",
        when=iso(_get(draft, "updated_at")),
        version=_get(draft, "version"),
        followups=[],
    )


# ---------------------------------------------------------------- emails

def email_word(status: Optional[str]) -> Optional[str]:
    return EMAIL_WORDS.get(status or "")


def email_note(status: Optional[str], followups: Iterable[Any] = ()) -> str:
    if status in ("pending_enrichment", "queued", "sending"):
        return NOTES["email_scheduled"]
    if status == "sent":
        planned = any(_get(f, "status") in FOLLOWUP_PLANNED for f in followups or ())
        return NOTES["sent_followup"] if planned else NOTES["sent"]
    if status in ("replied", "bounced", "expired"):
        return NOTES[status]
    if status in ("failed", "generation_failed"):
        return NOTES["email_failed"]
    return ""


def email_when(email: Any) -> Optional[str]:
    for name in ("reply_received_at", "sent_at", "scheduled_at", "status_changed_at"):
        value = _get(email, name)
        if value is not None:
            return iso(value)
    return None


def followup(f: Any) -> Followup:
    return Followup(
        number=int(_get(f, "followup_number") or _get(f, "number") or 0),
        status=_get(f, "status") or "",
        scheduled_at=iso(_get(f, "scheduled_at")),
        sent_at=iso(_get(f, "sent_at")),
    )


def email_row(email: Any, lead: Any, followups: Iterable[Any] = ()) -> Optional[OutreachRow]:
    """The OutreachRow for a Touch 1 emails_sent row, or None when hidden.
    `followups` are its Touch 2 and 3 rows (any order)."""
    status = _get(email, "status")
    word = email_word(status)
    if word is None:
        return None
    fus = sorted(list(followups or ()), key=lambda f: _get(f, "followup_number") or 0)
    return OutreachRow(
        id=f"e_{_get(email, 'id')}",
        kind="email",
        lead=lead_ref(lead, _get(email, "lead_id")),
        subject=_get(email, "subject") or "",
        preview=preview(_get(email, "body")),
        status_word=word,
        server_status=status,
        note=email_note(status, fus),
        busy=False,
        when=email_when(email),
        version=None,
        followups=[followup(f) for f in fus],
    )


def reply_group(email: Any) -> Optional[str]:
    """Inbox group of a Touch 1 row: positive, not_now, question, bounced.
    None for rows that are not replies or bounces."""
    status = _get(email, "status")
    if status == "bounced":
        return "bounced"
    if status != "replied":
        return None
    sentiment = _get(email, "reply_sentiment")
    if sentiment == "positive":
        return "positive"
    if sentiment == "negative":
        return "not_now"
    return "question"


# ---------------------------------------------------------------- details

EDITABLE = ("needs_review", "approved")
SKIPPABLE = ("drafting", "needs_review", "approved", "failed")
DISCARDABLE = ("needs_review", "approved", "failed")
_SENTIMENTS = ("positive", "negative", "neutral")


def _models(cls, items: Any) -> list:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            try:
                out.append(cls(**{k: item.get(k) for k in cls.model_fields if k in item}))
            except Exception:  # a malformed stored item is skipped, never a 500
                continue
    return out


def draft_detail(draft: Any, lead: Any) -> OutreachDetail:
    """The full OutreachDetail of a draft. Hidden statuses (discarded,
    superseded) still render, as `skipped` with the discarded note, because the
    discard route returns the row one last time."""
    status = _get(draft, "status")
    flags = _models(Flag, _get(draft, "flags"))
    return OutreachDetail(
        id=f"d_{_get(draft, 'id')}",
        kind="draft",
        lead=lead_ref(lead, _get(draft, "lead_id")),
        subject=_get(draft, "subject") or "",
        preview="" if status == "drafting" else preview(_get(draft, "body")),
        status_word=draft_word(status) or "skipped",
        server_status=status,
        note=draft_note(status, flags),
        busy=status == "drafting",
        when=iso(_get(draft, "updated_at")),
        version=_get(draft, "version"),
        followups=[],
        body=_get(draft, "body") or "",
        style=_get(draft, "style"),
        instructions=_get(draft, "instructions"),
        facts_used=_models(Fact, _get(draft, "facts_used")),
        flags=flags,
        versions=_models(DraftVersion, _get(draft, "versions")),
        approved_at=iso(_get(draft, "approved_at")),
        can_edit=status in EDITABLE,
        can_approve=status == "needs_review",
        can_skip=status in SKIPPABLE,
        can_discard=status in DISCARDABLE,
        send_blocker="sending_off" if status in EDITABLE else None,
        thread=None,
    )


def email_detail(email: Any, lead: Any, followups: Iterable[Any] = (),
                 failure_note: Optional[str] = None) -> Optional[OutreachDetail]:
    """The full OutreachDetail of a Touch 1 email, or None when hidden.
    `failure_note` is customer_failure_reason, never the raw error_message."""
    row = email_row(email, lead, followups)
    if row is None:
        return None
    fus = sorted(list(followups or ()), key=lambda f: _get(f, "followup_number") or 0)
    touches = [SentTouch(number=0, status=_get(email, "status") or "", subject=_get(email, "subject"),
                         body=_get(email, "body"), sent_at=iso(_get(email, "sent_at")))]
    for f in fus:
        touches.append(SentTouch(number=int(_get(f, "followup_number") or 0), status=_get(f, "status") or "",
                                 subject=_get(f, "subject"), body=_get(f, "body"),
                                 sent_at=iso(_get(f, "sent_at"))))
    sentiment = _get(email, "reply_sentiment")
    return OutreachDetail(
        **row.model_dump(),
        body=_get(email, "body") or "",
        can_edit=False, can_approve=False, can_skip=False, can_discard=False,
        thread=EmailThread(
            touches=touches,
            reply_text=redact_contact(_get(email, "reply_text")) or None,
            reply_sentiment=sentiment if sentiment in _SENTIMENTS else None,
            reply_received_at=iso(_get(email, "reply_received_at")),
            failure_note=failure_note,
        ),
    )


def sort_key(row: OutreachRow) -> tuple:
    """Lists sort by `when` desc, then id desc: use with reverse=True."""
    return (row.when or "", int(row.id.split("_", 1)[1]))
