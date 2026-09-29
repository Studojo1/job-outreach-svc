"""Privacy Policy v2.0 §15: what account deletion covers and what it keeps.

Runs the real delete_account, the real external-deletion calls (with the
Azure client and HTTP stubbed) and the real retention job against SQLite.
The control-plane schema is an attached database named cp.
"""
import hashlib
import json
import pathlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import account_deletion, deletion_external, retention
from services.account_deletion import UnclassifiedUserTables, delete_account

SENT = datetime(2026, 9, 1, 10, 0, 0)
ACCOUNT = "studojostorage"
RESUME_URL = f"https://{ACCOUNT}.blob.core.windows.net/resumes/application-uploads/u1/1700-abc.pdf"
ASSIGNMENT_URL = f"https://{ACCOUNT}.blob.core.windows.net/assignments/u1/out%20file.docx?sv=x"

DDL = [
    'CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, image TEXT,'
    ' phone_number TEXT, email_verified BOOLEAN, banned BOOLEAN, ban_reason TEXT, updated_at TIMESTAMP)',
    "CREATE TABLE session (id INTEGER PRIMARY KEY, user_id TEXT, ip_address TEXT, user_agent TEXT)",
    "CREATE TABLE candidates (id INTEGER PRIMARY KEY, user_id TEXT)",
    "CREATE TABLE leads (id INTEGER PRIMARY KEY, candidate_id INTEGER, email TEXT)",
    "CREATE TABLE campaigns (id INTEGER PRIMARY KEY, candidate_id INTEGER, outreach_order_id INTEGER)",
    "CREATE TABLE emails_sent (id INTEGER PRIMARY KEY, campaign_id INTEGER, lead_id INTEGER, to_email TEXT,"
    " subject TEXT, body TEXT, sent_at TIMESTAMP, status TEXT, followup_number INTEGER)",
    "CREATE TABLE outreach_orders (id INTEGER PRIMARY KEY, user_id TEXT)",
    "CREATE TABLE payment_orders (id INTEGER PRIMARY KEY, user_id TEXT, outreach_order_id INTEGER)",
    "CREATE TABLE linkedin_connection_requests (id INTEGER PRIMARY KEY, campaign_id INTEGER, user_id TEXT,"
    " name TEXT, profile_url TEXT, profile_urn TEXT, connection_note TEXT, status TEXT, sent_at TIMESTAMP,"
    " followup_sent_at TIMESTAMP)",
    "CREATE TABLE application_resume_uploads (id INTEGER PRIMARY KEY, user_id TEXT, url TEXT)",
    "CREATE TABLE support_chat_logs (id INTEGER PRIMARY KEY, session_id TEXT, user_message TEXT,"
    " bot_response TEXT, ip_address TEXT, user_agent TEXT, created_at TIMESTAMP)",
    "CREATE TABLE deleted_account_sends (id INTEGER PRIMARY KEY, channel TEXT NOT NULL, recipient_hash TEXT NOT NULL,"
    " campaign_id INTEGER, sent_at TIMESTAMP, status TEXT, followup_number INTEGER, payment_order_id INTEGER,"
    " deleted_at TIMESTAMP NOT NULL)",
    "CREATE TABLE cp.jobs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, idempotency_key_id TEXT, payload TEXT,"
    " result TEXT)",
    "CREATE TABLE cp.idempotency_keys (id TEXT PRIMARY KEY, key TEXT, job_id TEXT, user_id TEXT NOT NULL)",
    "CREATE TABLE cp.job_state_transitions (id TEXT PRIMARY KEY, job_id TEXT)",
    "CREATE TABLE cp.payments (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, job_id TEXT, amount INTEGER)",
    "CREATE TABLE cp.deployment_history (id INTEGER PRIMARY KEY, sha TEXT)",
]


def h(v):
    return hashlib.sha256(v.strip().lower().encode()).hexdigest()


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://")

    @event.listens_for(eng, "connect")
    def _attach(dbapi_conn, _rec):
        dbapi_conn.execute("ATTACH DATABASE ':memory:' AS cp")

    return eng


