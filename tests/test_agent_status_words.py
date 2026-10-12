"""Pins the outreach status words of contract section 1: every draft and email
status maps to its word and its exact note, hidden statuses vanish, and the
reply groups follow the sentiment rules."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from services.agent import status_words as sw

LEAD = SimpleNamespace(id=9912, name="Ananya Rao", title="Engineering Lead", company="Kitebase",
                       company_domain="kitebase.in")
T = datetime(2026, 10, 12, 11, 2, 10, 123456)


def _draft(status, flags=None, body="Hi Ananya, I built Splitkart, a UPI bill splitter"):
    return SimpleNamespace(id=88, lead_id=9912, status=status, subject="quick question Ananya", body=body,
                           flags=flags or [], updated_at=T, version=1, style=None, instructions=None,
                           facts_used=[], versions=[], approved_at=None)


def _email(status, **kw):
    base = dict(id=4512, lead_id=9912, status=status, subject="quick question Karan", body="Hi Karan,",
                reply_received_at=None, sent_at=None, scheduled_at=None, status_changed_at=None,
                reply_sentiment=None, reply_text=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("status,word,note,busy", [
    ("drafting", "needs_you", "Writing your email.", True),
    ("needs_review", "needs_you", "Draft ready. Review it.", False),
    ("approved", "scheduled", "Approved. Sending comes next, so nothing has gone out.", False),
    ("skipped", "skipped", "You passed on this person.", False),
    ("failed", "failed", "I couldn't write this one. Try again.", False),
])
def test_each_visible_draft_status_has_its_word_and_note(status, word, note, busy):
    row = sw.draft_row(_draft(status), LEAD)
    assert (row.status_word, row.note, row.busy, row.server_status) == (word, note, busy, status)
    assert row.id == "d_88" and row.kind == "draft" and row.when == "2026-10-12T11:02:10"
    assert row.preview == ("" if status == "drafting" else "Hi Ananya, I built Splitkart, a UPI bill splitter")


def test_a_flagged_draft_asks_the_student_to_check_the_marked_line():
    row = sw.draft_row(_draft("needs_review", flags=[{"text": "40%", "reason": "x"}]), LEAD)
    assert row.note == "Draft ready. Check the marked line."


@pytest.mark.parametrize("status", ["discarded", "superseded", "sent"])
def test_hidden_draft_statuses_produce_no_row(status):
    assert sw.draft_row(_draft(status), LEAD) is None


@pytest.mark.parametrize("status,word,note", [
    ("pending_enrichment", "scheduled", "Waiting for its send slot."),
    ("queued", "scheduled", "Waiting for its send slot."),
    ("sending", "scheduled", "Waiting for its send slot."),
    ("sent", "sent", "Sent."),
    ("replied", "replied", "They wrote back."),
    ("bounced", "failed", "This address did not accept the email."),
    ("failed", "failed", "This one could not be sent."),
    ("generation_failed", "failed", "This one could not be sent."),
    ("expired", "failed", "Not sent before the campaign ended. The credit came back to you."),
])
def test_each_email_status_has_its_word_and_note(status, word, note):
    row = sw.email_row(_email(status), LEAD, [])
    assert (row.status_word, row.note, row.kind, row.id, row.version) == (word, note, "email", "e_4512", None)


@pytest.mark.parametrize("status", ["cancelled", "skipped", "", None, "followup_pending"])
def test_other_email_statuses_are_hidden(status):
    assert sw.email_row(_email(status), LEAD, []) is None


def test_a_sent_email_with_a_planned_follow_up_says_so_and_nests_it():
    fu = SimpleNamespace(followup_number=1, status="followup_pending", scheduled_at=T, sent_at=None)
    row = sw.email_row(_email("sent", sent_at=T), LEAD, [fu])
    assert row.note == "Sent. Follow-up planned."
    assert [(f.number, f.status) for f in row.followups] == [(1, "followup_pending")]


def test_email_when_prefers_reply_then_sent_then_scheduled_then_status_change():
    later = datetime(2026, 10, 13, 9, 0, 0)
    assert sw.email_row(_email("replied", reply_received_at=later, sent_at=T), LEAD).when == "2026-10-13T09:00:00"
    assert sw.email_row(_email("sent", sent_at=T, scheduled_at=later), LEAD).when == "2026-10-12T11:02:10"
    assert sw.email_row(_email("queued", scheduled_at=later), LEAD).when == "2026-10-13T09:00:00"
    assert sw.email_row(_email("failed", status_changed_at=T), LEAD).when == "2026-10-12T11:02:10"


@pytest.mark.parametrize("status,sentiment,group", [
    ("replied", "positive", "positive"), ("replied", "negative", "not_now"), ("replied", "neutral", "question"),
    ("replied", None, "question"), ("bounced", None, "bounced"), ("sent", None, None),
])
def test_reply_groups(status, sentiment, group):
    assert sw.reply_group(_email(status, reply_sentiment=sentiment)) == group


def test_draft_detail_rights_follow_the_status():
    approved = sw.draft_detail(_draft("approved"), LEAD)
    assert (approved.can_edit, approved.can_approve, approved.can_skip, approved.send_blocker) == \
        (True, False, True, "sending_off")
    review = sw.draft_detail(_draft("needs_review"), LEAD)
    assert review.can_approve and review.sending_enabled is False
    gone = sw.draft_detail(_draft("discarded"), LEAD)
    assert not (gone.can_edit or gone.can_approve or gone.can_discard)
