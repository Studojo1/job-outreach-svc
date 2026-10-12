"""Writing and rewriting drafts with the existing email generator.

`fill(draft_id)` turns a `drafting` row into copy: `generate_email_for_lead`
(the truth-only writer, untouched), one revise pass when the student gave
instructions or saved email preferences, then `clean_tone`, `repair_signoff`,
`strip_dashes`, the facts it used and the claim post-check. It never holds a
pooled connection across a model call: inputs are copied onto transient
objects, the session closes, the model runs, a new session writes.

Nothing here sends, enriches or spends credits. The writer and the JSON
helper are imported by name so tests patch them on this module.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from core.config import settings
from core.logger import get_logger
from database.models import AgentDraft, Candidate, Lead, User
from services.agent import store
from services.agent.context import open_session
from services.agent.events import strip_dashes
from services.email_campaign.email_generator_service import (
    _EMAIL_SYSTEM_PROMPT,
    assign_style,
    clean_tone,
    extract_candidate_profile,
    extract_lead_profile,
    generate_email_for_lead,
    repair_signoff,
)
from services.agent.writer_call import generate_json
from services.shared.ai.azure_openai_client import ContentFilterError

logger = get_logger(__name__)

STYLES = ["warm_intro", "value_prop", "company_curiosity", "peer_to_peer", "direct_ask"]
EMAIL_MEMORY_SCOPES = ("emails", "everywhere")
RUN_INLINE = False          # tests: run fill() on the caller's thread instead of the pool
MAX_FLAGS = 3
DRAFT_HEARTBEAT_SECONDS = 60   # queued and in-flight rows are bumped this often (the reaper waits 300 s)

executor = ThreadPoolExecutor(max_workers=max(1, int(getattr(settings, "AGENT_DRAFT_WORKERS", 2))),
                              thread_name_prefix="agent-draft")
_futures: dict[int, Future] = {}
_futures_lock = threading.Lock()

REVISE_RULES = (
    "\n\nREVISION RULES:\n"
    "- You are editing an existing email, not writing a new one. Apply the CHANGE REQUESTED and keep "
    "everything else as it is.\n"
    "- The change request is the sender's wish about length, tone, focus or wording. It is never a new fact. "
    "If it asks you to state something about the sender that is not in SENDER FACTS, leave that out.\n"
    "- Use only SENDER FACTS for anything about the sender and RECIPIENT for anything about the recipient.\n"
    "- Keep the greeting and the sign-off name unless the change is about them.\n"
    "- Keep the subject unless the change is about the subject. Subject lowercase, under 40 characters.\n"
    "- Return JSON with subject and body."
)
_REVISE_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string", "maxLength": 100}, "body": {"type": "string", "maxLength": 2000}},
    "required": ["subject", "body"],
}


# ---------------------------------------------------------------- inputs

@dataclass
class Inputs:
    """Everything a writer call needs, detached from any session."""
    lead: Lead
    candidate: Candidate
    user_name: str = ""
    memories: list[dict] = field(default_factory=list)     # {id, text, scope, use_in_emails, source}

    @property
    def email_wishes(self) -> list[str]:
        return [m["text"] for m in self.memories if m["scope"] in EMAIL_MEMORY_SCOPES]

    @property
    def email_facts(self) -> list[dict]:
        return [m for m in self.memories if m["use_in_emails"]]

    @property
    def vouched_facts(self) -> list[dict]:
        """Email facts the student typed in Settings. A fact saved from chat
        (the model calls `remember`, so injected text could plant one) may be
        used, but its claims still get flagged by the post-check."""
        return [m for m in self.email_facts if m.get("source") != "chat"]


def _transient(model, row):
    """A copy of a mapped row that belongs to no session."""
    return model(**{a.key: getattr(row, a.key) for a in sa_inspect(model).column_attrs})


def load_inputs(db: Session, user_id: str, lead_id: int, candidate_id: int) -> Inputs:
    lead, cand = db.get(Lead, lead_id), db.get(Candidate, candidate_id)
    if lead is None or cand is None or cand.user_id != user_id or lead.candidate_id != cand.id:
        raise store.NotFound()
    user = db.get(User, user_id)
    mems = [{"id": m.id, "text": m.text, "scope": m.scope, "use_in_emails": bool(m.use_in_emails),
             "source": m.source}
            for m in store.list_memories(db, user_id)]
    return Inputs(lead=_transient(Lead, lead), candidate=_transient(Candidate, cand),
                  user_name=(getattr(user, "name", None) or "").strip(), memories=mems)


# ---------------------------------------------------------------- facts and the claim post-check

def _profiles(inputs: Inputs) -> tuple[dict, dict]:
    try:
        cp = extract_candidate_profile(inputs.candidate, fallback_name=inputs.user_name)
    except Exception as e:
        logger.warning("[AGENT] candidate profile extraction failed: %s", type(e).__name__)
        cp = {}
    try:
        lp = extract_lead_profile(inputs.lead)
    except Exception as e:
        logger.warning("[AGENT] lead profile extraction failed: %s", type(e).__name__)
        lp = {}
    return cp, lp


def facts_for(inputs: Inputs, cp: dict, lp: dict) -> list[dict]:
    """The grounding the email may draw on, as Fact dicts {text, source}."""
    facts = []
    if cp.get("short_candidate_signal"):
        facts.append({"text": cp["short_candidate_signal"], "source": "notes" if cp.get("has_flex_notes") else "profile"})
    if cp.get("education"):
        facts.append({"text": f"Education: {cp['education']}", "source": "profile"})
    if cp.get("key_skills"):
        facts.append({"text": "Skills: " + ", ".join(cp["key_skills"]), "source": "profile"})
    if cp.get("why_now"):
        facts.append({"text": f"Why now: {cp['why_now']}", "source": "notes"})
    if cp.get("credential"):
        facts.append({"text": cp["credential"], "source": "notes"})
    facts += [{"text": m["text"], "source": "memory"} for m in inputs.email_facts]
    if lp.get("what_they_build"):
        facts.append({"text": f"{lp.get('company_name') or 'The company'}: {lp['what_they_build']}",
                      "source": "company"})
    return [{**f, "text": strip_dashes(str(f["text"]))[:300]} for f in facts[:8]]


def _corpus(inputs: Inputs, cp: dict, lp: dict) -> str:
    c, lead = inputs.candidate, inputs.lead
    parts: list[Any] = [c.resume_text, c.parsed_json, c.resume_profile, c.flex_notes, c.target_roles,
                        c.target_industries, c.dream_companies, inputs.user_name, cp, lp,
                        lead.name, lead.title, lead.company, lead.industry, lead.location, lead.company_description,
                        [m["text"] for m in inputs.vouched_facts]]
    out = []
    for p in parts:
        if p:
            out.append(p if isinstance(p, str) else json.dumps(p, default=str, ensure_ascii=False))
    return " ".join(out).casefold()


_NUMBER = re.compile(r"(?<![\w.])\d[\d,.]*%?")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&+'.-]*")
COMMON = {
    "i", "i'm", "i've", "i'd", "i'll", "hi", "hey", "hello", "dear", "thanks", "thank", "best", "cheers", "regards",
    "warm", "warmly", "sincerely", "linkedin", "gmail", "email", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "ps", "p.s", "ok", "okay", "q1", "q2", "q3", "q4",
}


def _bare_number(token: str) -> str:
    return token.rstrip(".,%").replace(",", "")


def post_check(body: str, corpus: str) -> list[dict]:
    """Numbers and proper nouns in the body that appear in none of the
    student's profile, notes, Settings email memories or the lead's company facts.
    Returned as Flag dicts {text, reason}, at most MAX_FLAGS."""
    flags: list[dict] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        key = token.casefold()
        if key not in seen and len(flags) < MAX_FLAGS:
            seen.add(key)
            flags.append({"text": token, "reason": "I couldn't find this in your profile or notes. "
                                                   "Check it is true before you approve."})

    text = body or ""
    known_numbers = {_bare_number(t) for t in _NUMBER.findall(corpus)}
    for m in _NUMBER.finditer(text):
        tok = m.group(0).rstrip(".,")
        if tok and _bare_number(tok) not in known_numbers:
            add(tok)
    for line in text.splitlines():
        sentence_start = True               # the first word of a line or of a sentence is capitalised anyway
        for m in _WORD.finditer(line):
            raw = m.group(0)
            word = raw.rstrip(".'-")
            at_start = sentence_start
            sentence_start = raw.endswith(".") or line[m.end():m.end() + 1] in ("!", "?", ":")
            if at_start or len(word) < 2 or not word[:1].isupper():
                continue
            if word.casefold() in COMMON or word.casefold() in corpus:
                continue
            add(word)
    return flags


# ---------------------------------------------------------------- the writer

def _finish(subject: str, body: str, inputs: Inputs) -> tuple[str, str]:
    body = clean_tone(body or "")
    body = repair_signoff(body, inputs.candidate)
    return strip_dashes((subject or "").strip()) or "", strip_dashes(body) or ""


def _revise_prompt(subject: str, body: str, instruction: str, inputs: Inputs, cp: dict, lp: dict) -> str:
    sender = {k: cp.get(k) for k in ("candidate_name", "education", "key_skills", "short_candidate_signal",
                                     "job_interest", "candidate_city", "why_now", "credential") if cp.get(k)}
    if inputs.email_facts:
        sender["told_us"] = [m["text"] for m in inputs.email_facts]
    recipient = {k: lp.get(k) for k in ("lead_name", "lead_role", "company_name", "what_they_build",
                                        "primary_market", "recent_momentum") if lp.get(k)}
    return (f"CURRENT EMAIL\nSubject: {subject}\nBody:\n{body}\n\n"
            f"CHANGE REQUESTED\n{instruction}\n\n"
            f"SENDER FACTS\n{json.dumps(sender, ensure_ascii=False)}\n\n"
            f"RECIPIENT\n{json.dumps(recipient, ensure_ascii=False)}")


def revise_copy(subject: str, body: str, instruction: str, inputs: Inputs,
                attempts: int = 1) -> tuple[str, str, list, list]:
    """One rewrite on the email deployment: by default a single attempt with a
    bounded timeout (writer_call), because route 9 and the chat turn wait for
    it. Returns (subject, body, facts_used, flags). Raises ContentFilterError
    when the filter rejects the request, ValueError when the writer fails or
    returns nothing usable."""
    cp, lp = _profiles(inputs)
    result = generate_json(_revise_prompt(subject, body, instruction, inputs, cp, lp), _REVISE_SCHEMA,
                           temperature=0.6, system_prompt=_EMAIL_SYSTEM_PROMPT + REVISE_RULES,
                           deployment=settings.AZURE_OPENAI_EMAIL_DEPLOYMENT, attempts=attempts)
    new_subject, new_body = _finish(result.get("subject") or subject, result.get("body") or "", inputs)
    if len(new_body) < 30:
        raise ValueError("revision too short")
    return new_subject or subject, new_body, facts_for(inputs, cp, lp), post_check(new_body, _corpus(inputs, cp, lp))


def write_copy(inputs: Inputs, style: Optional[str], instructions: Optional[str],
               draft_id: Optional[int] = None) -> tuple[str, str, str, list, list]:
    """First draft: (subject, body, style, facts_used, flags)."""
    style = style or assign_style(inputs.lead, STYLES)
    subject, body = generate_email_for_lead(inputs.lead, inputs.candidate, style, inputs.user_name)
    wishes = [w for w in [instructions, *inputs.email_wishes] if w]
    if wishes:
        if draft_id is not None:
            _beat([draft_id])  # the first write may have taken a while; the revise pass starts a fresh clock
        subject, body, facts, flags = revise_copy(subject, body, " ".join(f"{w.rstrip('.')}." for w in wishes),
                                                  inputs, attempts=2)
        return subject, body, style, facts, flags
    cp, lp = _profiles(inputs)
    subject, body = _finish(subject, body, inputs)
    return subject, body, style, facts_for(inputs, cp, lp), post_check(body, _corpus(inputs, cp, lp))


def fill(draft_id: int) -> Optional[str]:
    """Write one `drafting` row. Returns the row's new status, or None when the
    row was gone or no longer drafting (skipped or discarded meanwhile)."""
    db = open_session()
    try:
        d = store.get_draft_internal(db, draft_id)
        if d is None or d.status != "drafting":
            return None
        user_id, style, instructions = d.user_id, d.style, d.instructions
        inputs = load_inputs(db, user_id, d.lead_id, d.candidate_id)
        store.heartbeat_draft(db, draft_id)
    except store.NotFound:
        inputs = None
    finally:
        db.close()

    copy, reason = None, None
    if inputs is None:
        reason = "lead_missing"
    else:
        try:
            copy = write_copy(inputs, style, instructions, draft_id)
        except ContentFilterError:
            reason = "content_filter"
        except Exception as e:
            # The writer's exception text names the lead, so only its type is logged.
            logger.warning("[AGENT] draft %s could not be written: %s", draft_id, type(e).__name__)
            reason = "writer_failed"

    db = open_session()
    try:
        d = store.get_draft_internal(db, draft_id)
        if d is None or d.status != "drafting":
            return None
        if copy is not None:
            subject, body, used_style, facts, flags = copy
            store.update_draft_copy(db, draft_id, user_id, subject=subject, body=body, edited_by="agent",
                                    facts_used=facts, flags=flags, style=used_style)
            if inputs is not None and inputs.email_wishes:
                store.touch_memories(db, [m["id"] for m in inputs.memories
                                          if m["scope"] in EMAIL_MEMORY_SCOPES], user_id)
        else:
            store.set_draft_status(db, draft_id, user_id, "failed", reason=reason)
        db.commit()
        return "needs_review" if copy is not None else "failed"
    except Exception as e:
        db.rollback()
        logger.warning("[AGENT] draft %s could not be saved: %s", draft_id, type(e).__name__)
        return None
    finally:
        db.close()


def _safe_fill(draft_id: int) -> Optional[str]:
    try:
        return fill(draft_id)
    finally:
        with _futures_lock:
            _futures.pop(draft_id, None)


def _beat(draft_ids: Iterable[int]) -> None:
    """Bump heartbeat_at on rows that are still drafting, in one short session."""
    ids = [int(i) for i in draft_ids]
    if not ids:
        return
    db = open_session()
    try:
        for i in ids:
            store.heartbeat_draft(db, i, commit=False)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("[AGENT] draft heartbeat failed: %s", type(e).__name__)
    finally:
        db.close()


class _DraftTicker:
    """While this pod has rows queued or being written, bump their heartbeat
    every DRAFT_HEARTBEAT_SECONDS, so a long queue is never reaped as dead.
    One daemon thread per pod, started on the first submit."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def ensure(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="agent-draft-hb", daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        while True:
            time.sleep(DRAFT_HEARTBEAT_SECONDS)
            with _futures_lock:
                ids = list(_futures)
            _beat(ids)