@pytest.fixture()
def db(engine, monkeypatch):
    with engine.begin() as c:
        for stmt in DDL:
            c.execute(text(stmt))
        for uid, email, ip in (("u1", "stu@gmail.com", "1.1.1.1"), ("u2", "other@gmail.com", "2.2.2.2")):
            c.execute(text('INSERT INTO "user" (id, email, name) VALUES (:u, :e, :u)'), {"u": uid, "e": email})
            c.execute(text("INSERT INTO session (user_id, ip_address, user_agent) VALUES (:u, :ip, 'UA')"),
                      {"u": uid, "ip": ip})
        c.execute(text("INSERT INTO candidates (id, user_id) VALUES (1, 'u1'), (2, 'u2')"))
        c.execute(text("INSERT INTO leads (id, candidate_id, email) VALUES (1, 1, 'HM@corp.com'), (2, 1, 'x@y.com'),"
                       " (3, 2, 'hm@corp.com')"))
        c.execute(text("INSERT INTO outreach_orders (id, user_id) VALUES (5, 'u1')"))
        c.execute(text("INSERT INTO payment_orders (id, user_id, outreach_order_id) VALUES (9, 'u1', 5)"))
        c.execute(text("INSERT INTO campaigns (id, candidate_id, outreach_order_id) VALUES (10, 1, 5), (20, 2, NULL)"))
        c.execute(text(
            "INSERT INTO emails_sent (campaign_id, lead_id, to_email, subject, body, sent_at, status, followup_number)"
            " VALUES (10, 1, 'HM@corp.com', 'Hi', 'secret body', :s, 'sent', 0),"
            "        (10, 1, 'HM@corp.com', 'Re: Hi', 'follow', :s, 'replied', 1),"
            "        (10, 2, 'x@y.com', 'Hi', 'not yet', NULL, 'queued', 0),"
            "        (NULL, 2, 'x@y.com', 'ext', 'extension send', :s, 'sent', 0),"
            "        (20, 3, 'hm@corp.com', 'Hi', 'other user', :s, 'sent', 0)"
        ), {"s": SENT})
        c.execute(text(
            "INSERT INTO linkedin_connection_requests (campaign_id, user_id, name, profile_url, status, sent_at)"
            " VALUES (3, 'u1', 'Asha', 'https://linkedin.com/in/asha', 'accepted', :s),"
            "        (3, 'u1', 'Not sent', 'https://linkedin.com/in/ns', 'pending', NULL)"
        ), {"s": SENT})
        c.execute(text("INSERT INTO application_resume_uploads (user_id, url) VALUES ('u1', :r)"), {"r": RESUME_URL})
        c.execute(text(
            "INSERT INTO support_chat_logs (session_id, user_message, bot_response, ip_address, user_agent, created_at)"
            " VALUES ('a', 'how do refunds work', 'x', '1.1.1.1', 'UA', :t),"
            "        ('b', 'my email is Stu@Gmail.com', 'x', '9.9.9.9', 'Other', :t),"
            "        ('c', 'hello', 'x', '2.2.2.2', 'UA', :t),"
            "        ('d', 'same ip other browser', 'x', '1.1.1.1', 'Firefox', :t)"
        ), {"t": SENT})
        c.execute(text(
            "INSERT INTO cp.jobs (id, user_id, idempotency_key_id, payload, result) VALUES"
            " ('j1', 'u1', 'k1', '{}', :r), ('j2', 'u2', 'k2', '{}', '{}')"
        ), {"r": json.dumps({"file": ASSIGNMENT_URL})})
        c.execute(text("INSERT INTO cp.idempotency_keys (id, key, job_id, user_id) VALUES"
                       " ('k1', 'a', 'j1', 'u1'), ('k2', 'b', 'j2', 'u2')"))
        c.execute(text("INSERT INTO cp.job_state_transitions (id, job_id) VALUES ('t1', 'j1'), ('t2', 'j2')"))
        c.execute(text("INSERT INTO cp.payments (id, user_id, job_id, amount) VALUES ('p1', 'u1', 'j1', 13900),"
                       " ('p2', 'u2', 'j2', 13900)"))
    monkeypatch.setattr(account_deletion, "revoke_google_grant", lambda t: True)
    external = []
    monkeypatch.setattr(deletion_external, "delete_external",
                        lambda uid, refs=(): external.append((uid, set(refs))) or {"stub": True})
    monkeypatch.setattr(deletion_external.settings, "AZURE_STORAGE_ACCOUNT_NAME", ACCOUNT)
    s = sessionmaker(bind=engine)()
    s.external = external
    yield s
    s.close()


def q(db, sql, **p):
    return db.execute(text(sql), p).all()


