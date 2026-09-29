"""Self-serve account deletion (B2C open item NEW-04).

The user row is kept as a tombstone rather than deleted: almost every table
cascades from it, including payment_orders, which we must keep for the
statutory period. Everything personal is deleted, the Gmail grant is revoked
with Google first, and the tombstone can never sign in again.

Every public table with a user_id column must be listed in KEEP or DELETE,
and every control-plane (cp schema) one in CP_KEEP or CP_DELETE.
delete_account refuses to run if the live schema has one that is not, so a
new table can never silently keep a deleted user's data.

Privacy Policy v2.0 §15 also covers, and this does:
  - records of what the account sent are kept 3 years with no content and
    no readable address: copied to deleted_account_sends (recipient hash,
    campaign, time, status, touch, payment) before emails_sent and
    linkedin_connection_requests go;
  - control-plane jobs (cp.jobs and their idempotency keys and transitions);
    cp.payments are payment records and are kept against the tombstone;
  - support chatbot history. support_chat_logs has no user id (the widget
    sends a random per-tab session id), so it is matched on the IP address
    and user agent of the user's own sessions, or their address in a message;
  - resume and other files in Azure Blob Storage, and the analytics
    providers (services/deletion_external.py), after the commit.
"""
import hashlib
import json
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
    "marketing_opt_outs",  # emailer-service: the account's own tips-and-offers opt-out
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


# Control-plane (Go service, schema cp). payments is the money record, kept.
# jobs carry the user's generated work in payload/result; idempotency_keys
# point at them. job_state_transitions has no user_id and goes with its job.
CP_SCHEMA = "cp"
CP_KEEP = frozenset({"payments"})
CP_DELETE = ("idempotency_keys", "jobs")

# Tables whose rows may hold blob URLs of the user's files. Read before the
# rows are deleted so the files can be deleted after the commit.
BLOB_REF_TABLES = (
    "application_resume_uploads", "internship_applications", "resumes", "resume_drafts",
    "tickets", "career_ops_applications", "career_ops_profiles", "rsb_sessions",
)


class UnclassifiedUserTables(RuntimeError):
    """The schema has user_id tables this module does not know how to treat."""


def tombstone_email(user_id: str) -> str:
    # .invalid is reserved (RFC 2606): nothing can ever be delivered to it,
    # and the real address is free to sign up again.
    return f"deleted-{user_id}@deleted.invalid"


def _has_cp(insp) -> bool:
    try:
        return CP_SCHEMA in insp.get_schema_names()
    except Exception:  # noqa: BLE001
        return False


def unclassified_user_tables(db: Session) -> list[str]:
    insp = inspect(db.get_bind())
    known = KEEP | set(DELETE) | {"user"}
    missing = [
        t for t in insp.get_table_names()
        if t not in known and any(c["name"] == "user_id" for c in insp.get_columns(t))
    ]
    if _has_cp(insp):
        cp_known = CP_KEEP | set(CP_DELETE)
        missing += [
            f"{CP_SCHEMA}.{t}" for t in insp.get_table_names(schema=CP_SCHEMA)
            if t not in cp_known
            and any(c["name"] == "user_id" for c in insp.get_columns(t, schema=CP_SCHEMA))
        ]
    return sorted(missing)


def recipient_hash(value: str | None) -> str:
    """sha256 of the lowercased, trimmed address (same as suppression)."""
    return hashlib.sha256((value or "").strip().lower().encode("utf-8")).hexdigest()


def _columns(insp, table: str, schema: str | None = None) -> set[str]:
    try:
        return {c["name"] for c in insp.get_columns(table, schema=schema)}
    except Exception:  # noqa: BLE001
        return set()


def _tombstone_sends(db: Session, user_id: str, tables: set[str]) -> int:
    """Copy what the account sent into deleted_account_sends: no content, no
    readable address (§15, kept 3 years). Must run before candidates is
    deleted, which cascades to emails_sent."""
    if "deleted_account_sends" not in tables:
        logger.error("[ACCOUNT_DELETE] deleted_account_sends missing (migration 055 not applied): "
                     "send records of %s are not kept", user_id)
        return -1
    now = datetime.utcnow()
    rows = []
    if {"emails_sent", "candidates", "campaigns", "leads"} <= tables:
        pay = {}
        if "payment_orders" in tables and "outreach_order_id" in _columns(inspect(db.connection()), "payment_orders"):
            pay = dict(db.execute(text(
                "SELECT outreach_order_id, min(id) FROM payment_orders "
                "WHERE user_id = :u AND outreach_order_id IS NOT NULL GROUP BY outreach_order_id"
            ), {"u": user_id}).all())
        sent = db.execute(text(
            "SELECT e.to_email, e.campaign_id, e.sent_at, e.status, e.followup_number, c.outreach_order_id "
            "FROM emails_sent e LEFT JOIN campaigns c ON c.id = e.campaign_id "
            "WHERE e.to_email IS NOT NULL "
            "AND (e.sent_at IS NOT NULL OR e.status IN ('sent', 'replied', 'bounced')) "
            "AND (e.campaign_id IN (SELECT c2.id FROM campaigns c2 JOIN candidates k ON k.id = c2.candidate_id "
            "                       WHERE k.user_id = :u) "
            "  OR e.lead_id IN (SELECT l.id FROM leads l JOIN candidates k ON k.id = l.candidate_id "
            "                   WHERE k.user_id = :u))"
        ), {"u": user_id}).all()
        rows += [{
            "ch": "email", "h": recipient_hash(r.to_email), "c": r.campaign_id, "s": r.sent_at,
            "st": r.status, "f": r.followup_number, "p": pay.get(r.outreach_order_id), "d": now,
        } for r in sent]
    if "linkedin_connection_requests" in tables:
        li = db.execute(text(
            "SELECT profile_url, profile_urn, name, campaign_id, sent_at, status, followup_sent_at "
            "FROM linkedin_connection_requests WHERE user_id = :u AND sent_at IS NOT NULL"
        ), {"u": user_id}).all()
        rows += [{
            "ch": "linkedin", "h": recipient_hash(r.profile_url or r.profile_urn or r.name),
            "c": r.campaign_id, "s": r.sent_at, "st": r.status,
            "f": 1 if r.followup_sent_at else 0, "p": None, "d": now,
        } for r in li]
    if rows:
        db.execute(text(
            "INSERT INTO deleted_account_sends "
            "(channel, recipient_hash, campaign_id, sent_at, status, followup_number, payment_order_id, deleted_at) "
            "VALUES (:ch, :h, :c, :s, :st, :f, :p, :d)"
        ), rows)
    return len(rows)


