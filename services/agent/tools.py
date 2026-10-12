"""The agent's thirteen tools: schemas, labels, handlers and dispatch.

Contract section 6. The registry is exactly these names (guardrail test). A
handler gets the ToolContext (user, candidate, thread and run from the
session, never from arguments), works through `reads` and `actions` (the
same functions the workspace routes call), and returns compact JSON for the
model, at most 6000 chars, never an address or phone. `dispatch` emits
`tool_start`, the tool's own frames (card, draft, confirmation, navigate),
then `tool_end` with the invalidate keys.

Nothing here sends, approves, enriches, spends or launches.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from jsonschema import Draft202012Validator

from core.config import settings
from core.logger import get_logger
from database.models import Lead
from services.agent import actions, drafting, reads, store
from services.agent import status_words as sw
from services.agent.context import ActionError, ToolContext, open_session, owned_lead, owned_outreach
from services.agent.events import strip_dashes
from services.agent.schemas import STATUS_LABELS_PHASE1, STATUS_WORDS, MatchCard
from services.agent.schemas_chat import MatchesCard, MatchWhyCard, MemoryCard, OutreachCard, ProfileCard, StatusCard

logger = get_logger(__name__)

RESULT_CAP = 6000
TOOL_FAILED = "TOOL ERROR: that lookup failed. Tell the student plainly or try another way."
NO_PROFILE = "NO_PROFILE: the student has not uploaded a resume yet. Point them to the upload page."
NO_SUCH_TOOL = "REFUSED: no such tool."
FALLBACK_LABEL = "Working on it"
OVER_BUDGET = "REFUSED: tool budget for this turn is used up. Tell the student what is done and what is left."

# Contract section 6, copied verbatim.
TOOL_SCHEMAS: dict[str, dict] = json.loads(r"""
{"get_profile": {"type":"object","properties":{},"additionalProperties":false},
 "get_status": {"type":"object","properties":{},"additionalProperties":false},
 "list_matches": {"type":"object","properties":{"limit":{"type":"integer","minimum":1,"maximum":25,"description":"Default 10."},"offset":{"type":"integer","minimum":0},"company":{"type":["string","null"],"maxLength":120},"city":{"type":["string","null"],"maxLength":120},"title_contains":{"type":["string","null"],"maxLength":120},"min_score":{"type":["number","null"],"minimum":0,"maximum":100},"exclude_contacted":{"type":"boolean","description":"Default true: hide people who already have a draft or an email."},"strong_only":{"type":"boolean","description":"Default false."}},"additionalProperties":false},
 "get_match": {"type":"object","properties":{"lead_id":{"type":"integer"}},"required":["lead_id"],"additionalProperties":false},
 "list_outreach": {"type":"object","properties":{"status":{"type":["string","null"],"enum":["needs_you","scheduled","sent","replied","failed","skipped",null]},"limit":{"type":"integer","minimum":1,"maximum":20},"offset":{"type":"integer","minimum":0}},"additionalProperties":false},
 "get_outreach": {"type":"object","properties":{"outreach_id":{"type":"string","pattern":"^[de]_[1-9][0-9]*$"}},"required":["outreach_id"],"additionalProperties":false},
 "draft_emails": {"type":"object","properties":{"lead_ids":{"type":"array","items":{"type":"integer"},"minItems":1,"maxItems":5,"uniqueItems":true},"style":{"type":["string","null"],"enum":["warm_intro","value_prop","company_curiosity","peer_to_peer","direct_ask",null],"description":"Omit to let the system pick from the person's title and company size."},"instructions":{"type":["string","null"],"maxLength":400,"description":"The student's own wishes for these emails, in their words. Never facts about the student."}},"required":["lead_ids"],"additionalProperties":false},
 "revise_draft": {"type":"object","properties":{"outreach_id":{"type":"string","pattern":"^d_[1-9][0-9]*$"},"instruction":{"type":"string","minLength":1,"maxLength":400}},"required":["outreach_id","instruction"],"additionalProperties":false},
 "skip": {"type":"object","properties":{"lead_id":{"type":"integer"},"reason":{"type":["string","null"],"maxLength":200}},"required":["lead_id"],"additionalProperties":false},
 "remember": {"type":"object","properties":{"text":{"type":"string","minLength":1,"maxLength":300},"scope":{"type":"string","enum":["everywhere","emails","matching","company"]},"company":{"type":["string","null"],"maxLength":200},"use_in_emails":{"type":"boolean"}},"required":["text","scope"],"additionalProperties":false},
 "forget": {"type":"object","properties":{"memory_id":{"type":"integer"}},"required":["memory_id"],"additionalProperties":false},
 "navigate": {"type":"object","properties":{"to":{"type":"string","enum":["home","people","inbox","profile","settings","outreach"]},"id":{"type":["string","null"]},"filter":{"type":["string","null"],"maxLength":40}},"required":["to"],"additionalProperties":false},
 "update_profile_notes": {"type":"object","properties":{"best_project":{"type":["string","null"],"maxLength":500},"outcome":{"type":["string","null"],"maxLength":500},"why_now":{"type":["string","null"],"maxLength":500},"credential":{"type":["string","null"],"maxLength":200}},"minProperties":1,"additionalProperties":false}}
