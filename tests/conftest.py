import os
import sys
import tempfile
from pathlib import Path

# Make the repo importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# core.config exits the process when a required variable is missing, which
# makes any api.* module unimportable in tests. Give it inert values; nothing
# here talks to a real database or provider.
for _k in ("DATABASE_URL", "APOLLO_API_KEY", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
           "GMAIL_REDIRECT_URI", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_KEY"):
    os.environ.setdefault(
        _k, f"sqlite:///{tempfile.gettempdir()}/outreach_tests.db" if _k == "DATABASE_URL" else "x"
    )

# Gmail tokens are encrypted with this key on every write (services/gmail_tokens.py).
# A fixed test-only key: 32 bytes of 0x07, base64. Not used anywhere real.
os.environ.setdefault("LINKEDIN_ENCRYPTION_KEY", "BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc=")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _send_window_always_open(request, monkeypatch):
    """The worker defers sends outside 9-5 in the campaign's timezone (PS-N16).
    Tests that are not about that would otherwise pass or fail by time of day,
    so they see the window as always open. Mark a test with
    @pytest.mark.real_send_window to exercise the real check."""
    if request.node.get_closest_marker("real_send_window"):
        return
    from services.email_campaign import campaign_worker
    monkeypatch.setattr(campaign_worker, "_deferred_to_send_window", lambda campaign, now: None)
