"""LinkedIn email+password login — extracts li_at + JSESSIONID without browser cookies."""

import logging
import re

logger = logging.getLogger(__name__)


def linkedin_login(email: str, password: str) -> tuple[str, str, str | None]:
    """Log in to LinkedIn with email+password and return (li_at, jsessionid, display_name).

    Uses the linkedin-api library which handles the auth flow internally.
    Raises ValueError on bad credentials or challenge (CAPTCHA / email verification).
    """
    try:
        from linkedin_api import Linkedin
        from linkedin_api.client import ChallengeException, UnauthorizedException
    except ImportError:
        raise RuntimeError("linkedin-api is not installed. Run: pip install linkedin-api==2.3.1")

    try:
        api = Linkedin(email, password, debug=False)
    except ChallengeException:
        raise ValueError(
            "LinkedIn requires verification. Log in via browser first, complete the challenge, then try again."
        )
    except UnauthorizedException:
        raise ValueError("Invalid LinkedIn credentials. Check your email and password.")
    except Exception as e:
        if "CHALLENGE" in str(e).upper() or "challenge" in str(e):
            raise ValueError(
                "LinkedIn security challenge triggered. Complete it in your browser, then retry."
            )
        raise ValueError(f"LinkedIn login failed: {e}")

    # Extract cookies from the underlying requests session
    cookies = api.client.session.cookies
    li_at = cookies.get("li_at")
    jsessionid_raw = cookies.get("JSESSIONID", "")
    # Strip surrounding quotes LinkedIn sometimes adds
    jsessionid = jsessionid_raw.strip('"')

    if not li_at:
        raise ValueError("Login appeared to succeed but li_at cookie not found. Try again.")

    # Try to extract display name cheaply from the page title of /in/me redirect
    display_name: str | None = None
    try:
        res = api.client.session.get("https://www.linkedin.com/in/me/", allow_redirects=True)
        match = re.search(r"<title>([^|<]+)", res.text)
        if match:
            display_name = match.group(1).strip()
    except Exception:
        pass

    logger.info("LinkedIn login successful for %s (name=%s)", email, display_name)
    return li_at, jsessionid, display_name