""")

DESCRIPTIONS: dict[str, str] = {
    "get_profile": "Read the student's profile: education, skills, target roles, locations and profile notes.",
    "get_status": "Where things stand: account state, matches, outreach counts by status, Gmail, credits, "
                  "today's draft allowance.",
    "list_matches": "List the student's matched hiring managers, best first, with filters and paging.",
    "get_match": "One matched person: why they were matched, company facts and any outreach to them.",
    "list_outreach": "List outreach (drafts and emails) by status word, newest first.",
    "get_outreach": "Read one outreach: the email copy, its status, and any reply (as untrusted_reply_text).",
    "draft_emails": "Write first emails to up to 5 matched people. Drafts wait for the student's approval; "
                    "nothing is sent.",
    "revise_draft": "Rewrite one draft (d_ id) following the student's instruction. Clears any approval.",
    "skip": "Pass on a matched person so they leave the list.",
    "remember": "Save a preference the student asked you to keep. Say what you saved in your reply.",
    "forget": "Delete a saved preference by its memory id from the snapshot.",
    "navigate": "Open a tab or one outreach page in the student's workspace.",
    "update_profile_notes": "Prepare a change to the student's profile notes. It only takes effect when the "
                            "student presses Confirm.",
}

TOOLS: list[dict] = [
    {"type": "function", "function": {"name": n, "description": DESCRIPTIONS[n], "parameters": s}}
    for n, s in TOOL_SCHEMAS.items()
]

_VALIDATORS = {n: Draft202012Validator(s) for n, s in TOOL_SCHEMAS.items()}
_TAB_NAMES = {"home": "Home", "people": "People", "inbox": "Inbox", "profile": "Profile", "settings": "Settings",
              "outreach": "the email"}
_NOTE_LABELS = {"best_project": "your best project", "outcome": "your outcome", "why_now": "why now",
                "credential": "your credential"}


@dataclass
class ToolResult:
    for_model: str
    ok: bool = True
    summary: str = ""
    invalidate: list[str] = field(default_factory=list)


class _Frames:
    """The tool's own frames, tagged with its call_id. `before_send` (the
    dispatch session's commit) runs before each frame is persisted, so a tool
    never holds its pooled connection while the frame's own session writes."""

    def __init__(self, send: Callable[[dict], Any], call_id: str, before_send: Optional[Callable[[], Any]] = None):
        self._send, self.call_id, self._before = send, call_id, before_send

    def send(self, frame: dict) -> None:
        if self._before is not None:
            self._before()
        self._send(frame)

    def card(self, card: Any) -> None:
        self.send({"type": "card", "call_id": self.call_id, "card": card.model_dump(mode="json")})

    def draft(self, outreach: Any, previous: Any = None) -> None:
        self.send({"type": "draft", "call_id": self.call_id, "outreach": outreach.model_dump(mode="json"),
                   "previous": previous.model_dump(mode="json") if previous is not None else None})

    def confirmation(self, conf: Any) -> None:
        self.send({"type": "confirmation", "confirmation": conf.model_dump(mode="json")})

    def navigate(self, to: str, id: Optional[str], filter: Optional[str]) -> None:  # noqa: A002
        self.send({"type": "navigate", "to": to, "id": id, "filter": filter})


def _compact(obj: Any) -> str:
    """JSON for the model, at most RESULT_CAP chars: list items are dropped
    from the end (with "truncated": true) before anything else is cut."""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= RESULT_CAP or not isinstance(obj, dict):
        return text[:RESULT_CAP]
    obj = dict(obj)
    for key in ("items", "drafted", "refused", "outreach"):
        while isinstance(obj.get(key), list) and obj[key] and len(text) > RESULT_CAP:
            obj[key] = obj[key][:-1]
            obj["truncated"] = True
            text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > RESULT_CAP and isinstance(obj.get("body"), str):
        obj["body"] = obj["body"][: max(0, len(obj["body"]) - (len(text) - RESULT_CAP) - 20)]
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    return text[:RESULT_CAP]