def test_sends_are_tombstoned_without_content_or_address(db):
    report = delete_account(db, "u1")

    rows = q(db, "SELECT channel, recipient_hash, campaign_id, sent_at, status, followup_number, payment_order_id,"
                 " deleted_at FROM deleted_account_sends ORDER BY id")
    assert report["tombstoned_sends"] == 4
    email = [r for r in rows if r.channel == "email"]
    assert sorted((r.recipient_hash, r.campaign_id, r.status, r.followup_number, r.payment_order_id) for r in email) \
        == sorted([(h("hm@corp.com"), 10, "sent", 0, 9), (h("hm@corp.com"), 10, "replied", 1, 9),
                   (h("x@y.com"), None, "sent", 0, None)])
    li = [r for r in rows if r.channel == "linkedin"]
    assert [(r.recipient_hash, r.status) for r in li] == [(h("https://linkedin.com/in/asha"), "accepted")]
    assert all(r.deleted_at is not None and r.sent_at is not None for r in rows)
    # the table has no column that could hold content or a readable address
    cols = {r[1] for r in q(db, "PRAGMA table_info(deleted_account_sends)")}
    assert cols == {"id", "channel", "recipient_hash", "campaign_id", "sent_at", "status", "followup_number",
                    "payment_order_id", "deleted_at"}


def test_control_plane_rows_go_and_payments_stay(db):
    report = delete_account(db, "u1")
    assert q(db, "SELECT id FROM cp.jobs") == [("j2",)]
    assert q(db, "SELECT id FROM cp.idempotency_keys") == [("k2",)]
    assert q(db, "SELECT id FROM cp.job_state_transitions") == [("t2",)]
    assert q(db, "SELECT id, user_id, job_id FROM cp.payments ORDER BY id") == [("p1", "u1", None), ("p2", "u2", "j2")]
    assert report["deleted"]["cp.jobs"] == 1


def test_chatbot_history_matched_on_own_sessions_and_address(db):
    report = delete_account(db, "u1")
    assert sorted(r[0] for r in q(db, "SELECT session_id FROM support_chat_logs")) == ["c", "d"]
    assert report["deleted"]["support_chat_logs"] == 2


def test_blob_urls_are_collected_before_rows_go(db):
    delete_account(db, "u1")
    [(uid, refs)] = db.external
    assert uid == "u1"
    assert refs == {("resumes", "application-uploads/u1/1700-abc.pdf"), ("assignments", "u1/out file.docx")}


def test_unclassified_control_plane_table_blocks_deletion(db):
    db.execute(text("CREATE TABLE cp.new_thing (id INTEGER PRIMARY KEY, user_id TEXT)"))
    db.commit()
    with pytest.raises(UnclassifiedUserTables, match="cp.new_thing"):
        delete_account(db, "u1")
    assert q(db, "SELECT count(*) FROM cp.jobs") == [(2,)]


# ── external: blobs and analytics ────────────────────────────────────────────

class _Blob:
    def __init__(self, name):
        self.name = name


class _Container:
    def __init__(self, svc, name):
        self.svc, self.name = svc, name

    def list_blobs(self, name_starts_with=None):
        return [_Blob(n) for n in self.svc.store.get(self.name, []) if n.startswith(name_starts_with or "")]

    def delete_blob(self, name, delete_snapshots=None):
        self.svc.deleted.append((self.name, name))


class _Svc:
    def __init__(self, store):
        self.store, self.deleted = store, []

    def get_container_client(self, name):
        return _Container(self, name)


def test_user_files_are_deleted_from_blob_storage(monkeypatch):
    s = deletion_external.settings
    monkeypatch.setattr(s, "AZURE_STORAGE_ACCOUNT_NAME", ACCOUNT)
    monkeypatch.setattr(s, "AZURE_STORAGE_ACCOUNT_KEY", "k")
    svc = _Svc({
        "resumes": ["application-uploads/u1/a.pdf", "application-uploads/u10/b.pdf", "templates/x.tex"],
        "humanizer-temp": ["u1/2026-abc/essay.docx", "u2/x.docx"],
        "ticket-screenshots": ["1700-ab12-u1.png", "1700-cd34-u10.png"],
    })
    monkeypatch.setattr(deletion_external, "_blob_service", lambda: svc)
    out = deletion_external.delete_user_blobs("u1", {("assignments", "u1/out.docx")})
    assert sorted(svc.deleted) == sorted([
        ("resumes", "application-uploads/u1/a.pdf"), ("humanizer-temp", "u1/2026-abc/essay.docx"),
        ("ticket-screenshots", "1700-ab12-u1.png"), ("assignments", "u1/out.docx"),
    ])
    assert out == {"status": "ok", "deleted": 4, "errors": 0}


def test_unconfigured_integrations_are_skipped_and_named(monkeypatch, caplog):
    s = deletion_external.settings
    for k in ("AZURE_STORAGE_ACCOUNT_NAME", "AZURE_STORAGE_ACCOUNT_KEY", "POSTHOG_PERSONAL_API_KEY",
              "POSTHOG_PROJECT_ID", "MIXPANEL_PROJECT_TOKEN", "MIXPANEL_GDPR_TOKEN"):
        monkeypatch.setattr(s, k, "")
    monkeypatch.setattr(deletion_external.requests, "post", lambda *a, **k: pytest.fail("no call"))
    out = deletion_external.delete_external("u1")
    assert {k: v["status"] for k, v in out.items()} == {"blobs": "skipped", "posthog": "skipped", "mixpanel": "skipped"}
    assert out["mixpanel"]["missing"] == ["MIXPANEL_PROJECT_TOKEN", "MIXPANEL_GDPR_TOKEN"]
    assert "Mixpanel data NOT deleted" in caplog.text


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.ok, self.text = status, body or {}, status < 400, ""

    def json(self):
        return self._body

    def raise_for_status(self):
        assert self.ok


