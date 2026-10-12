"""Wire contract for the student agent, Phase 1.

Every request body, response body and stream event of /api/v1/agent/* is
declared here, and nowhere else. The source of truth is
scratchpad/real_build/contract.md; the frontend mirror is
frontend app/lib/desk/types.ts. Owned by the lead: builders import these
models and never edit them. Ask the coordinator for a change.

Rules encoded here:
- Responses forbid unknown fields, so a builder cannot leak a column (an
  email address, a phone number) by adding it to a dict.
- Requests ignore unknown fields, so an older browser never gets a 422.
- Phase 1 never sends: `sent` and `sending_enabled` are the literal False.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------- vocabulary

StatusWord = Literal["needs_you", "scheduled", "sent", "replied", "failed", "skipped"]
STATUS_WORDS: tuple[str, ...] = ("needs_you", "scheduled", "sent", "replied", "failed", "skipped")
# Phase 1 has no sending, so the "scheduled" chip reads "Approved". Phase 2 flips it.
STATUS_LABELS_PHASE1: dict[str, str] = {
    "needs_you": "Needs you", "scheduled": "Approved", "sent": "Sent",
    "replied": "Replied", "failed": "Failed", "skipped": "Skipped",
}
DraftStatus = Literal["drafting", "needs_review", "approved", "skipped", "failed",
                      "discarded", "superseded", "sent"]
MatchState = Literal["new", "drafted", "contacted", "skipped"]
SignalStrength = Literal["high", "medium", "low"]
DraftStyle = Literal["warm_intro", "value_prop", "company_curiosity", "peer_to_peer", "direct_ask"]
MemoryScope = Literal["everywhere", "emails", "matching", "company"]
Route = Literal["home", "people", "inbox", "profile", "settings", "outreach"]
InvalidateKey = Literal["status", "matches", "outreach", "replies", "profile", "memories",
                        "settings", "threads"]
RunStatus = Literal["running", "done", "error", "cancelled", "limit"]
StreamErrorCode = Literal["internal", "model_unavailable", "content_filter", "interrupted"]
ReplyGroup = Literal["positive", "question", "not_now", "bounced"]
Sentiment = Literal["positive", "negative", "neutral"]
AccountState = Literal["needs_resume", "needs_profile", "not_paid", "connect_gmail",
                       "launch_ready", "launch_draft", "campaign_active"]
ConfirmationStatus = Literal["pending", "approved", "declined", "expired", "failed"]
ErrorCode = Literal["agent_disabled", "agent_not_enabled", "not_found", "run_in_progress",
                    "duplicate_turn", "daily_limit", "draft_limit", "busy", "wrong_state",
                    "version_conflict", "invalid"]

OUTREACH_ID_PATTERN = r"^[de]_[1-9][0-9]*$"          # d_88 = agent_drafts.id, e_4512 = emails_sent.id
THREAD_SCOPE_PATTERN = r"^(main|outreach:[de]_[1-9][0-9]*)$"
OutreachId = Annotated[str, Field(pattern=OUTREACH_ID_PATTERN)]
IsoTime = str                                          # naive UTC ISO 8601, "2026-10-12T11:02:10"

class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")

class _In(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# ---------------------------------------------------------------- errors

class ErrorDetail(_Out):
    code: ErrorCode
    message: str                                        # written for the student
    run_id: Optional[int] = None                        # run_in_progress, duplicate_turn
    limit: Optional[str] = None                         # daily_limit, draft_limit: which cap
    resets_at: Optional[IsoTime] = None                 # daily_limit
    current: Optional[OutreachDetail] = None            # version_conflict: the row as it is now

class ErrorResponse(_Out):
    detail: ErrorDetail


# ---------------------------------------------------------------- matches

class LeadRef(_Out):
    id: int
    name: str
    title: Optional[str] = None
    company: Optional[str] = None
    company_domain: Optional[str] = None

class Why(_Out):
    headline: Optional[str] = None
    bullets: list[str] = []
    signal_strength: Optional[SignalStrength] = None
    source: Literal["ai", "score"]                      # ai = justification_json, score = built from relevance ints

class CompanyFacts(_Out):
    summary: Optional[str] = None
    what_they_build: Optional[str] = None
    core_tech: list[str] = []
    primary_market: Optional[str] = None
    recent_momentum: Optional[str] = None
    hiring_signal: Optional[str] = None

class MatchCard(_Out):
    lead_id: int
    name: str
    title: Optional[str] = None
    company: Optional[str] = None
    company_domain: Optional[str] = None
    city: Optional[str] = None                          # Lead.location
    score: Optional[int] = None                         # round(overall_score) clamped 0..100, None if unscored
    reason: Optional[str] = None                        # justification headline, else None
    signal_strength: Optional[SignalStrength] = None
    strong: bool                                        # not _is_broader()
    has_address: bool                                   # email present AND verified; the address is never sent
    state: MatchState
    outreach_id: Optional[str] = None                   # live draft or Touch 1 email for this lead

class MatchDetail(MatchCard):
    industry: Optional[str] = None
    company_size: Optional[str] = None
    linkedin_url: Optional[str] = None
    why: Why
    company_facts: Optional[CompanyFacts] = None
    outreach: list[OutreachRow] = []

class MatchList(_Out):
    items: list[MatchCard]
    total: int                                          # after filters
    strong_total: int
    offset: int
    limit: int


# ---------------------------------------------------------------- outreach

class Followup(_Out):
    number: int                                         # 1 = Touch 2, 2 = Touch 3
    status: str                                         # raw emails_sent.status
    scheduled_at: Optional[IsoTime] = None
    sent_at: Optional[IsoTime] = None

class OutreachRow(_Out):
    id: OutreachId
    kind: Literal["draft", "email"]
    lead: LeadRef
    subject: str
    preview: str                                        # first 140 chars of body, "" while drafting
    status_word: StatusWord
    server_status: str                                  # agent_drafts.status or emails_sent.status
    note: str                                           # one line for the student, see contract section 1
    busy: bool = False                                  # True while status == drafting
    when: Optional[IsoTime] = None
    version: Optional[int] = None                       # drafts only
    followups: list[Followup] = []                      # emails only

class Counts(_Out):
    all: int
    needs_you: int
    scheduled: int
    sent: int
    replied: int
    failed: int
    skipped: int

class OutreachList(_Out):
    items: list[OutreachRow]
    counts: Counts                                      # unfiltered, for the chips
    total: int                                          # filtered
    offset: int
    limit: int

class Fact(_Out):
    text: str
    source: Literal["profile", "notes", "memory", "company"]

class Flag(_Out):
    text: str                                           # the line or token to check
    reason: str                                         # student facing

class DraftVersion(_Out):
    version: int
    subject: str
    body: str
    edited_by: Literal["agent", "student"]
    created_at: IsoTime

class SentTouch(_Out):
    number: int                                         # 0 = Touch 1
    status: str
    subject: Optional[str] = None
    body: Optional[str] = None
    sent_at: Optional[IsoTime] = None

class EmailThread(_Out):
    touches: list[SentTouch]
    reply_text: Optional[str] = None
    reply_sentiment: Optional[Sentiment] = None
    reply_received_at: Optional[IsoTime] = None
    failure_note: Optional[str] = None                  # customer_failure_reason, never error_message

class PreviousCopy(_Out):
    subject: str
    body: str
    version: int

class OutreachDetail(OutreachRow):
    body: str
    style: Optional[str] = None
    instructions: Optional[str] = None
    facts_used: list[Fact] = []
    flags: list[Flag] = []
    versions: list[DraftVersion] = []                   # older versions, newest first
    approved_at: Optional[IsoTime] = None
    can_edit: bool
    can_approve: bool
    can_skip: bool
    can_discard: bool
    sending_enabled: Literal[False] = False
    send_blocker: Optional[Literal["sending_off"]] = None
    thread: Optional[EmailThread] = None                # emails only

class Refusal(_Out):
    lead_id: Optional[int] = None
    outreach_id: Optional[str] = None
    code: str                                           # not_found | wrong_state | draft_limit | never_contact | skipped
    message: str

class DraftsCreated(_Out):
    items: list[OutreachRow]                            # rows in status drafting, filled in the background
    refused: list[Refusal] = []

class ReviseResult(_Out):
    outreach: OutreachDetail
    previous: PreviousCopy

class ApproveResult(_Out):
    outreach: OutreachDetail
    sent: Literal[False] = False
    message: str

class ApproveAllResult(_Out):
    approved: list[OutreachRow]
    refused: list[Refusal] = []
    sent: Literal[False] = False
    message: str

class ReplyCounts(_Out):
    positive: int
    question: int
    not_now: int
    bounced: int

class ReplyItem(_Out):
    outreach_id: OutreachId                             # always e_<id>
    lead: LeadRef
    subject: Optional[str] = None
    reply_text: Optional[str] = None                    # None for bounced
    sentiment: Optional[Sentiment] = None
    group: ReplyGroup
    received_at: Optional[IsoTime] = None
    sent_at: Optional[IsoTime] = None

class ReplyList(_Out):
    items: list[ReplyItem]
    counts: ReplyCounts
    total: int
    offset: int
    limit: int


# ---------------------------------------------------------------- profile, memory, settings, status

class ProfileNotes(_Out):
    best_project: Optional[str] = None
    outcome: Optional[str] = None
    why_now: Optional[str] = None
    credential: Optional[str] = None

class Profile(_Out):
    candidate_id: int
    name: str
    education: Optional[str] = None
    summary: Optional[str] = None
    top_skills: list[str] = []
    target_roles: list[str] = []
    target_industries: list[str] = []
    dream_companies: list[str] = []
    locations: list[str] = []
    career_stage: Optional[str] = None
    hiring_manager_titles: list[str] = []
    notes: ProfileNotes

class Memory(_Out):
    id: int
    text: str
    scope: MemoryScope
    company: Optional[str] = None
    use_in_emails: bool
    source: Literal["chat", "settings", "profile"]
    created_at: IsoTime

class MemoryList(_Out):
    items: list[Memory]
    cap: int

class NeverContact(_Out):
    kind: Literal["company", "person"]
    value: str = Field(min_length=1, max_length=120)

class AgentSettings(_Out):
    review_before_send: bool
    followups: bool
    never_contact: list[NeverContact]

class Credits(_Out):
    available: int
    reserved: int

class GmailState(_Out):
    connected: bool
    address: Optional[str] = None                       # the student's own mailbox; UI only, never given to the model
    token_valid: Optional[bool] = None

class MatchTotals(_Out):
    total: int
    strong: int
    new: int                                            # state == new and not on the never-contact list

class Limits(_Out):
    messages_today: int
    messages_cap: int
    drafts_today: int
    drafts_cap: int
    drafts_waiting: int                                 # drafting + needs_review
    drafts_waiting_cap: int
    max_message_chars: int

class Status(_Out):
    state: AccountState
    candidate_id: Optional[int] = None
    first_name: str
    credits: Credits
    gmail: GmailState
    counts: Counts
    matches: MatchTotals
    sending_enabled: Literal[False] = False
    limits: Limits


# ---------------------------------------------------------------- requests

class ContextItem(_In):
    kind: Literal["lead", "outreach"]
    id: Union[int, str]

class PageContext(_In):
    route: Route
    outreach_id: Optional[OutreachId] = None
    lead_id: Optional[int] = None
    filter: Optional[str] = Field(default=None, max_length=40)
    query: Optional[str] = Field(default=None, max_length=120)
    selected: list[ContextItem] = Field(default_factory=list, max_length=5)

class ConfirmationDecision(_In):
    id: str = Field(min_length=3, max_length=40)
    decision: Literal["approve", "decline"]

class SendMessage(_In):
    content: str = Field(default="", max_length=4000)
    client_turn_id: str = Field(min_length=8, max_length=64)
    page_context: Optional[PageContext] = None
    confirmation: Optional[ConfirmationDecision] = None
    time_zone: Optional[str] = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _content_or_confirmation(self) -> SendMessage:
        if not self.content and self.confirmation is None:
            raise ValueError("content is required unless a confirmation is sent")
        return self

class CreateThread(_In):
    title: Optional[str] = Field(default=None, max_length=80)
    scope: str = Field(default="main", pattern=THREAD_SCOPE_PATTERN)

class CreateDrafts(_In):
    lead_ids: list[int] = Field(min_length=1, max_length=5)
    style: Optional[DraftStyle] = None
    instructions: Optional[str] = Field(default=None, max_length=400)

class ReviseDraft(_In):
    instruction: str = Field(min_length=1, max_length=400)
    expected_version: int = Field(ge=1)

class EditDraft(_In):
    subject: Optional[str] = Field(default=None, min_length=1, max_length=200)
    body: Optional[str] = Field(default=None, min_length=1, max_length=5000)
    expected_version: int = Field(ge=1)

class ApproveDraft(_In):
    expected_version: int = Field(ge=1)

class ApproveAll(_In):
    ids: list[OutreachId] = Field(min_length=1, max_length=50)

class DiscardDraft(_In):
    reason: Optional[str] = Field(default=None, max_length=200)

class SkipMatch(_In):
    reason: Optional[str] = Field(default=None, max_length=200)

class UpdateNotes(_In):
    best_project: Optional[str] = Field(default=None, max_length=500)
    outcome: Optional[str] = Field(default=None, max_length=500)
    why_now: Optional[str] = Field(default=None, max_length=500)
    credential: Optional[str] = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateNotes:
        if all(v is None for v in (self.best_project, self.outcome, self.why_now, self.credential)):
            raise ValueError("send at least one note")
        return self

class CreateMemory(_In):
    text: str = Field(min_length=1, max_length=300)
    scope: MemoryScope = "everywhere"
    company: Optional[str] = Field(default=None, max_length=200)
    use_in_emails: bool = False

class UpdateSettings(_In):
    review_before_send: Optional[bool] = None
    followups: Optional[bool] = None
    never_contact: Optional[list[NeverContact]] = Field(default=None, max_length=100)


for _m in (ErrorDetail, MatchDetail, OutreachDetail):
    _m.model_rebuild()
