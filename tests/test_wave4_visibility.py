"""What customers see (audit P20/P22/P39/P40/P41)."""
import pathlib
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.models import Base, Campaign, EmailSent
from services.email_campaign.campaign_service import customer_failure_reason, get_campaign_metrics


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):  # pragma: no cover - test plumbing
    return "JSON"


@pytest.mark.parametrize("status,msg,expect", [
    ("failed", "Apollo could not find email for this contact", "No verified email address"),
    ("failed", "Token refresh failed: Gmail auth expired — x must reconnect", "Gmail disconnected"),
    ("failed", 'Send failed: Gmail send failed: 401: {"code": 401}', "Gmail disconnected"),
    ("failed", "Enrichment error: boom", "could not verify"),
    ("failed", "Send failed: Gmail send failed: 400: bad", "Gmail did not accept"),
    ("expired", None, "credit was returned"),
    ("sent", None, None),
])
def test_customer_safe_reasons(status, msg, expect):
    reason = customer_failure_reason(status, msg)
    assert (reason is None) if expect is None else (expect in reason)
    if reason:
        assert "Traceback" not in reason and "{" not in reason


def test_metrics_split_skips_from_failures_and_report_delivery():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Campaign.__table__, EmailSent.__table__])
    db = sessionmaker(bind=engine)()
    db.add(Campaign(id=1, candidate_id=None, name="c", status="completed", daily_limit=20,
                    pause_reason=None, credits_reserved=200, credits_released=30))
    for status, msg in ([("sent", None)] * 6 + [("failed", "Apollo could not find email for this contact")] * 3
                        + [("failed", "Send failed: 400")] * 1):
        db.add(EmailSent(campaign_id=1, status=status, error_message=msg, sent_at=datetime(2026, 9, 1)))
    db.commit()
    m = get_campaign_metrics(db, 1)
    assert (m["emails_failed"], m["emails_skipped_no_email"], m["emails_failed_other"]) == (4, 3, 1)
    assert (m["first_touch_delivered"], m["first_touch_total"]) == (6, 10)
    assert m["daily_limit"] == 20
    assert (m["credits_reserved"], m["credits_released"]) == (200, 30)
