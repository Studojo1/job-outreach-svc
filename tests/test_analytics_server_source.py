"""ST-N09: server PostHog events are tagged source='server'.

The backend sends resume_uploaded, profile_quiz_completed, payment_confirmed,
campaign_started and coupon_applied under the browser's names, so event totals
double count unless the server copy can be told apart.
"""
from core import analytics


class FakeClient:
    def __init__(self):
        self.calls = []

    def capture(self, **kw):
        self.calls.append(kw)


def test_capture_tags_every_server_event(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(analytics, "_get_client", lambda: fake)
    analytics.capture("resume_uploaded", "u1", {"file_type": "pdf"})
    analytics.capture("campaign_started", "u1")
    assert fake.calls[0]["properties"] == {"file_type": "pdf", "source": "server"}
    assert fake.calls[1]["properties"] == {"source": "server"}


def test_tag_wins_over_a_callers_source_and_does_not_mutate_it(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(analytics, "_get_client", lambda: fake)
    props = {"source": "webhook", "plan_id": "weekly"}
    analytics.capture("payment_confirmed", "u1", props)
    assert fake.calls[0]["properties"]["source"] == "server"
    assert props["source"] == "webhook"

