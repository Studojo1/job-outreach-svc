"""LinkedIn email+password login via Playwright browser (PIN challenge support)."""

import logging
import re
import time
import uuid

logger = logging.getLogger(__name__)

# In-memory store for pending challenge sessions (TTL: 10 min)
_pending: dict[str, dict] = {}
_CHALLENGE_TTL = 600


async def linkedin_login_start(
    email: str,
    password: str,
    proxy_url: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Login to LinkedIn with email+password using a real browser.

    Returns (li_at, jsessionid, display_name, session_key).
    On success: li_at and jsessionid are set, session_key is None.
    On challenge: li_at/jsessionid are None, session_key is set.
    Raises ValueError on bad credentials or unrecoverable error.
    """
    from services.linkedin_outreach.playwright_service import _parse_proxy

    proxy = _parse_proxy(proxy_url) if proxy_url else None

    try:
        return await _linkedin_login_attempt(email, password, proxy)
    except ValueError as e:
        if "Timeout" in str(e):
            raise ValueError("Connection timed out. The proxy may be slow — please try again in a few seconds.")
        raise


async def _linkedin_login_attempt(
    email: str,
    password: str,
    proxy,
) -> tuple[str | None, str | None, str | None, str | None]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            proxy=proxy,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled", "--window-size=1280,900",
            ],
            ignore_default_args=["--enable-automation"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = await ctx.new_page()

        try:
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(1500)

            await page.fill("#username", email)
            await page.fill("#password", password)
            await page.click('[data-litms-control-urn="login-submit"], [type="submit"]')
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(2000)

            current_url = page.url
            logger.info("Login result URL: %s", current_url)

            # Wrong password / account locked
            if "login" in current_url and "authwall" not in current_url:
                error_text = await page.evaluate(
                    "() => document.querySelector('.error-for-password, .alert-content, [role=alert]')?.innerText || ''"
                )
                raise ValueError(
                    error_text.strip() or "Invalid email or password. Please check your credentials."
                )

            # Challenge required (PIN / captcha)
            if "checkpoint" in current_url or "challenge" in current_url:
                cookies = await ctx.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}
                challenge_page = await page.content()
                if "captcha" in challenge_page.lower():
                    challenge_type = "captcha"
                else:
                    challenge_type = "pin"

                key = str(uuid.uuid4())
                _pending[key] = {
                    "cookies": cookie_dict,
                    "challenge_url": current_url,
                    "challenge_type": challenge_type,
                    "email": email,
                    "expires": time.time() + _CHALLENGE_TTL,
                }
                logger.info("LinkedIn challenge for %s — type=%s key=%s", email, challenge_type, key)
                await browser.close()
                return None, None, None, key

            # Success
            cookies = await ctx.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            li_at = cookie_dict.get("li_at", "")
            jsessionid = cookie_dict.get("JSESSIONID", "").strip('"')

            if not li_at:
                raise ValueError("Login appeared to succeed but session cookie not found. Try again.")

            display_name = await _get_display_name_from_page(page)
            logger.info("LinkedIn login OK for %s (name=%s)", email, display_name)
            await browser.close()
            return li_at, jsessionid, display_name, None

        except ValueError:
            await browser.close()
            raise
        except Exception as e:
            await browser.close()
            raise ValueError(f"LinkedIn login failed: {e}")


async def _get_display_name_from_page(page) -> str | None:
    try:
        name = await page.evaluate(
            "() => document.querySelector('.profile-nav-item__name, .t-16.t-black.t-bold')?.innerText?.trim() || ''"
        )
        if name:
            return name
        title = await page.title()
        m = re.search(r"^([^|<]+)", title or "")
        if m:
            n = m.group(1).strip()
            if n and "LinkedIn" not in n and "Sign" not in n:
                return n
    except Exception:
        pass
    return None


async def linkedin_verify_pin(session_key: str, pin: str) -> tuple[str, str, str | None]:
    """Submit the PIN LinkedIn emailed and complete the login."""
    # Clean up expired sessions
    now = time.time()
    for k in [k for k, v in _pending.items() if v["expires"] < now]:
        del _pending[k]

    entry = _pending.get(session_key)
    if not entry:
        raise ValueError("Session expired or not found. Please start the login again.")

    cookies = entry["cookies"]
    challenge_url = entry.get("challenge_url", "https://www.linkedin.com/checkpoint/challenge/verify")
    csrf = cookies.get("JSESSIONID", "").strip('"')

    import httpx
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            res = await client.post(
                "https://www.linkedin.com/checkpoint/challenge/verify",
                content=f"pin={pin.strip()}&csrfToken={csrf}&submit=Submit",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookie_header,
                    "Referer": challenge_url,
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                    ),
                },
            )

        # Extract updated cookies from response headers
        updated: dict[str, str] = dict(cookies)
        for header_val in res.headers.get_list("set-cookie"):
            m = re.match(r"([^=]+)=([^;]*)", header_val)
            if m:
                updated[m.group(1).strip()] = m.group(2).strip()

        li_at = updated.get("li_at", "")
        jsessionid = updated.get("JSESSIONID", "").strip('"')

        if not li_at:
            raise ValueError("PIN incorrect or expired. Please try again.")

        del _pending[session_key]

        # Try to get display name
        display_name = None
        try:
            cookie_header2 = "; ".join(f"{k}={v}" for k, v in updated.items())
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client2:
                r2 = await client2.get(
                    "https://www.linkedin.com/in/me/",
                    headers={"Cookie": cookie_header2, "User-Agent": "Mozilla/5.0"},
                )
            m2 = re.search(r"<title>([^|<]+)", r2.text)
            if m2:
                display_name = m2.group(1).strip()
        except Exception:
            pass

        logger.info("LinkedIn PIN verified for %s (name=%s)", entry["email"], display_name)
        return li_at, jsessionid, display_name

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"PIN verification failed: {e}")