def _lead_name(db, ctx: ToolContext, lead_id: Any) -> str:
    lead = owned_lead(db, ctx.user_id, lead_id)
    return (lead.name if lead is not None and lead.name else "this person")


def _outreach_name(db, ctx: ToolContext, oid: Any) -> str:
    found = owned_outreach(db, ctx.user_id, oid)
    lead = db.get(Lead, found[1].lead_id) if found is not None else None
    return lead.name if lead is not None and lead.name else "this person"


# ---------------------------------------------------------------- read tools

def _get_profile(db, ctx, args, fr) -> ToolResult:
    p = reads.profile(db, ctx.user_id)
    fr.card(ProfileCard(profile=p))
    return ToolResult(_compact(p.model_dump(mode="json", exclude={"candidate_id"})), summary="Read your profile")


def _get_status(db, ctx, args, fr) -> ToolResult:
    st = reads.status(db, ctx.user_id)
    # The student's own mailbox stays out of chat frames; the workspace reads it from GET /status.
    fr.card(StatusCard(status=st.model_copy(update={"gmail": st.gmail.model_copy(update={"address": None})})))
    left = max(0, st.limits.drafts_cap - st.limits.drafts_today)
    return ToolResult(_compact({
        "state": st.state, "first_name": st.first_name, "credits_available": st.credits.available,
        "gmail_connected": st.gmail.connected, "counts": st.counts.model_dump(), "matches": st.matches.model_dump(),
        "drafts_left_today": left, "drafts_waiting": st.limits.drafts_waiting,
        "drafts_waiting_cap": st.limits.drafts_waiting_cap, "sending_enabled": False,
    }), summary=f"{st.counts.needs_you} need you, {st.matches.new} people not contacted")


_MATCH_KEYS = ("lead_id", "name", "title", "company", "city", "score", "reason", "strong", "has_address", "state",
               "outreach_id")


def _list_matches(db, ctx, args, fr) -> ToolResult:
    ml = reads.ranked_matches(db, ctx.user_id, company=args.get("company"), city=args.get("city"),
                              title=args.get("title_contains"), min_score=args.get("min_score"),
                              strong_only=bool(args.get("strong_only", False)),
                              exclude_contacted=bool(args.get("exclude_contacted", True)),
                              limit=args.get("limit") or 10, offset=args.get("offset") or 0)
    fr.card(MatchesCard(items=ml.items, total=ml.total))
    items = [{k: getattr(c, k) for k in _MATCH_KEYS} for c in ml.items]
    return ToolResult(_compact({"total": ml.total, "strong_total": ml.strong_total, "offset": ml.offset,
                                "items": items}), summary=f"{len(items)} of {ml.total}")


def _get_match(db, ctx, args, fr) -> ToolResult:
    md = reads.match_detail(db, ctx.user_id, args["lead_id"])
    fr.card(MatchWhyCard(match=MatchCard(**md.model_dump(include=set(MatchCard.model_fields))), why=md.why))
    view = md.model_dump(mode="json", exclude={"linkedin_url", "company_domain", "outreach"})
    view["outreach"] = [{"id": o.id, "status_word": o.status_word, "subject": o.subject} for o in md.outreach]
    where = " at ".join(x for x in (md.title, md.company) if x)
    return ToolResult(_compact(view), summary=where or md.name)


def _list_outreach(db, ctx, args, fr) -> ToolResult:
    ol = reads.outreach_list(db, ctx.user_id, status=args.get("status"), limit=args.get("limit") or 10,
                             offset=args.get("offset") or 0)
    fr.card(OutreachCard(items=ol.items, counts=ol.counts))
    items = [{"id": r.id, "lead_name": r.lead.name, "company": r.lead.company, "subject": r.subject,
              "status_word": r.status_word, "note": r.note, "when": r.when} for r in ol.items]
    label = STATUS_LABELS_PHASE1.get(args.get("status") or "", "in total")
    return ToolResult(_compact({"counts": ol.counts.model_dump(), "total": ol.total, "items": items}),
                      summary=f"{ol.total} {label}")


