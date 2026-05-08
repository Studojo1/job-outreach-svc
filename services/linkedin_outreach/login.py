"""LinkedIn email+password login with PIN challenge support."""

import logging
import re
import time
import uuid
from typing import Union

logger = logging.getLogger(__name__)

# In-memory store for pending challenge sessions (TTL: 10 min)
_pending: dict[str, dict] = {}
_CHALLENGE_TTL = 600


def _extract_cookies(session) -> tuple[str, str]:
    li_at = session.cookies.get("li_at", "")
    jsessionid = session.cookies.get("JSESSIONID", "").strip('"')
    return li_at, jsessionid


def _extract_display_name(session) -> str | None:
    try:
        res = session.get("https://www.linkedin.com/in/me/", allow_redirects=True)
        match = re.search(r"<title>([^|<]+)", res.text)
        if match:
            return match.group(1).strip()
    except Exception:
        pass
    return None


def linkedin_login_start(email: str, password: str) -> Union[
    tuple[str, str, str | None],       # (li_at, jsessionid, display_name) — success
    tuple[None, None, str],             # (None, None, session_key) — challenge required
]:
    """First step. Returns cookies on success, or a session_key if LinkedIn wants a PIN."""
    try:
        from linkedin_api.client import Client, ChallengeException, UnauthorizedException
    except ImportError:
        raise RuntimeError("linkedin-api is not installed.")

    # Create the client ourselves so we can access .session even after ChallengeException
    client = Client(debug=False)
    try:
        client.authenticate(email, password)
    except ChallengeException:
        # LinkedIn sent a PIN to the user's email — store client for step 2
        key = str(uuid.uuid4())
        _pending[key] = {"client": client, "email": email, "expires": time.time() + _CHALLENGE_TTL}
        logger.info("LinkedIn challenge triggered for %s — PIN required (key=%s)", email, key)
        return None, None, key
    except UnauthorizedException:
        raise ValueError("Invalid LinkedIn credentials. Check your email and password.")
    except Exception as e:
        raise ValueError(f"LinkedIn login failed: {e}")

    li_at, jsessionid = _extract_cookies(client.session)
    if not li_at:
        raise ValueError("Login succeeded but li_at cookie not found. Try again.")

    display_name = _extract_display_name(client.session)
    logger.info("LinkedIn login successful for %s (name=%s)", email, display_name)
    return li_at, jsessionid, display_name


def linkedin_verify_pin(session_key: str, pin: str) -> tuple[str, str, str | None]:
    """Second step — submit the PIN LinkedIn emailed and complete the challenge."""
    # Clean up expired sessions
    now = time.time()
    expired = [k for k, v in _pending.items() if v["expires"] < now]
    for k in expired:
        del _pending[k]

    entry = _pending.get(session_key)
    if not entry:
        raise ValueError("Session expired or not found. Please start the login again.")

    client = entry["client"]
    session = client.session

    # Submit the PIN to LinkedIn's challenge endpoint
    try:
        # Get CSRF token from current cookies
        csrf = session.cookies.get("JSESSIONID", "").strip('"')

        res = session.post(
            "https://www.linkedin.com/checkpoint/challenge/verify",
            data={"pin": pin.strip(), "csrfToken": csrf, "submit": "Submit"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.linkedin.com/checkpoint/challenge/verify",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                ),
            },
            allow_redirects=True,
        )

        li_at, jsessionid = _extract_cookies(session)
        if not li_at:
            raise ValueError("PIN incorrect or expired. Please try again.")

        del _pending[session_key]
        display_name = _extract_display_name(session)
        logger.info("LinkedIn PIN verified for %s (name=%s)", entry["email"], display_name)
        return li_at, jsessionid, display_name

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"PIN verification failed: {e}")