def test_analytics_providers_are_asked_to_delete(monkeypatch):
    s = deletion_external.settings
    monkeypatch.setattr(s, "POSTHOG_PERSONAL_API_KEY", "phx")
    monkeypatch.setattr(s, "POSTHOG_PROJECT_ID", "42")
    monkeypatch.setattr(s, "POSTHOG_HOST", "https://eu.i.posthog.com")
    monkeypatch.setattr(s, "POSTHOG_API_HOST", "")
    monkeypatch.setattr(s, "MIXPANEL_PROJECT_TOKEN", "tok")
    monkeypatch.setattr(s, "MIXPANEL_GDPR_TOKEN", "oauth")
    calls = []
    r = deletion_external.requests
    monkeypatch.setattr(r, "get", lambda url, **k: calls.append(("GET", url, k)) or _Resp(200, {"results": [{"id": "p-uuid"}]}))
    monkeypatch.setattr(r, "delete", lambda url, **k: calls.append(("DELETE", url, k)) or _Resp(202))
    monkeypatch.setattr(r, "post", lambda url, **k: calls.append(("POST", url, k)) or _Resp(200, {"results": {"task_id": "t1"}}))

    assert deletion_external.delete_posthog_person("u1") == {"status": "ok", "deleted": 1}
    assert deletion_external.delete_mixpanel_user("u1") == {"status": "requested", "task_id": "t1"}
    get, delete, post = calls
    assert get[1] == "https://eu.posthog.com/api/projects/42/persons/" and get[2]["params"] == {"distinct_id": "u1"}
    assert get[2]["headers"]["Authorization"] == "Bearer phx"
    assert delete[1] == "https://eu.posthog.com/api/projects/42/persons/p-uuid/"
    assert delete[2]["params"] == {"delete_events": "true"}
    assert post[1] == "https://mixpanel.com/api/app/data-deletions/v3.0/"
    assert post[2]["params"] == {"token": "tok"} and post[2]["headers"]["Authorization"] == "Bearer oauth"
    assert post[2]["json"] == {"distinct_ids": ["u1"], "compliance_type": "GDPR"}


# ── retention ────────────────────────────────────────────────────────────────

def test_retention_deletes_only_rows_past_their_period(db):
    now = datetime(2026, 9, 29, 12, 0, 0)
    db.execute(text("DELETE FROM support_chat_logs"))
    for sid, age in (("old", 366), ("edge", 364), ("new", 1)):
        db.execute(text("INSERT INTO support_chat_logs (session_id, user_message, bot_response, created_at)"
                        " VALUES (:s, 'm', 'r', :t)"), {"s": sid, "t": now - timedelta(days=age)})
    for n, age in ((1, 3 * 365 + 1), (2, 3 * 365 - 1), (3, 10)):
        db.execute(text("INSERT INTO deleted_account_sends (id, channel, recipient_hash, deleted_at)"
                        " VALUES (:i, 'email', 'h', :t)"), {"i": n, "t": now - timedelta(days=age)})
    db.commit()

    counts = retention.run(db, now=now)

    assert counts == {"support_chat_logs": 1, "deleted_account_sends": 1}
    assert sorted(r[0] for r in q(db, "SELECT session_id FROM support_chat_logs")) == ["edge", "new"]
    assert sorted(r[0] for r in q(db, "SELECT id FROM deleted_account_sends")) == [2, 3]


def test_retention_runs_from_the_hourly_sweep(monkeypatch):
    from services import campaign_notices, launch_nudge, reconcile

    ran = []
    monkeypatch.setattr(reconcile, "run", lambda db, now: None)
    monkeypatch.setattr(campaign_notices, "run", lambda db, now: None)
    monkeypatch.setattr(retention, "run", lambda db, now: ran.append(now))
    monkeypatch.setattr(launch_nudge, "sweep", lambda db, now: {"nudged": [], "stuck": []})

    from database.models import SystemEvent
    eng = create_engine("sqlite://")
    SystemEvent.__table__.create(eng)
    s = sessionmaker(bind=eng)()
    launch_nudge.maybe_sweep(s)
    assert len(ran) == 1
