"""Areas audit, 30 Sep 2026: database hot paths and deploy-time 502s.

AR-D02: company_profiles was read with name ILIKE :name, which no btree index
serves (4.1M full scans of the 94k-row table). profile_by_name compares
lower(name) = :name, which the migration 075 expression index serves.
AR-D01 / AR-D03: migration 075 adds the emails_sent indexes the 30-second
campaign cycle needs and drops three duplicate indexes, all CONCURRENTLY.
AR-B01: deploys patch a preStop sleep so a terminating pod stops getting
traffic before it exits (rollouts answered live users with 502s).
"""
import re
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from database.models import Base, CompanyProfile
from services.company_intelligence.lead_backfill import profile_by_name

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "migrations" / "075_hot_path_indexes.sql"


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[CompanyProfile.__table__])
    statements = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cur, stmt, *a: statements.append(stmt))
    s = sessionmaker(bind=engine)()
    s.add_all([
        CompanyProfile(id=1, domain="acme.io", name="Acme"),
        CompanyProfile(id=2, domain="acme-two.io", name="ACME"),
        CompanyProfile(id=3, domain="axb.io", name="AXB Labs"),
        CompanyProfile(id=4, domain="pct.io", name="100 Percent"),
    ])
    s.commit()
    s.statements = statements
    yield s
    s.close()


def test_name_lookup_ignores_case_and_spaces_and_is_deterministic(db):
    assert profile_by_name(db, "  acme ").id == 1
    assert profile_by_name(db, "AcMe").id == 1


def test_name_lookup_does_not_treat_underscore_or_percent_as_wildcards(db):
    assert profile_by_name(db, "A_B Labs") is None
    assert profile_by_name(db, "100%") is None
    assert profile_by_name(db, "%") is None


def test_blank_name_does_not_query(db):
    db.statements.clear()
    assert profile_by_name(db, "") is None
    assert profile_by_name(db, None) is None
    assert db.statements == []


def test_name_lookup_uses_the_indexed_expression(db):
    db.statements.clear()
    profile_by_name(db, "Acme")
    sql = " ".join(db.statements).lower()
    assert "lower(company_profiles.name) =" in sql
    assert "like" not in sql


def test_no_ilike_name_lookups_left():
    offenders = []
    for path in list((ROOT / "services").rglob("*.py")) + list((ROOT / "api").rglob("*.py")):
        if re.search(r"CompanyProfile\.name\.ilike\(", path.read_text()):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"use profile_by_name (index-backed) instead of ILIKE: {offenders}"


def _statements():
    body = "\n".join(l.split("--")[0] for l in MIGRATION.read_text().splitlines())
    return [s.strip() for s in body.split(";") if s.strip()]


def test_index_migration_never_locks_writes():
    stmts = _statements()
    assert stmts
    for s in stmts:
        assert re.match(r"(CREATE|DROP) INDEX CONCURRENTLY IF (NOT )?EXISTS ", s), s
    text = MIGRATION.read_text().upper()
    assert "BEGIN" not in re.sub(r"--.*", "", text), "CONCURRENTLY cannot run in a transaction"


def test_index_migration_covers_the_hot_paths():
    joined = " ".join(re.sub(r"\s+", " ", s) for s in _statements())
    assert "ON emails_sent (campaign_id, status)" in joined
    assert "ON emails_sent (status, scheduled_at)" in joined
    assert "ON emails_sent (lead_id)" in joined
    # Must be exactly the expression profile_by_name filters on.
    assert "ON company_profiles (lower(name))" in joined
    # Only exact duplicates of a unique index are dropped, never the unique one.
    for kept in ("company_profiles_domain_key", "uq_lead_scores_lead",
                 "payment_orders_razorpay_order_id_key"):
        assert f"DROP INDEX CONCURRENTLY IF EXISTS {kept}" not in joined


@pytest.mark.parametrize("workflow", ["deploy.yml", "deploy-staging.yml"])
def test_deploys_drain_before_the_pod_stops(workflow):
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text())
    runs = "\n".join(step.get("run", "") for job in wf["jobs"].values() for step in job["steps"])
    patch = runs.index('"preStop":{"sleep":{"seconds":')
    assert patch < runs.index("kubectl set image deployment/"), "patch before the rollout starts"
