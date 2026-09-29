"""Self-serve account deletion (B2C open item NEW-04).

The user row is kept as a tombstone rather than deleted: almost every table
cascades from it, including payment_orders, which we must keep for the
statutory period. Everything personal is deleted, the Gmail grant is revoked
with Google first, and the tombstone can never sign in again.

Every public table with a user_id column must be listed in KEEP or DELETE.
delete_account refuses to run if the live schema has one that is not, so a
new table can never silently keep a deleted user's data.
"""
import logging
from datetime import datetime

import requests
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Money and audit trail, kept against the tombstone. No personal data beyond ids.
KEEP = frozenset({
    "payment_orders", "credit_ledger", "user_credits",
    "coupons",            # ambassador-owned codes: the code outlives the person
    "coupon_issuance",    # kept, email replaced with the tombstone address below
    "bob_credits", "bob_credit_ledger",
})

# Deleted by user_id, in this order. Children before parents where a foreign
# key has no cascade: outreach_orders -> linkedin_campaigns (NO ACTION),
# internship_applications -> resumes (RESTRICT). candidates cascades to leads,
# lead_scores, campaigns and emails_sent (bodies and reply_text included).
DELETE = (
    "scheduled_emails", "email_send_log", "email_opens", "email_preferences",
    "launch_nudges", "campaign_notices", "system_events", "tickets", "user_attribution", "tool_used",
    "consultation_signups", "extension_drafts", "job_queue", "api_keys", "enrichment_jobs",
    "outreach_orders", "outreach_campaigns", "outreach_contacts",
    "linkedin_connection_requests", "linkedin_outreach_leads", "linkedin_search_jobs",
    "linkedin_campaigns", "linkedin_tokens", "user_linkedin_sessions",
    "candidates", "email_accounts",
    "internship_applications", "application_resume_uploads",
    "resume_drafts", "resumes", "rsb_sessions",
    "career_ops_applications", "career_ops_reports", "career_ops_profiles",
    "autoapply_jobs", "autoapply_configs", "mesa_searches",
    "user_question_responses", "user_profile",
    "session", "account", "passkey", "two_factor", "password_reset_tokens",
)

# Rows tied to the person only by their email address. leads.email and
# emails_sent.to_email are deliberately absent: those are the hiring managers
# the student wrote to, not the student.
DELETE_BY_EMAIL = (
    ("email_clicks", "email"), ("email_replies", "email"),
    ("webinar_registrations", "email"), ("webinar_link_sent", "email"),
    ("webinar_standing_subscribers", "email"),
    ("campus_ambassador_applications", "email"),
    ("campus_ambassador_applications_archive", "email"),
    ("career_applications", "email"), ("dissertation_submissions", "email"),
)


class UnclassifiedUserTables(RuntimeError):
    """The schema has user_id tables this module does not know how to treat."""


def tombstone_email(user_id: str) -> str:
    # .invalid is reserved (RFC 2606): nothing can ever be delivered to it,
    # and the real address is free to sign up again.
    return f"deleted-{user_id}@deleted.invalid"


def unclassified_user_tables(db: Session) -> list[str]:
    insp = inspect(db.get_bind())
    known = KEEP | set(DELETE) | {"user"}
    return sorted(
        t for t in insp.get_table_names()
        if t not in known and any(c["name"] == "user_id" for c in insp.get_columns(t))
    )


def revoke_google_grant(refresh_token: str) -> bool:
    """Best effort: a revoked or expired token also answers 400."""
    try:
        resp = requests.post(GOOGLE_REVOKE_URL, data={"token": refresh_token}, timeout=(5, 15))
        return resp.status_code == 200
    except requests.RequestException as e:
        logger.warning("[ACCOUNT_DELETE] Google revoke failed: %s", e)
        return False


def delete_account(db: Session, user_id: str) -> dict:
    """Delete everything personal for user_id and tombstone the user row.

    One transaction: either all of it happens or none of it does. Revoking the
    Gmail grant happens first and does not depend on the transaction.
    """
    missing = unclassified_user_tables(db)
    if missing:
        raise UnclassifiedUserTables(", ".join(missing))

    row = db.execute(text('SELECT email FROM "user" WHERE id = :u'), {"u": user_id}).first()
    if row is None:
        raise LookupError(user_id)
    email = row[0]

    tables = set(inspect(db.get_bind()).get_table_names())
    report = {"revoked_gmail": 0, "deleted": {}}

    if "email_accounts" in tables:
        tokens = db.execute(
            text("SELECT refresh_token FROM email_accounts WHERE user_id = :u AND refresh_token IS NOT NULL"),
            {"u": user_id},
        ).scalars().all()
        report["revoked_gmail"] = sum(revoke_google_grant(t) for t in tokens)

    try:
        for t in DELETE:
            if t in tables:
                n = db.execute(text(f'DELETE FROM "{t}" WHERE user_id = :u'), {"u": user_id}).rowcount  # noqa: S608 - t is from DELETE
                if n:
                    report["deleted"][t] = n
        for t, col in DELETE_BY_EMAIL:
            if t in tables:
                n = db.execute(text(f'DELETE FROM "{t}" WHERE lower({col}) = lower(:e)'), {"e": email}).rowcount  # noqa: S608 - from DELETE_BY_EMAIL
                if n:
                    report["deleted"][t] = n
        if "coupon_issuance" in tables:
            db.execute(text("UPDATE coupon_issuance SET email = :e WHERE user_id = :u"),
                       {"e": tombstone_email(user_id), "u": user_id})

        db.execute(text(
            'UPDATE "user" SET email = :e, name = :n, image = NULL, phone_number = NULL, '
            "email_verified = false, banned = true, ban_reason = 'account deleted', "
            "updated_at = :now WHERE id = :u"
        ), {"e": tombstone_email(user_id), "n": "Deleted user", "now": datetime.utcnow(), "u": user_id})
        db.commit()
    except Exception:
        db.rollback()
        raise

    logger.info("[ACCOUNT_DELETE] user %s deleted: %s", user_id, report)
    return report
