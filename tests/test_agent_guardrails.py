"""Phase 1 guardrails for the student agent (contract section 0).

1. No spend: nothing under services/agent/ or api/routes_agent.py imports
   enrichment, Apollo, discovery, orders, scoring, the campaign worker, the
   Gmail sender, credits, create_campaign or transition_campaign.
2. Nothing is sent: no agent code constructs or writes emails_sent,
   campaigns, outreach_orders, user_credits or credit_ledger rows.
3. No addresses: no lead address appears in any agent HTTP response.
4. The tool registry is exactly the thirteen Phase 1 tools.
The end-to-end versions (a real turn with the model faked) are in
tests/test_agent_flow.py.
"""
import ast
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT_FILES = sorted((ROOT / "services" / "agent").glob("*.py")) + \
    [p for p in [ROOT / "api" / "routes_agent.py"] if p.exists()]
APP_FILES = [p for p in AGENT_FILES if p.name != "testing.py"]

FORBIDDEN_MODULES = (
    "services.enrichment", "api.routes_discovery", "api.routes_orders", "api.routes_scoring",
    "services.email_campaign.campaign_worker", "services.credits",
)
FORBIDDEN_FRAGMENTS = ("apollo", "gmail_send_service")
FORBIDDEN_NAMES = ("create_campaign", "transition_campaign")
SEND_TABLES = ("emails_sent", "campaigns", "outreach_orders", "user_credits", "credit_ledger")
SEND_MODELS = ("EmailSent", "Campaign", "OutreachOrder", "UserCredit", "CreditLedger")
PHASE1_TOOLS = {"get_profile", "get_status", "list_matches", "get_match", "list_outreach", "get_outreach",
                "draft_emails", "revise_draft", "skip", "remember", "forget", "navigate", "update_profile_notes"}


def _imports(path: pathlib.Path) -> list[str]:
    """Every module (and module.name) a file imports, statically or through importlib."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(["services", "agent"] + ([base] if base else []))
            found += [base] + [f"{base}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant):
                found.append(str(node.args[0].value))
    return found


def require(*modules):
    """Skip while another builder's module is missing or not importable yet."""
    import importlib
    try:
        return [importlib.import_module(m) for m in modules]
    except ImportError as e:
        pytest.skip(f"not importable yet: {e}")


def _forbidden(mod: str) -> bool:
    low = mod.lower()
    return (any(low == m or low.startswith(m + ".") for m in FORBIDDEN_MODULES)
            or any(f in low for f in FORBIDDEN_FRAGMENTS)
            or low.rsplit(".", 1)[-1] in FORBIDDEN_NAMES)


def test_the_scan_sees_the_agent_package():
    names = {p.name for p in AGENT_FILES}
    assert {"store.py", "schemas.py", "testing.py"} <= names
    assert _forbidden("services.enrichment.enrichment_service")
    assert _forbidden("services.email_campaign.campaign_service.create_campaign")
    assert _forbidden("services.apollo_client") and not _forbidden("services.email_campaign.email_generator_service")


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.name)
def test_agent_code_never_imports_a_spending_or_sending_module(path):
    bad = sorted({m for m in _imports(path) if _forbidden(m)})
    assert bad == [], f"{path.name} imports {bad}"


@pytest.mark.parametrize("path", APP_FILES, ids=lambda p: p.name)
def test_agent_code_never_writes_send_or_money_tables(path):
    src = path.read_text()
    tree = ast.parse(src)
    built = sorted({n.func.id for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in SEND_MODELS})
    assert built == [], f"{path.name} constructs {built}"
    sql = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?(%s)\b" % "|".join(SEND_TABLES), re.I)
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not [s for s in strings if sql.search(s)], f"{path.name} writes a send table in SQL"
    for model in SEND_MODELS:
        # ORM bulk writes: update(EmailSent) / insert(EmailSent) / delete(EmailSent)
        assert not re.search(rf"\b(update|insert|delete)\(\s*{model}\b", src), f"{path.name} writes {model}"
        assert not re.search(rf"query\(\s*{model}\b[^\n]*\)\s*\.\s*(update|delete)\(", src), \
            f"{path.name} bulk writes {model}"


def test_no_app_module_imports_the_test_harness():
    users = [p.name for p in APP_FILES if any(m.startswith("services.agent.testing") for m in _imports(p))]
    users += [p.relative_to(ROOT).as_posix() for p in (ROOT / "api").glob("*.py")
              if "services.agent.testing" in p.read_text()]
    assert users == []


def test_no_dashes_in_agent_source():
    for path in AGENT_FILES:
        text = path.read_text()
        assert "—" not in text and "–" not in text, path.name


def test_registry_is_exactly_the_phase1_tools():
    (tools,) = require("services.agent.tools")
    assert set(tools.REGISTRY) == PHASE1_TOOLS
    declared = {t["function"]["name"] for t in tools.TOOLS}
    assert declared == PHASE1_TOOLS
    for name in declared:
        assert not re.search(r"send|enrich|launch|reserve|apollo|reveal|pay|credit", name), name


# ---------------------------------------------------------------- HTTP: no address in any body

def _walk_strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)
    elif isinstance(value, str):
        yield value