def _collect_blob_refs(db: Session, user_id: str, tables: set[str], cp_tables: set[str]) -> set:
    from services.deletion_external import blob_refs

    values = []
    for t in BLOB_REF_TABLES:
        if t in tables:
            for row in db.execute(text(f'SELECT * FROM "{t}" WHERE user_id = :u'), {"u": user_id}).all():  # noqa: S608
                values.extend(row)
    if "jobs" in cp_tables:
        for row in db.execute(text("SELECT payload, result FROM cp.jobs WHERE user_id = :u"),
                              {"u": user_id}).all():
            values.extend(json.dumps(v) if isinstance(v, (dict, list)) else v for v in row)
    return blob_refs(values)


def _delete_chat_logs(db: Session, user_id: str, email: str, tables: set[str]) -> int:
    if "support_chat_logs" not in tables:
        return 0
    insp = inspect(db.connection())
    n = 0
    if "session" in tables and {"ip_address", "user_agent"} <= _columns(insp, "session"):
        seen = db.execute(text(
            "SELECT DISTINCT ip_address, user_agent FROM session WHERE user_id = :u AND ip_address IS NOT NULL"
        ), {"u": user_id}).all()
        for ip, ua in seen:
            if ua:
                n += db.execute(text("DELETE FROM support_chat_logs WHERE ip_address = :ip AND user_agent = :ua"),
                                {"ip": ip, "ua": ua}).rowcount
            else:
                n += db.execute(text("DELETE FROM support_chat_logs WHERE ip_address = :ip AND user_agent IS NULL"),
                                {"ip": ip}).rowcount
    if email:
        pat = email.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        n += db.execute(text("DELETE FROM support_chat_logs WHERE lower(user_message) LIKE :p ESCAPE '\\'"),
                        {"p": f"%{pat}%"}).rowcount
    return n


def _delete_cp(db: Session, user_id: str, cp_tables: set[str]) -> dict:
    out = {}
    u = {"u": user_id}
    if "jobs" in cp_tables:
        if "job_state_transitions" in cp_tables:
            out["cp.job_state_transitions"] = db.execute(text(
                "DELETE FROM cp.job_state_transitions WHERE job_id IN (SELECT id FROM cp.jobs WHERE user_id = :u)"
            ), u).rowcount
        if "payments" in cp_tables:
            # payments are kept; the FK would null this anyway (ON DELETE SET NULL)
            db.execute(text(
                "UPDATE cp.payments SET job_id = NULL WHERE job_id IN (SELECT id FROM cp.jobs WHERE user_id = :u)"
            ), u)
        db.execute(text("UPDATE cp.jobs SET idempotency_key_id = NULL WHERE user_id = :u"), u)
    if "idempotency_keys" in cp_tables:
        if "jobs" in cp_tables:
            out["cp.idempotency_keys"] = db.execute(text(
                "DELETE FROM cp.idempotency_keys WHERE user_id = :u "
                "OR job_id IN (SELECT id FROM cp.jobs WHERE user_id = :u)"
            ), u).rowcount
        else:
            out["cp.idempotency_keys"] = db.execute(text(
                "DELETE FROM cp.idempotency_keys WHERE user_id = :u"), u).rowcount
    if "jobs" in cp_tables:
        out["cp.jobs"] = db.execute(text("DELETE FROM cp.jobs WHERE user_id = :u"), u).rowcount
    return {k: v for k, v in out.items() if v}


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

    insp = inspect(db.connection())
    tables = set(insp.get_table_names())
    cp_tables = set(insp.get_table_names(schema=CP_SCHEMA)) if _has_cp(insp) else set()
    report = {"revoked_gmail": 0, "deleted": {}, "tombstoned_sends": 0}

    if "email_accounts" in tables:
        tokens = db.execute(
            text("SELECT refresh_token FROM email_accounts WHERE user_id = :u AND refresh_token IS NOT NULL"),
            {"u": user_id},
        ).scalars().all()
        # Raw SQL bypasses the ORM type, so decrypt here (legacy plaintext passes through).
        from services.gmail_tokens import decrypt_token
        report["revoked_gmail"] = sum(revoke_google_grant(decrypt_token(t)) for t in tokens)

    try:
        report["tombstoned_sends"] = _tombstone_sends(db, user_id, tables)
        refs = _collect_blob_refs(db, user_id, tables, cp_tables)
        n = _delete_chat_logs(db, user_id, email, tables)  # before session rows go
        if n:
            report["deleted"]["support_chat_logs"] = n
        report["deleted"].update(_delete_cp(db, user_id, cp_tables))
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

    # Outside our database, after the commit: never undoes it, only reports.
    from services import deletion_external
    report["external"] = deletion_external.delete_external(user_id, refs)

    logger.info("[ACCOUNT_DELETE] user %s deleted: %s", user_id, report)
    return report
