"""LinkedIn email+password login via Playwright browser (PIN / phone-tap challenge support)."""

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

    # Use .start() not context manager so we can keep browser alive for phone-tap challenges
    pw = await async_playwright().start()
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

    async def _cleanup():
        try:
            await browser.close()
            await pw.stop()
        except Exception:
            pass

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
            await _cleanup()
            raise ValueError(
                error_text.strip() or "Invalid email or password. Please check your credentials."
            )

        # Challenge required
        if "checkpoint" in current_url or "challenge" in current_url:
            challenge_page = await page.content()
            challenge_lower = challenge_page.lower()

            if "captcha" in challenge_lower:
                challenge_type = "captcha"
            elif any(x in challenge_lower for x in ["another device", "sign in to", "approve", "notification"]):
                challenge_type = "phone_tap"
            else:
                challenge_type = "pin"

            key = str(uuid.uuid4())
            logger.info("LinkedIn challenge for %s — type=%s key=%s", email, challenge_type, key)

            if challenge_type == "phone_tap":
                # Keep browser alive — we'll poll for redirect after user taps Yes
                _pending[key] = {
                    "challenge_type": "phone_tap",
                    "pw": pw,
                    "browser": browser,
                    "ctx": ctx,
                    "page": page,
                    "email": email,
                    "expires": time.time() + _CHALLENGE_TTL,
                }
                return None, None, None, key
            else:
                # PIN/captcha: store cookies for httpx submit, close browser
                cookies = await ctx.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}
                _pending[key] = {
                    "challenge_type": challenge_type,
                    "cookies": cookie_dict,
                    "challenge_url": current_url,
                    "email": email,
                    "expires": time.time() + _CHALLENGE_TTL,
                }
                await _cleanup()
                return None, None, None, key

        # Success
        cookies = await ctx.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        li_at = cookie_dict.get("li_at", "")
        jsessionid = cookie_dict.get("JSESSIONID", "").strip('"')

        if not li_at:
            await _cleanup()
            raise ValueError("Login appeared to succeed but session cookie not found. Try again.")

        display_name = await _get_display_name_from_page(page)
        logger.info("LinkedIn login OK for %s (name=%s)", email, display_name)
        await _cleanup()
        return li_at, jsessionid, display_name, None

    except ValueError:
        await _cleanup()
        raise
    except Exception as e:
        await _cleanup()
        raise ValueError(f"LinkedIn login failed: {e}")


async def linkedin_check_phone_tap(session_key: str) -> tuple[str, str, str | None] | None:
    """Check if the user approved the phone tap. Returns (li_at, jsessionid, name) on success, None if still waiting."""
    now = time.time()
    for k in [k for k, v in _pending.items() if v["expires"] < now]:
        entry = _pending.pop(k)
        if entry.get("challenge_type") == "phone_tap":
            try:
                await entry["browser"].close()
                await entry["pw"].stop()
            except Exception:
                pass

    entry = _pending.get(session_key)
    if not entry:
        raise ValueError("Session expired. Please log in again.")
    if entry.get("challenge_type") != "phone_tap":
        raise ValueError("Not a phone tap session.")

    page = entry["page"]
    ctx = entry["ctx"]
    current_url = page.url
    logger.info("Phone tap check: url=%s", current_url)

    # Wait a moment for any in-flight navigation to settle
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
    current_url = page.url

    approved = (
        "/feed" in current_url
        or (
            "checkpoint" not in current_url
            and "challenge" not in current_url
            and "login" not in current_url
            and "linkedin.com" in current_url
        )
    )

    if not approved:
        return None  # still waiting

    cookies = await ctx.cookies()
    cookie_dict = {c["name"]: c["value"] for c in cookies}
    li_at = cookie_dict.get("li_at", "")
    jsessionid = cookie_dict.get("JSESSIONID", "").strip('"')

    if not li_at:
        return None  # approved but no cookie yet, let frontend retry

    display_name = await _get_display_name_from_page(page)
    logger.info("Phone tap approved for %s (name=%s)", entry["email"], display_name)

    try:
        await entry["browser"].close()
        await entry["pw"].stop()
    except Exception:
        pass
    del _pending[session_key]

    return li_at, jsessionid, display_name


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
            await client.post(
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
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client2:
            r2 = await client2.post(
                "https://www.linkedin.com/checkpoint/challenge/verify",
                content=f"pin={pin.strip()}&csrfToken={csrf}&submit=Submit",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": cookie_header,
                    "Referer": challenge_url,
                    "User-Agent": "Mozilla/5.0",
                },
            )
        for header_val in r2.headers.get_list("set-cookie"):
            m = re.match(r"([^=]+)=([^;]*)", header_val)
            if m:
                updated[m.group(1).strip()] = m.group(2).strip()

        li_at = updated.get("li_at", "")
        jsessionid = updated.get("JSESSIONID", "").strip('"')

        if not li_at:
            raise ValueError("PIN incorrect or expired. Please try again.")

        del _pending[session_key]

        display_name = None
        try:
            cookie_header2 = "; ".join(f"{k}={v}" for k, v in updated.items())
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c3:
                r3 = await c3.get(
                    "https://www.linkedin.com/in/me/",
                    headers={"Cookie": cookie_header2, "User-Agent": "Mozilla/5.0"},
                )
            m3 = re.search(r"<title>([^|<]+)", r3.text)
            if m3:
                display_name = m3.group(1).strip()
        except Exception:
            pass

        logger.info("LinkedIn PIN verified for %s (name=%s)", entry["email"], display_name)
        return li_at, jsessionid, display_name

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"PIN verification failed: {e}")