def test_no_agent_read_route_returns_a_lead_address(monkeypatch):
    require("services.agent.actions", "api.routes_agent")
    from services.agent import store
    from services.agent.testing import (
        agent_client, enable_agent, make_engine, seed_student, session_factory, use_engine,
    )

    engine = make_engine()
    use_engine(monkeypatch, engine)
    enable_agent(monkeypatch)
    db = session_factory(engine)()
    s = seed_student(db, matches=6, with_email=3)
    d = store.insert_drafting(db, s.user_id, s.candidate_id, s.lead_ids[4])
    store.update_draft_copy(db, d.id, s.user_id, subject="quick question", body="Hi")
    db.commit()
    client = agent_client(engine, s.user_id)
    paths = ["bootstrap", "status", "matches?limit=50", "matches?state=contacted", f"matches/{s.lead_ids[0]}",
             f"matches/{s.lead_ids[1]}", "outreach?status=all", f"outreach/e_{s.email_ids[0]}",
             f"outreach/d_{d.id}", "replies", "profile", "memories", "settings", "threads"]
    for path in paths:
        res = client.get(f"/api/v1/agent/{path}")
        assert res.status_code == 200, (path, res.text[:300])
        body = res.json()
        leaked = [x for x in _walk_strings(body) if any(a in x for a in s.addresses)]
        assert leaked == [], (path, leaked)
        assert "to_email" not in json.dumps(body) and "phone" not in json.dumps(body).lower(), path


# ---------------------------------------------------------------- reply text: no address or number (invariant 3)

REAL_REPLY = (
    "Hi Asha,\n\nThanks for writing. Happy to chat next week. Call me on +91 98450 12345 or write to "
    "priya@kitebase.in, and copy (080) 4123-4567 if it is urgent.\n\n"
    "Priya Nair\nHead of Product, Kitebase\n\n"
    "On Mon, 12 Oct 2026 at 10:15, Asha Verma <asha.verma@gmail.com> wrote:\n"
    "> Hi Priya, I built Splitkart, a UPI bill splitter.\n> Could we talk?\n"
)
REPLY_SECRETS = ("priya@kitebase.in", "98450", "12345", "4123-4567", "asha.verma@gmail.com", "wrote:",
                 "Could we talk?")


def test_redact_contact_cuts_quoted_history_and_removes_addresses_and_numbers():
    (sw,) = require("services.agent.status_words")
    out = sw.redact_contact(REAL_REPLY)
    assert out.startswith("Hi Asha,") and "Happy to chat next week." in out and "Priya Nair" in out
    assert [s for s in REPLY_SECRETS if s in out] == []
    assert out.count(sw.REDACTED) == 3
    # Short numbers and dates stay; other quoted-history shapes are cut too.
    keep = "We have 120 interns, met on 12.10.2026 at 10:30, budget 2026-10-12."
    assert sw.redact_contact(keep) == keep
    assert sw.redact_contact("Sure.\n-----Original Message-----\nFrom: a@b.co") == "Sure."
    assert sw.redact_contact("Sure.\nFrom: Priya <p@k.in>\nSent: Monday") == "Sure."
    assert sw.redact_contact("Thanks!\nOn Mon, 12 Oct 2026, Priya Nair <\npriya@kitebase.in> wrote:\n> x") == "Thanks!"
    assert sw.redact_contact(None) is None and sw.redact_contact("") == ""


def test_a_real_reply_never_shows_an_address_or_number_to_the_ui_or_the_model(monkeypatch):
    require("services.agent.actions", "api.routes_agent", "services.agent.runtime")
    import queue

    from database.models import EmailSent
    from services.agent import runtime
    from services.agent.testing import (
        FakeLLM, agent_client, drain, enable_agent, make_engine, seed_student, session_factory, start_turn, text,
        tool, use_engine,
    )

    engine = make_engine()
    use_engine(monkeypatch, engine)
    enable_agent(monkeypatch)
    db = session_factory(engine)()
    s = seed_student(db, matches=4, with_email=2)
    replied = db.get(EmailSent, s.email_ids[0])
    replied.reply_text = REAL_REPLY
    db.commit()
    oid = f"e_{replied.id}"

    client = agent_client(engine, s.user_id)
    for path in ("replies", f"outreach/{oid}", "bootstrap"):
        res = client.get(f"/api/v1/agent/{path}")
        assert res.status_code == 200, (path, res.text[:300])
        leaked = [x for x in REPLY_SECRETS if x in res.text]
        assert leaked == [], (path, leaked)
    assert "Happy to chat next week." in client.get("/api/v1/agent/replies").text

    fake = FakeLLM([tool("get_outreach", {"outreach_id": oid}, "r1"), text("Priya wants to chat next week.")])
    fake.install(monkeypatch)
    run_id, _, _ = start_turn(db, s.user_id, "What did Priya say?", page_context={"route": "outreach",
                                                                                   "outreach_id": oid})
    q = queue.Queue()
    runtime.run_turn(run_id, q, None)
    frames = drain(q)
    assert frames[-1]["type"] == "done"
    model_saw, ui_got = json.dumps(fake.seen), json.dumps(frames)
    assert "untrusted_reply_text" in model_saw and "Happy to chat next week." in model_saw
    for secret in REPLY_SECRETS:
        assert secret not in model_saw and secret not in ui_got, secret
    db.close()
