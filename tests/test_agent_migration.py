"""Migration 076 and the agent models describe the same eight tables.

check_schema only compares table and column names at startup, so a model
column missing from the SQL blocks a rollout, and a nullability mismatch is
found by nobody until an insert fails. This file compares names, nullability
and index names with the SQL, checks the file can run inside the frontend
migrator's transaction, and that deletion and export know every agent table.
"""
import pathlib
import re

import pytest

from database.models import Base
from services import account_deletion, data_export

ROOT = pathlib.Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "migrations" / "076_agent_runtime.sql"
FRONTEND_COPY = ROOT.parent / "frontend" / "drizzle" / "0037_agent_runtime.sql"
AGENT_TABLES = ("agent_threads", "agent_runs", "agent_messages", "agent_drafts", "agent_confirmations",
                "agent_memories", "agent_settings", "agent_usage_daily")
SQL = SQL_PATH.read_text()


def _sql_tables() -> dict[str, dict[str, bool]]:
    """table -> {column: nullable} from the CREATE TABLE blocks."""
    out = {}
    for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SQL, re.S):
        cols, pk = {}, set()
        for line in body.strip().splitlines():
            line = line.strip().rstrip(",")
            m = re.match(r"PRIMARY KEY \((.*)\)", line)
            if m:
                pk |= {c.strip() for c in m.group(1).split(",")}
                continue
            col, rest = line.split(None, 1)
            cols[col] = not ("NOT NULL" in rest or "PRIMARY KEY" in rest)
        for c in pk:
            cols[c] = False
        out[name] = cols
    return out


def test_sql_creates_exactly_the_eight_agent_tables():
    assert tuple(_sql_tables()) == AGENT_TABLES


@pytest.mark.parametrize("table", AGENT_TABLES)
def test_model_columns_and_nullability_match_the_sql(table):
    model = Base.metadata.tables[table]
    sql_cols = _sql_tables()[table]
    assert [c.name for c in model.columns] == list(sql_cols), table
    for col in model.columns:
        assert col.nullable == sql_cols[col.name], f"{table}.{col.name} nullable"


@pytest.mark.parametrize("table", AGENT_TABLES)
def test_model_indexes_match_the_sql(table):
    sql_idx = set(re.findall(rf"INDEX IF NOT EXISTS (\w+)\s+ON {table} ", SQL))
    model_idx = {i.name for i in Base.metadata.tables[table].indexes}
    assert model_idx == sql_idx
    for idx in Base.metadata.tables[table].indexes:
        assert idx.unique == (f"UNIQUE INDEX IF NOT EXISTS {idx.name}" in SQL), idx.name


def test_sql_runs_inside_the_frontend_migrators_transaction():
    code = "\n".join(line.split("--")[0] for line in SQL.splitlines())
    assert not re.search(r"\b(BEGIN|COMMIT|CONCURRENTLY)\b", code)
    assert code.count("CREATE TABLE IF NOT EXISTS") == 8
    assert "CREATE TABLE " not in code.replace("CREATE TABLE IF NOT EXISTS", "")
    assert "CREATE INDEX " not in code.replace("CREATE INDEX IF NOT EXISTS", "")
    assert 'REFERENCES "user"(id)' in code and "REFERENCES user(" not in code
    assert "—" not in SQL and "–" not in SQL


@pytest.mark.skipif(not FRONTEND_COPY.exists(), reason="frontend checkout not alongside this repo")
def test_frontend_copy_is_byte_identical():
    assert FRONTEND_COPY.read_bytes() == SQL_PATH.read_bytes()


def test_deletion_removes_agent_tables_children_first_before_candidates():
    order = list(account_deletion.DELETE)
    expected = ["agent_confirmations", "agent_memories", "agent_drafts", "agent_messages", "agent_runs",
                "agent_threads", "agent_settings", "agent_usage_daily"]
    assert [t for t in order if t.startswith("agent_")] == expected
    assert max(order.index(t) for t in expected) < order.index("candidates")
    assert not set(expected) & account_deletion.KEEP


def test_export_has_no_address_or_internal_columns():
    cols = (data_export.AGENT_THREAD_COLS + data_export.AGENT_MESSAGE_COLS + data_export.AGENT_DRAFT_COLS
            + data_export.AGENT_MEMORY_COLS + data_export.AGENT_SETTINGS_COLS)
    for bad in ("email", "to_email", "tool_calls", "meta", "events", "args", "approved_hash", "error"):
        assert bad not in cols
    for table, allow in (("agent_threads", data_export.AGENT_THREAD_COLS),
                         ("agent_messages", data_export.AGENT_MESSAGE_COLS),
                         ("agent_drafts", data_export.AGENT_DRAFT_COLS),
                         ("agent_memories", data_export.AGENT_MEMORY_COLS),
                         ("agent_settings", data_export.AGENT_SETTINGS_COLS)):
        assert set(allow) <= set(Base.metadata.tables[table].columns.keys()), table


def test_export_includes_chat_and_drafts_but_not_tool_rows():
    from services.agent import store
    from services.agent.testing import make_engine, seed_student, session_factory, start_turn

    db = session_factory(make_engine())()
    s = seed_student(db, user_id="ux")
    run_id, _, thread_id = start_turn(db, "ux", "Hello there")
    store.save_assistant_tool_calls(db, run_id, [{"id": "c1", "type": "function",
                                                  "function": {"name": "get_status", "arguments": "{}"}}])
    store.save_tool_result(db, run_id, "c1", "get_status", '{"internal": true}', summary="Checked")
    store.save_final_message(db, run_id, "Hi Asha")
    store.finish_run(db, run_id, "done")
    d = store.insert_drafting(db, "ux", s.candidate_id, s.lead_ids[0])
    store.update_draft_copy(db, d.id, "ux", subject="quick question", body="Hi")
    store.save_memory(db, "ux", "Short emails please", scope="emails")
    store.update_settings(db, "ux", followups=False)
    db.commit()
    out = data_export.export_user_data(db, "ux")["agent"]
    assert [m["content"] for m in out["messages"]] == ["Hello there", "Hi Asha"]
    assert out["drafts"][0]["subject"] == "quick question" and out["threads"][0]["id"] == thread_id
    assert out["memories"][0]["text"] == "Short emails please" and out["settings"]["followups"] is False
