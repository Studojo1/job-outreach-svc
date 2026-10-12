"""Wire contract for the student agent, Phase 1: chat, cards, stream events, bootstrap.

Second half of services/agent/schemas.py (read its docstring for the rules).
Owned by the lead: builders import these models and never edit them.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import Field

from services.agent.schemas import (
    AgentSettings,
    ConfirmationStatus,
    Counts,
    InvalidateKey,
    IsoTime,
    MatchCard,
    Memory,
    OutreachDetail,
    OutreachList,
    OutreachRow,
    PreviousCopy,
    Profile,
    Route,
    RunStatus,
    Status,
    StreamErrorCode,
    Why,
    _Out,
)

# ---------------------------------------------------------------- chat

class Suggestion(_Out):
    label: str                                          # what the chip shows
    prompt: str                                         # what is sent when tapped

class Thread(_Out):
    id: int
    title: str
    scope: str                                          # main | outreach:d_88
    updated_at: IsoTime

class ThreadList(_Out):
    items: list[Thread]

class Step(_Out):
    call_id: str
    tool: str
    label: str
    ok: Optional[bool] = None                           # None while running
    summary: Optional[str] = None

class Confirmation(_Out):
    id: str                                             # c_<12 hex>
    tool: str
    summary: str
    status: ConfirmationStatus
    expires_at: IsoTime

class NavigateTarget(_Out):
    to: Route
    id: Optional[str] = None                            # outreach id when to == outreach
    filter: Optional[str] = None                        # a status word on home; search text on people

class MatchesCard(_Out):
    kind: Literal["matches"] = "matches"
    items: list[MatchCard]
    total: int

class MatchWhyCard(_Out):
    kind: Literal["match"] = "match"
    match: MatchCard
    why: Why

class OutreachCard(_Out):
    kind: Literal["outreach"] = "outreach"
    items: list[OutreachRow]
    counts: Counts

class StatusCard(_Out):
    kind: Literal["status"] = "status"
    status: Status

class ProfileCard(_Out):
    kind: Literal["profile"] = "profile"
    profile: Profile

class MemoryCard(_Out):
    kind: Literal["memory"] = "memory"
    memory: Memory
    action: Literal["saved", "removed"]

Card = Annotated[Union[MatchesCard, MatchWhyCard, OutreachCard, StatusCard, ProfileCard, MemoryCard],
                 Field(discriminator="kind")]

class Message(_Out):
    id: int
    role: Literal["user", "assistant"]
    content: str
    created_at: IsoTime
    run_id: Optional[int] = None
    status: Optional[RunStatus] = None                  # assistant only: done | cancelled | limit
    steps: list[Step] = []
    cards: list[Card] = []
    outreach_ids: list[str] = []                        # drafts written or revised in this turn
    confirmations: list[Confirmation] = []              # current state, not the state at the time
    navigate: Optional[NavigateTarget] = None
    suggestions: list[Suggestion] = []

class Run(_Out):
    id: int
    thread_id: int
    status: RunStatus
    started_at: IsoTime
    finished_at: Optional[IsoTime] = None
    error_code: Optional[StreamErrorCode] = None

class ThreadDetail(_Out):
    thread: Thread
    messages: list[Message]                             # oldest first, last 50 unless ?before=
    latest_run: Optional[Run] = None
    has_more: bool


# ---------------------------------------------------------------- stream events

class Usage(_Out):
    messages_today: int
    messages_cap: int

class RunStartEvent(_Out):
    type: Literal["run_start"] = "run_start"
    seq: int
    run_id: int
    thread_id: int
    user_message_id: int

class TextEvent(_Out):                                  # the only event that is not persisted
    type: Literal["text"] = "text"
    seq: int
    delta: str

class ToolStartEvent(_Out):
    type: Literal["tool_start"] = "tool_start"
    seq: int
    call_id: str
    tool: str
    label: str

class ToolEndEvent(_Out):
    type: Literal["tool_end"] = "tool_end"
    seq: int
    call_id: str
    tool: str
    ok: bool
    summary: str
    invalidate: list[InvalidateKey] = []

class CardEvent(_Out):
    type: Literal["card"] = "card"
    seq: int
    call_id: str
    card: Card

class DraftEvent(_Out):
    type: Literal["draft"] = "draft"
    seq: int
    call_id: str
    outreach: OutreachDetail
    previous: Optional[PreviousCopy] = None             # set on a revision, for the diff view

class ConfirmationEvent(_Out):                          # a new ticket, or a status change of one
    type: Literal["confirmation"] = "confirmation"
    seq: int
    confirmation: Confirmation

class NavigateEvent(_Out):
    type: Literal["navigate"] = "navigate"
    seq: int
    to: Route
    id: Optional[str] = None
    filter: Optional[str] = None

class SuggestionsEvent(_Out):
    type: Literal["suggestions"] = "suggestions"
    seq: int
    items: list[Suggestion]

class MessageEndEvent(_Out):
    type: Literal["message_end"] = "message_end"
    seq: int
    message_id: int
    content: str                                        # canonical full text; replaces the streamed text

class DoneEvent(_Out):
    type: Literal["done"] = "done"
    seq: int
    run_id: int
    status: Literal["done", "cancelled", "limit"]
    invalidate: list[InvalidateKey] = []                # union of every invalidate in the run
    usage: Usage

class ErrorEvent(_Out):
    type: Literal["error"] = "error"
    seq: int
    code: StreamErrorCode
    message: str
    retryable: bool

StreamEvent = Annotated[Union[RunStartEvent, TextEvent, ToolStartEvent, ToolEndEvent, CardEvent,
                              DraftEvent, ConfirmationEvent, NavigateEvent, SuggestionsEvent,
                              MessageEndEvent, DoneEvent, ErrorEvent],
                        Field(discriminator="type")]
PERSISTED_EVENT_TYPES: frozenset[str] = frozenset({
    "run_start", "tool_start", "tool_end", "card", "draft", "confirmation", "navigate",
    "suggestions", "message_end", "done", "error",
})

class RunPoll(_Out):
    run: Run
    events: list[StreamEvent]                           # persisted events with seq > after_seq
    message: Optional[Message] = None                   # final assistant message once terminal

class CancelResult(_Out):
    ok: Literal[True] = True
    status: RunStatus

class OkResult(_Out):
    ok: Literal[True] = True


# ---------------------------------------------------------------- bootstrap

class BootUser(_Out):
    id: str
    first_name: str

class Bootstrap(_Out):
    enabled: Literal[True] = True
    user: BootUser
    status: Status
    settings: AgentSettings
    status_labels: dict[str, str]                       # STATUS_LABELS_PHASE1
    top_matches: list[MatchCard]                        # up to 5, state new, best first
    outreach: OutreachList                              # first page, all statuses
    threads: list[Thread]
    thread: ThreadDetail                                # newest main thread, created if none
    suggestions: list[Suggestion]
    greeting: Optional[str] = None                      # built in code from real counts, no model call

class BootstrapDisabled(_Out):
    enabled: Literal[False] = False
    reason: Literal["agent_disabled", "agent_not_enabled"]


for _m in (StatusCard, Message, ThreadDetail, RunPoll, Bootstrap, DraftEvent, CardEvent, OutreachCard):
    _m.model_rebuild()