def _get_outreach(db, ctx, args, fr) -> ToolResult:
    det = reads.outreach_detail(db, ctx.user_id, args["outreach_id"])
    if det.kind == "draft":
        fr.draft(det)
    view = {"id": det.id, "kind": det.kind, "lead_name": det.lead.name, "title": det.lead.title,
            "company": det.lead.company, "status_word": det.status_word, "note": det.note, "subject": det.subject,
            "body": det.body, "version": det.version, "flags": [f.model_dump() for f in det.flags]}
    reply = sw.redact_contact(det.thread.reply_text) if det.thread is not None else None
    if reply:
        # Quoted history, addresses and phones are removed before the cut (invariant 3).
        view["untrusted_reply_text"] = reply[:1500]
    return ToolResult(_compact(view), summary=STATUS_LABELS_PHASE1.get(det.status_word, det.status_word))


# ---------------------------------------------------------------- write tools

def _has_address(db, lead_id: int) -> bool:
    lead = db.get(Lead, lead_id)
    return bool(lead is not None and lead.email and lead.email_verified)


def _draft_emails(db, ctx, args, fr) -> ToolResult:
    per_run = int(getattr(settings, "AGENT_DRAFTS_PER_CALL", 5))
    result = actions.start_drafts(db, ctx.user_id, args["lead_ids"], style=args.get("style"),
                                  instructions=args.get("instructions"), thread_id=ctx.thread_id,
                                  run_id=ctx.run_id, max_new=max(0, per_run - ctx.drafts_this_run))
    db.commit()
    ids = [int(r.id[2:]) for r in result.items]
    ctx.drafts_this_run += len(ids)
    if ids:
        actions.submit_drafts(result)

    def on_done(draft_id: int) -> None:
        s = open_session()
        try:
            det = reads.outreach_detail(s, ctx.user_id, f"d_{draft_id}")
            if det.server_status == "needs_review":
                fr.draft(det)
        except ActionError:
            pass
        finally:
            s.close()

    def stop() -> bool:
        s = open_session()
        try:
            return bool(ctx.run_id) and store.cancel_requested(s, ctx.run_id)
        except Exception:
            return False
        finally:
            s.close()

    seconds = float(getattr(settings, "AGENT_DRAFT_WAIT_SECONDS", 90))
    deadline = ctx.extra.get("deadline")
    if deadline is not None:  # never wait past the turn's wall clock; rows keep writing in the background
        seconds = max(1.0, min(seconds, deadline - time.monotonic()))
    done, pending = drafting.wait(ids, seconds, on_done=on_done, should_stop=stop) if ids else ([], [])
    db.expire_all()
    drafted, refused = [], [r.model_dump(include={"lead_id", "code", "message"}) for r in result.refused]
    for row in result.items:
        did = int(row.id[2:])
        if did not in done:
            continue
        d = store.get_draft_internal(db, did)
        if d is not None and d.status == "needs_review":
            drafted.append({"id": row.id, "lead_name": row.lead.name, "company": row.lead.company,
                            "subject": d.subject, "has_address": _has_address(db, d.lead_id)})
        else:
            refused.append({"lead_id": row.lead.id, "code": "failed", "message": "I couldn't write this one."})
    summary = f"{len(drafted)} {'draft' if len(drafted) == 1 else 'drafts'} ready"
    if pending:
        summary += f", {len(pending)} still writing"
    return ToolResult(_compact({"drafted": drafted, "still_writing": [f"d_{i}" for i in pending],
                                "refused": refused}),
                      ok=bool(ids) or not refused, summary=summary if ids else "No drafts written",
                      invalidate=["outreach", "matches", "status"] if ids else [])


def _revise_draft(db, ctx, args, fr) -> ToolResult:
    res = actions.revise(db, ctx.user_id, args["outreach_id"], args["instruction"])
    db.commit()
    fr.draft(res.outreach, res.previous)
    o = res.outreach
    return ToolResult(_compact({"id": o.id, "version": o.version, "subject": o.subject, "body": o.body}),
                      summary=f"Version {o.version} ready", invalidate=["outreach"])


def _skip(db, ctx, args, fr) -> ToolResult:
    row = actions.skip_lead(db, ctx.user_id, args["lead_id"], reason=args.get("reason"),
                            thread_id=ctx.thread_id, run_id=ctx.run_id)
    db.commit()
    return ToolResult(_compact({"lead_id": row.lead.id, "name": row.lead.name, "outreach_id": row.id,
                                "status_word": "skipped"}),
                      summary=f"Passed on {row.lead.name}", invalidate=["outreach", "matches", "status"])


