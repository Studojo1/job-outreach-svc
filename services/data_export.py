"""Download my data (Privacy Policy v2.0): everything job-outreach-svc holds
about a user, as JSON.

Columns are allowlisted per table, not denylisted, so a column added later
(a token, a signature, an internal id) stays out until someone decides it
belongs in the export. Never exported: OAuth tokens, LinkedIn cookies and
nonces, payment signatures and idempotency keys, tracking tokens, admin actor
ids.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from database.models import (
    AgentDraft, AgentMemory, AgentMessage, AgentSetting, AgentThread, Campaign, Candidate, CreditLedger,
    EmailAccount, EmailSent, Lead, LinkedInCampaign, LinkedInConnectionRequest, OutreachOrder, PaymentOrder,
    User, UserCredit,
)
from services.connections import connection_status

USER_COLS = ("id", "email", "name", "created_at")
CANDIDATE_COLS = ("id", "resume_text", "parsed_json", "resume_profile", "dream_companies", "target_roles",
                  "target_industries", "flex_notes", "quiz_answers", "quiz_answers_updated_at", "created_at")
LEAD_COLS = ("id", "candidate_id", "name", "title", "company", "industry", "location", "linkedin_url", "email",
             "company_size", "status", "created_at")
MAILBOX_COLS = ("id", "email_address", "provider", "daily_send_limit", "created_at")
CAMPAIGN_COLS = ("id", "candidate_id", "email_account_id", "name", "status", "subject_template", "body_template",
                 "daily_limit", "user_timezone", "selected_styles", "generation_mode", "started_at", "paused_at",
                 "pause_reason", "completed_at", "expires_at", "outcome", "credits_reserved", "credits_released",
                 "created_at")
EMAIL_COLS = ("id", "campaign_id", "to_email", "subject", "body", "status", "error_message", "followup_number",
              "is_test", "scheduled_at", "sent_at", "reply_text", "reply_received_at", "reply_sentiment",
              "bounce_reason", "first_opened_at", "last_opened_at", "open_count", "created_at")
ORDER_COLS = ("id", "candidate_id", "campaign_id", "status", "plan_type", "leads_collected", "leads_target",
              "credits_reserved", "credits_used", "credits_refunded", "resume_uploaded_at", "quiz_completed_at",
              "payment_made_at", "gmail_connected_at", "campaign_launched_at", "campaign_completed_at",
              "linkedin_connected_at", "created_at", "updated_at")
PAYMENT_COLS = ("id", "provider", "amount_cents", "currency", "tier", "plan_id", "status", "credits_granted",
                "refunded_cents", "refunded_at", "created_at")
LEDGER_COLS = ("id", "delta_total", "delta_used", "reason", "campaign_id", "payment_order_id", "note", "created_at")
LI_CAMPAIGN_COLS = ("id", "candidate_id", "name", "status", "target_role", "target_industries", "target_locations",
                    "target_company_sizes", "target_keywords", "connection_note", "followup_message", "daily_limit",
                    "send_with_note", "total_leads", "total_sent", "total_accepted", "total_followups_sent",
                    "total_replied", "launched_at", "created_at", "updated_at")
LI_REQUEST_COLS = ("id", "campaign_id", "name", "headline", "company", "location", "profile_url", "connection_note",
                   "followup_message", "match_reason", "status", "sent_at", "accepted_at", "followup_sent_at",
                   "reply_text", "reply_received_at", "reply_sentiment", "created_at")

# Student agent (migration 076). Chat rows are the student's own words and the
# replies they saw; tool rows (internal lookups) and run telemetry stay out.
AGENT_THREAD_COLS = ("id", "scope", "title", "status", "created_at", "updated_at")
AGENT_MESSAGE_COLS = ("id", "thread_id", "role", "content", "created_at")
AGENT_DRAFT_COLS = ("id", "lead_id", "kind", "status", "style", "instructions", "subject", "body",
                    "original_subject", "original_body", "version", "versions", "edited_by", "approved_at",
                    "approved_version", "skipped_at", "skip_reason", "discarded_at", "discard_reason",
                    "created_at", "updated_at")
AGENT_MEMORY_COLS = ("id", "text", "scope", "company", "use_in_emails", "source", "created_at")
AGENT_SETTINGS_COLS = ("review_before_send", "followups", "never_contact", "updated_at")


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _rows(objs, cols) -> list[dict]:
    return [{c: _jsonable(getattr(o, c)) for c in cols} for o in objs]


def export_user_data(db: Session, user_id: str) -> dict:
    user = db.query(User).filter(User.id == user_id).first()
    candidates = db.query(Candidate).filter(Candidate.user_id == user_id).order_by(Candidate.id).all()
    cand_ids = [c.id for c in candidates] or [-1]
    campaigns = db.query(Campaign).filter(Campaign.candidate_id.in_(cand_ids)).order_by(Campaign.id).all()
    camp_ids = [c.id for c in campaigns] or [-1]
    li_campaigns = db.query(LinkedInCampaign).filter(LinkedInCampaign.user_id == user_id).order_by(LinkedInCampaign.id).all()
    credit = db.query(UserCredit).filter(UserCredit.user_id == user_id).first()
    connections = connection_status(db, user_id)

    return {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "service": "job-outreach",
        "profile": _rows([user], USER_COLS)[0] if user else None,
        "candidates": _rows(candidates, CANDIDATE_COLS),
        "leads": _rows(db.query(Lead).filter(Lead.candidate_id.in_(cand_ids)).order_by(Lead.id), LEAD_COLS),
        "connections": connections,
        "gmail_accounts": _rows(
            db.query(EmailAccount).filter(EmailAccount.user_id == user_id).order_by(EmailAccount.id), MAILBOX_COLS),
        "outreach_orders": _rows(
            db.query(OutreachOrder).filter(OutreachOrder.user_id == user_id).order_by(OutreachOrder.id), ORDER_COLS),
        "campaigns": _rows(campaigns, CAMPAIGN_COLS),
        "emails_sent": _rows(
            db.query(EmailSent).filter(EmailSent.campaign_id.in_(camp_ids)).order_by(EmailSent.id), EMAIL_COLS),
        "payments": _rows(
            db.query(PaymentOrder).filter(PaymentOrder.user_id == user_id).order_by(PaymentOrder.id), PAYMENT_COLS),
        "credits": {
            "total_credits": credit.total_credits if credit else 0,
            "used_credits": credit.used_credits if credit else 0,
            "ledger": _rows(
                db.query(CreditLedger).filter(CreditLedger.user_id == user_id).order_by(CreditLedger.id), LEDGER_COLS),
        },
        "linkedin_campaigns": _rows(li_campaigns, LI_CAMPAIGN_COLS),
        "linkedin_connection_requests": _rows(
            db.query(LinkedInConnectionRequest).filter(LinkedInConnectionRequest.user_id == user_id)
            .order_by(LinkedInConnectionRequest.id), LI_REQUEST_COLS),
        "agent": _agent_rows(db, user_id),
    }


def _agent_rows(db: Session, user_id: str) -> dict | None:
    # Migration 076 lands the eight agent tables together; a database without
    # them (a test schema, a service ahead of its migration) has no agent data.
    if not inspect(db.get_bind()).has_table("agent_threads"):
        return None
    settings = db.query(AgentSetting).filter(AgentSetting.user_id == user_id).first()
    return {
        "threads": _rows(
            db.query(AgentThread).filter(AgentThread.user_id == user_id).order_by(AgentThread.id), AGENT_THREAD_COLS),
        "messages": _rows(
            db.query(AgentMessage).filter(AgentMessage.user_id == user_id, AgentMessage.role.in_(("user", "assistant")),
                                          AgentMessage.tool_calls.is_(None)).order_by(AgentMessage.id),
            AGENT_MESSAGE_COLS),
        "drafts": _rows(
            db.query(AgentDraft).filter(AgentDraft.user_id == user_id).order_by(AgentDraft.id), AGENT_DRAFT_COLS),
        "memories": _rows(
            db.query(AgentMemory).filter(AgentMemory.user_id == user_id).order_by(AgentMemory.id), AGENT_MEMORY_COLS),
        "settings": _rows([settings], AGENT_SETTINGS_COLS)[0] if settings else None,
    }
