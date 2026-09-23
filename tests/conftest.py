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