def _remember(db, ctx, args, fr) -> ToolResult:
    m = actions.save_memory(db, ctx.user_id, args["text"], scope=args["scope"], company=args.get("company"),
                            use_in_emails=bool(args.get("use_in_emails", False)), source="chat")
    db.commit()
    fr.card(MemoryCard(memory=m, action="saved"))
    return ToolResult(_compact({"memory_id": m.id, "text": m.text, "scope": m.scope}), summary="Saved",
                      invalidate=["memories"])


def _forget(db, ctx, args, fr) -> ToolResult:
    m = actions.delete_memory(db, ctx.user_id, args["memory_id"])
    db.commit()
    fr.card(MemoryCard(memory=m, action="removed"))
    return ToolResult(_compact({"removed": True, "text": m.text}), summary="Forgotten", invalidate=["memories"])


def _navigate(db, ctx, args, fr) -> ToolResult:
    to, oid, filt = args["to"], args.get("id"), args.get("filter")
    if to == "outreach":
        if not oid or owned_outreach(db, ctx.user_id, oid) is None:
            return ToolResult("NOT_FOUND: I couldn't find that email.", ok=False, summary="Couldn't open it")
    else:
        oid = None
    if to == "home" and filt and filt not in STATUS_WORDS:
        return ToolResult("TOOL ERROR: invalid arguments: filter", ok=False, summary="Couldn't open it")
    if to not in ("home", "people"):
        filt = None
    fr.navigate(to, oid, filt)
    return ToolResult(_compact({"opened": to}), summary=f"Opened {_TAB_NAMES[to]}")


def _note_summary(notes: dict) -> str:
    parts = []
    for key, value in notes.items():
        label = _NOTE_LABELS[key]
        if value:
            shown = value if len(value) <= 90 else value[:87].rstrip() + "..."
            parts.append(f'Save "{shown}" as {label}.')
        else:
            parts.append(f"Remove {label}.")
    return " ".join(parts) + " Every new email will use it."


def _update_profile_notes(db, ctx, args, fr) -> ToolResult:
    notes = {k: strip_dashes((v or "").strip()) or "" for k, v in args.items() if v is not None}
    if not notes:
        return ToolResult("TOOL ERROR: invalid arguments: notes", ok=False, summary="Nothing to change")
    summary = _note_summary(notes)
    c = store.create_confirmation(db, ctx.user_id, ctx.thread_id, "update_profile_notes", notes, summary,
                                  run_id=ctx.run_id)
    db.commit()
    fr.confirmation(store.confirmation_out(c))
    return ToolResult(_compact({"status": "needs_confirmation", "confirmation_id": c.id, "summary": summary}),
                      summary="Waiting for your Confirm")


# ---------------------------------------------------------------- registry and dispatch

@dataclass
class Spec:
    handler: Callable[..., ToolResult]
    label: Callable[[Any, ToolContext, dict], str]
    needs_candidate: bool = True


def _n_label(db, ctx, args) -> str:
    ids = args.get("lead_ids") or []
    return f"Writing to {_lead_name(db, ctx, ids[0])}" if len(ids) == 1 else f"Writing {len(ids)} emails"


REGISTRY: dict[str, Spec] = {
    "get_profile": Spec(_get_profile, lambda db, ctx, a: "Reading your profile"),
    "get_status": Spec(_get_status, lambda db, ctx, a: "Checking where things stand", needs_candidate=False),
    "list_matches": Spec(_list_matches, lambda db, ctx, a: "Finding people"),
    "get_match": Spec(_get_match, lambda db, ctx, a: f"Looking at {_lead_name(db, ctx, a.get('lead_id'))}"),
    "list_outreach": Spec(_list_outreach, lambda db, ctx, a: "Checking your outreach", needs_candidate=False),
    "get_outreach": Spec(_get_outreach,
                         lambda db, ctx, a: f"Opening the email to {_outreach_name(db, ctx, a.get('outreach_id'))}",
                         needs_candidate=False),
    "draft_emails": Spec(_draft_emails, _n_label),
    "revise_draft": Spec(_revise_draft,
                         lambda db, ctx, a: f"Rewriting the email to {_outreach_name(db, ctx, a.get('outreach_id'))}",
                         needs_candidate=False),
    "skip": Spec(_skip, lambda db, ctx, a: f"Passing on {_lead_name(db, ctx, a.get('lead_id'))}"),
    "remember": Spec(_remember, lambda db, ctx, a: "Saving that", needs_candidate=False),
    "forget": Spec(_forget, lambda db, ctx, a: "Forgetting that", needs_candidate=False),
    "navigate": Spec(_navigate, lambda db, ctx, a: f"Opening {_TAB_NAMES.get(a.get('to'), 'the workspace')}",
                     needs_candidate=False),
    "update_profile_notes": Spec(_update_profile_notes,
                                 lambda db, ctx, a: "Preparing a change to your profile notes"),
}
assert set(REGISTRY) == set(TOOL_SCHEMAS)  # noqa: S101 - import-time guard, the registry is the contract's list