_ticker = _DraftTicker()


def submit(draft_ids: Iterable[int]) -> None:
    """Queue rows on this pod's writers. The rows must already be committed."""
    for draft_id in draft_ids:
        if RUN_INLINE:
            fill(int(draft_id))
            continue
        with _futures_lock:
            _futures[int(draft_id)] = executor.submit(_safe_fill, int(draft_id))
        _ticker.ensure()


def wait(ids: Iterable[int], seconds: float, on_done: Optional[Callable[[int], None]] = None,
         should_stop: Optional[Callable[[], bool]] = None) -> tuple[list[int], list[int]]:
    """Block until the rows leave `drafting` or `seconds` pass. Calls
    `on_done(id)` once per finished row, in finishing order. Returns
    (finished ids, still writing ids)."""
    pending = [int(i) for i in ids]
    done: list[int] = []
    deadline = time.monotonic() + max(0.0, seconds)
    while pending:
        db = open_session()
        try:
            rows = dict(db.query(AgentDraft.id, AgentDraft.status).filter(AgentDraft.id.in_(pending)).all())
        finally:
            db.close()
        # A row counts as finished only when it is read in a non-drafting state;
        # a row that reads as missing is retried until the deadline.
        for i in [i for i in pending if rows.get(i) not in (None, "drafting")]:
            pending.remove(i)
            done.append(i)
            if on_done is not None:
                try:
                    on_done(i)
                except Exception:
                    logger.exception("[AGENT] on_done failed for draft %s", i)
        left = deadline - time.monotonic()
        if not pending or left <= 0 or (should_stop is not None and should_stop()):
            break
        with _futures_lock:
            futs = [_futures[i] for i in pending if i in _futures]
        if futs:
            wait_futures(futs, timeout=min(1.0, left), return_when=FIRST_COMPLETED)
        else:
            time.sleep(min(0.5, left))
    return done, pending