def _call_parts(call: Any) -> tuple[str, str, str]:
    """(call_id, name, raw arguments) from an llm.ToolCall, or an OpenAI-shaped dict."""
    if isinstance(call, dict):
        fn = call.get("function") or {}
        return (str(call.get("id") or ""), str(call.get("name") or fn.get("name") or ""),
                call.get("arguments") if "arguments" in call else fn.get("arguments") or "{}")
    return str(getattr(call, "id", "") or ""), str(getattr(call, "name", "") or ""), getattr(call, "arguments", "{}")


def _parse_args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _invalid_field(name: str, args: dict) -> Optional[str]:
    err = next(iter(sorted(_VALIDATORS[name].iter_errors(args), key=lambda e: list(e.absolute_path))), None)
    if err is None:
        return None
    if err.absolute_path:
        return ".".join(str(p) for p in err.absolute_path)
    if err.validator == "required":
        missing = [k for k in err.validator_value if k not in args]
        return missing[0] if missing else "arguments"
    if err.validator == "additionalProperties":
        extra = [k for k in args if k not in TOOL_SCHEMAS[name].get("properties", {})]
        return extra[0] if extra else "arguments"
    return "arguments"


def _refusal(e: Exception) -> str:
    if isinstance(e, ActionError):
        return ("NOT_FOUND: " if e.code == "not_found" else "REFUSED: ") + e.message
    if isinstance(e, store.NotFound):
        return "NOT_FOUND: that item is not theirs or no longer exists."
    if isinstance(e, store.VersionConflict):
        return "REFUSED: the draft changed just now. Read it again before changing it."
    return "REFUSED: that is not possible for this one right now."


def dispatch(ctx: ToolContext, call: Any, emit: Any) -> ToolResult:
    """Run one model tool call. `emit` is the run's Emitter (or its emit
    function). Never raises for a tool's own failure: the model gets a prefixed
    line and the step row ends with ok false."""
    send = emit.emit if hasattr(emit, "emit") else emit
    call_id, name, raw = _call_parts(call)
    spec = REGISTRY.get(name)
    if spec is None:
        return ToolResult(NO_SUCH_TOOL, ok=False, summary="Not available")
    ctx.tool_calls += 1
    if ctx.tool_calls > int(getattr(settings, "AGENT_MAX_TOOL_CALLS", 16)):
        return ToolResult(OVER_BUDGET, ok=False, summary="Out of steps for this turn")
    args = _parse_args(raw)
    bad = _invalid_field(name, args)
    if bad is not None:
        return ToolResult(f"TOOL ERROR: invalid arguments: {bad}", ok=False, summary="Invalid request")

    db = open_session()
    try:
        try:
            label = spec.label(db, ctx, args)
            db.commit()  # release the connection before tool_start is persisted in its own session
        except Exception:
            db.rollback()
            label = FALLBACK_LABEL
        send({"type": "tool_start", "call_id": call_id, "tool": name, "label": label})
        if spec.needs_candidate and ctx.candidate_id is None:
            result = ToolResult(NO_PROFILE, ok=False, summary="No resume yet")
        else:
            try:
                result = spec.handler(db, ctx, args, _Frames(send, call_id, before_send=db.commit))
                db.commit()
            except (ActionError, store.NotFound, store.WrongState, store.VersionConflict) as e:
                db.rollback()
                result = ToolResult(_refusal(e), ok=False, summary="Not done")
            except Exception as e:
                db.rollback()
                # Type only: a validation error's text quotes its input (draft copy, names).
                logger.warning("[AGENT] tool %s failed in run %s: %s", name, ctx.run_id, type(e).__name__)
                logger.debug("[AGENT] tool traceback", exc_info=True)
                result = ToolResult(TOOL_FAILED, ok=False, summary="That didn't work")
        send({"type": "tool_end", "call_id": call_id, "tool": name, "ok": result.ok,
              "summary": strip_dashes(result.summary) or "", "invalidate": result.invalidate if result.ok else []})
        return result
    finally:
        db.close()

