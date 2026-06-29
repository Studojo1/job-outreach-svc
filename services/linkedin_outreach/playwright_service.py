"""LinkedIn automation via Playwright — runs a real Chromium browser routed through
a residential proxy with the user's cookies injected. LinkedIn sees a real browser
on a residential IP, identical to the user sitting at their computer.

This is how PhantomBuster, Expandi, and similar SaaS tools work server-side.
Strategy: click the Connect button directly and let LinkedIn's own React code
handle the invitation (authToken, CSRF, etc.) rather than extracting those values
from the DOM (LinkedIn no longer server-renders them).
"""

import asyncio
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)


def _httpx_proxy(proxy_url: str | None, session_id: str | None = None) -> dict | None:
    """Return httpx proxy dict. When session_id is set, inject Evomi sticky
    modifier into the password — keeps the user on one residential IP across
    the call chain. See automation_service.apply_sticky_session() for why.
    """
    from core.config import settings
    from services.linkedin_outreach.automation_service import apply_sticky_session
    from services.linkedin_outreach.proxy_ctx import apply_current_country
    url = (proxy_url or "").strip() or (settings.LINKEDIN_PROXY_URL or "").strip()
    if not url:
        return None
    url = apply_sticky_session(apply_current_country(url), session_id)
    # Evomi host:port:user:pass format
    if url.startswith("http://") and "@" not in url and url.count(":") >= 3:
        without_scheme = url[len("http://"):]
        parts = without_scheme.split(":")
        host, port = parts[0], parts[1]
        user, _, password = ":".join(parts[2:]).partition(":")
        return {"http://": f"http://{user}:{password}@{host}:{port}",
                "https://": f"http://{user}:{password}@{host}:{port}"}
    m = re.match(r"http://([^:]+):([^@]+)@(.+)", url)
    if m:
        return {"http://": url, "https://": url}
    return {"http://": url, "https://": url}


async def _get_fresh_jsessionid(li_at: str, proxy_url: str | None, session_id: str | None = None) -> str | None:
    """Obtain a fresh JSESSIONID for the current proxy IP via a lightweight httpx request.

    The JSESSIONID we get back is bound to whatever proxy IP this httpx call
    landed on. When session_id is passed, we hit the SAME sticky-session IP
    that downstream Playwright calls will use, so JSESSIONID and request IP
    agree. Without it, JSESSIONID is bound to one IP and the actual operation
    uses a different IP → LinkedIn rejects with redirect/999.
    """
    proxies = _httpx_proxy(proxy_url, session_id)
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
    )
    # LinkedIn profile pages can be served without a prior JSESSIONID (semi-public),
    # unlike /feed/ which requires one. We hit a simple public profile to get LinkedIn
    # to issue a fresh JSESSIONID for this proxy IP, then use it for the actual target.
    # Try several well-known slugs — the first one that returns 200 gives us the token.
    _CANARY_SLUGS = ["williamhgates", "jeffweiner08", "reidhoffman", "ariannabuffington"]

    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,  # follow to final 200 where LinkedIn sets JSESSIONID
            proxies=proxies,
        ) as client:
            for canary in _CANARY_SLUGS:
                try:
                    resp = await client.get(
                        f"https://www.linkedin.com/in/{canary}/",
                        headers={
                            "User-Agent": ua,
                            "Cookie": f"li_at={li_at}",
                            "Accept": "text/html,application/xhtml+xml",
                        },
                    )
                    # Check jar first (populated as redirects are followed)
                    jsessionid = client.cookies.get("JSESSIONID") or resp.cookies.get("JSESSIONID")
                    logger.info(
                        "_get_fresh_jsessionid: canary=%s status=%s jsessionid=%s",
                        canary, resp.status_code, repr(jsessionid),
                    )
                    if jsessionid:
                        return jsessionid
                    if resp.status_code not in (200, 301, 302, 999):
                        continue
                except Exception as e:
                    logger.debug("_get_fresh_jsessionid: canary %s failed (%s)", canary, e)
            logger.warning("_get_fresh_jsessionid: all canaries exhausted, no JSESSIONID obtained")
            return None
    except Exception as e:
        logger.warning("_get_fresh_jsessionid: httpx client failed (%s)", e)
        return None

# Limit concurrent browser instances to prevent OOM on the pod
_BROWSER_SEM = asyncio.Semaphore(3)


def _parse_proxy(proxy_url: str | None, session_id: str | None = None) -> dict | None:
    """Convert proxy URL to Playwright proxy dict. Handles standard and Evomi
    generator formats. When session_id is set, injects Evomi sticky modifier
    into the password so the browser pins to one residential IP for the call.
    """
    from core.config import settings
    from services.linkedin_outreach.automation_service import apply_sticky_session
    from services.linkedin_outreach.proxy_ctx import apply_current_country
    url = (proxy_url or "").strip() or (settings.LINKEDIN_PROXY_URL or "").strip()
    if not url:
        return None
    url = apply_sticky_session(apply_current_country(url), session_id)

    # Evomi generator emits host:port:user:pass — convert to standard http://user:pass@host:port
    if url.startswith("http://") and "@" not in url and url.count(":") >= 3:
        without_scheme = url[len("http://"):]
        parts = without_scheme.split(":")
        if len(parts) >= 3:
            host = parts[0]
            port = parts[1]
            credentials = ":".join(parts[2:])
            user, _, password = credentials.partition(":")
            return {"server": f"http://{host}:{port}", "username": user, "password": password}

    # Standard http://user:pass@host:port — Playwright wants server + username/password separate
    m = re.match(r"http://([^:]+):([^@]+)@(.+)", url)
    if m:
        return {"server": f"http://{m.group(3)}", "username": m.group(1), "password": m.group(2)}

    return {"server": url}


async def _find_connect_button(page):
    """Find the Connect button on a LinkedIn profile page.

    LinkedIn puts the button in two places:
    1. Directly in the action bar (aria-label contains 'Invite' or 'Connect')
    2. Hidden inside a More (...) dropdown (Creator Mode profiles)

    Returns the button locator, the sentinel "DROPDOWN_CONNECT_CLICKED", or None.
    """
    # Scroll to top so the profile action bar is in the viewport before we look
    # for buttons — LinkedIn auto-scrolls the page on load and the header can
    # end up above the visible area, causing force clicks to miss.
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(300)

    # Wait up to 8s for the action bar to render. Check by visible text because
    # LinkedIn's Follow/Message buttons often have no aria-label attribute.
    try:
        await page.wait_for_selector(
            'button:has-text("Follow"), button:has-text("Message"), '
            'button[aria-label="Connect"], button[aria-label*="Invite"]',
            timeout=6000,
        )
    except Exception:
        pass  # Continue anyway — page may have loaded with different structure

    # Check for a direct Connect button in the action bar
    for selector in [
        'button[aria-label*="Invite"][aria-label*="connect"]',
        'button[aria-label="Connect"]',
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=500):
                return btn
        except Exception:
            pass

    # No direct Connect — open the profile action More (...) dropdown.
    # Use evaluate_handle to find the More button atomically (avoids the race
    # condition where RSC loads shift button indices), then click the returned
    # ElementHandle with force=True (generates an isTrusted event, needed for
    # React; bypasses scroll_into_view which always times out on sticky headers).
    try:
        more_handle = await page.evaluate_handle("""
            () => {
                const isMore = b => {
                    const t = b.textContent.trim();
                    const lbl = b.getAttribute('aria-label') || '';
                    const r = b.getBoundingClientRect();
                    return b.offsetParent !== null &&
                        !b.closest('nav, header, #global-nav') &&
                        r.y > 60 &&
                        (t === 'More' || lbl === 'More' || lbl.includes('More actions'));
                };
                const allBtns = [...document.querySelectorAll('button')];
                // Anchor on the profile card Follow/Message button.
                // LinkedIn renders two Follow+More pairs: one in the sticky header (y≈3,
                // covered by the fixed nav — clicks are intercepted) and one in the profile
                // card (y>60). We must pick the profile-card one by requiring y > 60.
                const anchor = allBtns.find(b => {
                    const t = b.textContent.trim();
                    const r = b.getBoundingClientRect();
                    return (t === 'Follow' || t.startsWith('Follow') || t === 'Message') &&
                           b.offsetParent !== null &&
                           !b.closest('nav, header, #global-nav') &&
                           r.y > 60;
                });
                if (anchor) {
                    let el = anchor.parentElement;
                    for (let d = 0; d < 3 && el; d++) {
                        const btns = [...el.querySelectorAll('button')];
                        if (btns.length > 8) break;
                        const found = btns.find(isMore);
                        if (found) return found;
                        el = el.parentElement;
                    }
                }
                // Fallback: first visible non-nav More button on page
                return allBtns.find(isMore) || null;
            }
        """)
        more_elem = more_handle.as_element()
        if not more_elem:
            logger.info("Playwright: no profile More button found")
            return None

        # Scroll the profile-card More button into view via JS (Playwright's built-in
        # scroll_into_view_if_needed times out on LinkedIn sticky headers).
        await page.evaluate("el => el.scrollIntoView({behavior: 'instant', block: 'center'})", more_handle)
        await page.wait_for_timeout(400)
        logger.info("Playwright: clicking More button via JS click()")
        # Use JS .click() instead of Playwright ElementHandle.click() — avoids the
        # 5 s timeout that fires when LinkedIn re-renders and the handle goes stale.
        clicked = await page.evaluate("el => { el.click(); return true; }", more_handle)
        logger.info("Playwright: More JS click returned: %s", clicked)
        await page.wait_for_timeout(1500)

        # Log what opened to confirm the dropdown appeared
        dropdown_items = await page.evaluate("""
            () => {
                const dd = document.querySelector(
                    '[class*="dropdown__content--is-open"], '
                    + '[class*="artdeco-dropdown__content--is-open"], '
                    + '[class*="overflow-actions"]'
                );
                if (!dd) {
                    // Fallback: look for any newly-visible role=menu
                    const menu = document.querySelector('[role="menu"]:not([hidden])');
                    if (menu) return [...menu.querySelectorAll('[role="menuitem"]')]
                        .map(e => e.textContent.trim().slice(0, 40)).filter(Boolean);
                    return [];
                }
                return [...dd.querySelectorAll('[role="menuitem"], li a, li button')]
                    .map(el => el.textContent.trim().slice(0, 40)).filter(Boolean);
            }
        """)
        logger.info("Playwright: More dropdown items: %s", dropdown_items)

        vanity = page.url.rstrip('/').split('/')[-1]

        # Click the dropdown "Connect" item with a REAL Playwright click — it fires
        # a trusted event, auto-scrolls, and re-resolves the element, which manual
        # mouse-coordinate clicks do NOT (those silently failed to trigger
        # LinkedIn's React onClick). In the current UI the item is a
        # menuitem/button whose visible text is exactly "Connect" (with a
        # person-add icon), not an <a custom-invite> link. Try the most specific
        # locators first; the legacy anchor stays as a fallback.
        dd = page.locator(
            '[class*="dropdown__content--is-open"], '
            '[class*="artdeco-dropdown__content--is-open"], '
            '[role="menu"]:not([hidden])'
        ).first
        candidates = [
            dd.get_by_role("menuitem", name="Connect", exact=True),
            dd.get_by_role("button", name="Connect", exact=True),
            dd.get_by_text("Connect", exact=True),
            page.get_by_role("menuitem", name="Connect", exact=True),
            page.locator(f'a[href*="custom-invite"][href*="{vanity}"]'),
        ]
        for cand in candidates:
            try:
                t = cand.first
                if await t.count() == 0 or not await t.is_visible():
                    continue
                await t.click(timeout=5000)
                logger.info("Playwright: clicked dropdown Connect (locator) for %s", vanity)
                await page.wait_for_timeout(2500)
                return "DROPDOWN_CONNECT_CLICKED"
            except Exception as ce:
                logger.info("Playwright: dropdown Connect candidate failed (%s)", repr(ce)[:90])
                continue

        logger.info("Playwright: More dropdown opened but Connect item not found for vanity=%s", vanity)
        await page.keyboard.press("Escape")
    except Exception as e:
        logger.info("Playwright: More dropdown search error: %s", e)

    return None


async def playwright_send_invitation(
    li_at: str,
    jsessionid: str,
    profile_url: str,
    note: str = "",
    proxy_url: str | None = None,
    existing_urn: str | None = None,
    cookies_blob: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Send a LinkedIn connection request by clicking the Connect button in a headless browser.

    The browser is routed through the user's residential proxy and loaded with
    their LinkedIn session cookies. We click the Connect button directly so
    LinkedIn's own React code fires the invitation (with authToken, CSRF, etc.)
    — we never need to extract those values from the DOM ourselves.

    Returns a dict with keys: ok, profile_urn, auth_token, error.
    Raises LinkedInAuthError if the session is expired.
    """
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    from services.linkedin_outreach.automation_service import LinkedInAuthError

    slug = profile_url.rstrip("/").split("/in/")[-1].split("?")[0].split("/")[0]
    if not slug:
        return {"ok": False, "error": "Invalid profile URL"}

    proxy = _parse_proxy(proxy_url, session_id)

    # Prime the proxy IP: make an httpx request first so LinkedIn issues a fresh
    # JSESSIONID for the current proxy IP. Injecting a stale JSESSIONID (from a
    # different IP) into the browser causes LinkedIn to redirect-loop. Pass the
    # session_id so the JSESSIONID-fetch lands on the SAME sticky IP the
    # browser will use.
    fresh_jsessionid = await _get_fresh_jsessionid(li_at, proxy_url, session_id)
    effective_jsessionid = fresh_jsessionid or jsessionid
    jsessionid_val = effective_jsessionid if effective_jsessionid.startswith('"') else f'"{effective_jsessionid}"'
    logger.info("Playwright: %s — using %s JSESSIONID", slug, "fresh" if fresh_jsessionid else "stored")

    async with _BROWSER_SEM:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                proxy=proxy,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-size=1280,800",
                ],
                # Remove --enable-automation so navigator.webdriver stays undefined.
                # Without this, LinkedIn detects the headless browser and blocks
                # the Connect invite modal from opening.
                ignore_default_args=["--enable-automation"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="Asia/Calcutta",
                java_script_enabled=True,
            )

            # Inject cookies before any navigation.
            # If the extension captured the full cookie jar, inject ALL of them so
            # LinkedIn sees a complete authentic session (passes anti-fraud checks).
            injected_full_jar = False
            if cookies_blob:
                try:
                    import json as _json
                    _ss_map = {
                        "no_restriction": "None", "lax": "Lax", "strict": "Strict",
                        "none": "None", "unspecified": "Lax",
                    }
                    pw_cookies = []
                    for c in _json.loads(cookies_blob):
                        name, value = c.get("name"), c.get("value")
                        if not name or value is None:
                            continue
                        domain = c.get("domain") or ".linkedin.com"
                        ss_raw = (c.get("sameSite") or "no_restriction").lower()
                        pw_cookies.append({
                            "name": name,
                            "value": value,
                            "domain": domain,
                            "path": c.get("path") or "/",
                            "httpOnly": bool(c.get("httpOnly")),
                            "secure": bool(c.get("secure", True)),
                            "sameSite": _ss_map.get(ss_raw, "Lax"),
                        })
                    if pw_cookies:
                        await context.add_cookies(pw_cookies)
                        injected_full_jar = True
                        logger.info("Playwright: %s — injected full cookie jar (%d cookies)", slug, len(pw_cookies))
                except Exception as e:
                    logger.warning("Playwright: could not inject cookie jar: %s", e)

            if not injected_full_jar:
                # Fallback: proxy-bound fresh cookies (li_at + JSESSIONID only)
                await context.add_cookies([
                    {
                        "name": "li_at",
                        "value": li_at,
                        "domain": ".linkedin.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "None",
                    },
                ])
                if fresh_jsessionid:
                    await context.add_cookies([{
                        "name": "JSESSIONID",
                        "value": jsessionid_val,
                        "domain": "www.linkedin.com",
                        "path": "/",
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "None",
                    }])

            # Force the LinkedIn UI into English so our (English) button selectors
            # match regardless of the connected account's default language. Without
            # this, a French/other-locale account renders "Envoyer sans note" etc.
            # and every aria-label selector below silently misses.
            try:
                await context.add_cookies([{
                    "name": "lang", "value": "v=2&lang=en-us",
                    "domain": ".linkedin.com", "path": "/",
                    "secure": True, "sameSite": "None",
                }])
            except Exception:
                pass

            page = await context.new_page()

            # Intercept invitation API responses to capture success status
            invitation_result: dict = {}

            async def capture_response(response):
                url = response.url
                method = response.request.method
                # Log all POST/PUT calls to LinkedIn API so we can identify the invite endpoint
                if method in ("POST", "PUT") and "linkedin.com" in url:
                    try:
                        body = await response.text()
                        logger.info("Playwright: API %s %s → %s body=%s", method, url, response.status, body[:200])
                    except Exception:
                        pass
                is_invite_url = (
                    "normInvitations" in url
                    or ("relationships/invitations" in url and method == "POST")
                    or ("custom-invite" in url and method == "POST")
                    or ("invite" in url.lower() and method == "POST")
                )
                if is_invite_url:
                    try:
                        invitation_result["status"] = response.status
                        invitation_result["body"] = await response.text()
                        invitation_result["url"] = url
                    except Exception:
                        pass

            page.on("response", capture_response)

            try:
                # Visit /feed first so LinkedIn's JS bootstraps the session (populates
                # localStorage / Redux store). Without this, cookie-injected contexts
                # silently skip modal rendering on the profile page.
                logger.info("Playwright: bootstrapping session via /feed")
                try:
                    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
                except Exception as goto_err:
                    msg = str(goto_err)
                    # Extension-login sessions are bound to the user's home IP — when routed
                    # through our proxy, LinkedIn bounces every request to auth wall, causing
                    # ERR_TOO_MANY_REDIRECTS. Surface this as auth-failed so the UI can prompt
                    # the user to reconnect via Email & Password (which creates proxy-bound cookies).
                    if "ERR_TOO_MANY_REDIRECTS" in msg or "ERR_HTTP_RESPONSE_CODE_FAILURE" in msg:
                        # Extension cookies (home-IP bound) can't work through the
                        # proxy — genuine reconnect-required. revoked=True so the
                        # daemon marks auth_failed authoritatively.
                        raise LinkedInAuthError(
                            "LinkedIn rejected the session — please reconnect using Email & Password. "
                            "(Extension cookies don't work through our network proxy.)",
                            revoked=True,
                        )
                    raise
                feed_url = page.url
                # The /feed bootstrap is the AUTHORITATIVE liveness check: a real
                # logged-in browser loads /feed. A redirect to /login means the
                # li_at is genuinely dead (revoked=True → mark auth_failed).
                if "/login" in feed_url or "/uas/login" in feed_url:
                    raise LinkedInAuthError(
                        "LinkedIn session is no longer valid — please reconnect using Email & Password.",
                        revoked=True,
                    )
                if "/authwall" in feed_url:
                    # Authwall is often transient rate-limiting, NOT death — back off.
                    raise LinkedInAuthError("LinkedIn authwall on /feed (likely rate-limited) — will retry")
                await page.wait_for_timeout(2000)

                logger.info("Playwright: navigating to /in/%s/ proxy=%s jsessionid=%s",
                            slug, bool(proxy), "fresh" if fresh_jsessionid else "stored")
                await page.goto(
                    f"https://www.linkedin.com/in/{slug}/",
                    wait_until="domcontentloaded",
                    timeout=55000,
                )

                final_url = page.url
                if "/login" in final_url or "/uas/login" in final_url:
                    # /feed bootstrapped fine but the profile nav redirected to
                    # login → genuine death. revoked=True → authoritative.
                    raise LinkedInAuthError("Session expired — browser redirected to login", revoked=True)
                if "/authwall" in final_url:
                    logger.warning("Playwright: authwall for %s — profile may be private or rate-limited", slug)
                    return {"ok": False, "error": "LinkedIn showed authwall — profile may be private or rate-limited"}

                # Give React a minimal head start; _find_connect_button polls up to 12s more
                await page.wait_for_timeout(1500)

                title = await page.title()
                logger.info("Playwright: %s loaded — title=%r", slug, title)

                # Check if already pending — look for a Pending button in the profile
                # action bar specifically, NOT a full-page text scan (which would false-
                # positive on "2 pending invitations" in LinkedIn's nav or sidebar).
                try:
                    pending_btn = page.locator(
                        'button[aria-label*="Pending"], button:has-text("Pending")'
                    ).first
                    if await pending_btn.is_visible(timeout=1500):
                        logger.info("Playwright: %s already has pending invite (button visible)", slug)
                        return {"ok": True, "profile_urn": existing_urn, "auth_token": None, "already_pending": True}
                except Exception:
                    pass

                # Find and click the Connect button — hard 30s cap so a stuck
                # More-dropdown click can't hang the daemon indefinitely.
                try:
                    connect_btn = await asyncio.wait_for(_find_connect_button(page), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning("Playwright: %s — _find_connect_button timed out after 30s", slug)
                    connect_btn = None

                if connect_btn is None:
                    # Dump button labels for debugging
                    buttons = await page.evaluate("""() =>
                        [...document.querySelectorAll('button')].map(b => b.getAttribute('aria-label') || b.innerText.trim()).filter(Boolean).slice(0, 20)
                    """)
                    logger.warning("Playwright: %s — no Connect button found. Buttons: %s", slug, buttons)
                    return {"ok": False, "error": "Connect button not found on page", "profile_urn": existing_urn}

                # connect_btn is either a Locator or the sentinel "DROPDOWN_CONNECT_CLICKED".
                # In both cases the click may trigger navigation (the Connect <a> carries
                # a /preload/custom-invite/ href), so we wait for the Send button to
                # appear rather than using a fixed sleep.
                if connect_btn != "DROPDOWN_CONNECT_CLICKED":
                    logger.info("Playwright: %s — clicking Connect button", slug)
                    await connect_btn.click()
                else:
                    logger.info("Playwright: %s — Connect menuitem clicked, waiting for modal", slug)

                # Wait up to 20s for the invite modal's Send button to appear.
                # IMPORTANT: do NOT then sleep — the modal can race-close, so we
                # click via a live locator below as soon as it's actionable.
                try:
                    await page.wait_for_selector(
                        'button[aria-label="Send without a note"], button:has-text("Send without a note"), '
                        'button[aria-label="Add a note"], button[aria-label="Send now"], '
                        'div[role="dialog"] button:has-text("Send")',
                        timeout=20000,
                    )
                    logger.info("Playwright: %s — invite modal is open", slug)
                except Exception:
                    logger.info("Playwright: %s — modal not detected after 20s (url=%s)", slug, page.url)

                # Detect LinkedIn's BLOCKING modals before attempting Send. These
                # otherwise fall through to "Send button not found", which errors
                # every lead and keeps retrying — wasting invites and bandwidth.
                modal_state = await page.evaluate("""
                    () => {
                        const txt = (document.body.innerText || '').toLowerCase();
                        const has = (...ks) => ks.some(k => txt.includes(k));
                        if (has("you've reached the weekly invitation limit",
                                "out of invitations for now",
                                "weekly invitation limit",
                                "you're out of invitations"))
                            return 'weekly_limit';
                        if (has('reached the monthly limit', 'commercial use limit',
                                "you've reached the commercial"))
                            return 'commercial_limit';
                        const emailField = document.querySelector('input[type="email"], input[name="email"]');
                        if (emailField && emailField.offsetParent !== null &&
                            has('enter the email', 'verify this member', 'know this person',
                                'email address', "enter this person"))
                            return 'email_required';
                        return 'ok';
                    }
                """)
                if modal_state in ("weekly_limit", "commercial_limit"):
                    logger.warning("Playwright: %s — %s modal", slug, modal_state)
                    return {"ok": False, "limit_reached": True,
                            "error": f"LinkedIn {modal_state.replace('_', ' ')} reached",
                            "profile_urn": existing_urn}
                if modal_state == "email_required":
                    logger.info("Playwright: %s — invite needs recipient email; skipping lead", slug)
                    return {"ok": False, "needs_email": True,
                            "error": "LinkedIn requires the recipient's email to connect",
                            "profile_urn": existing_urn}

                # Add the personalised note when one was requested (send_with_note).
                # React-controlled textarea needs real input events, so use Playwright
                # fill() rather than JS. Any failure falls back to send-without-note.
                note_added = False
                _note = (note or "").strip()
                if _note:
                    try:
                        add_btn = page.locator(
                            'button[aria-label="Add a note"], button:has-text("Add a note")'
                        ).first
                        if await add_btn.is_visible(timeout=2500):
                            await add_btn.click()
                            await page.wait_for_timeout(800)
                            ta = page.locator(
                                'textarea#custom-message, textarea[name="message"], '
                                'div[role="dialog"] textarea'
                            ).first
                            await ta.fill(_note[:300])
                            await page.wait_for_timeout(400)
                            note_added = True
                            logger.info("Playwright: %s — added note (%d chars)", slug, len(_note[:300]))
                    except Exception as note_err:
                        logger.warning("Playwright: %s — add-note failed (%s); sending without note", slug, note_err)

                # Click Send via a LIVE Playwright locator (auto-waits for visible +
                # enabled + stable and clicks ASAP) — the old "sleep then JS re-scan"
                # raced the modal closing and missed the button entirely. Labels
                # differ: note path = "Send"/"Send invitation"; no-note = "Send
                # without a note". Try in priority order, scoped to the dialog.
                if note_added:
                    send_sels = [
                        'div[role="dialog"] button:has-text("Send invitation")',
                        'div[role="dialog"] button:has-text("Send")',
                        'button[aria-label="Send invitation"]',
                        'button[aria-label="Send now"]',
                    ]
                else:
                    send_sels = [
                        'button[aria-label="Send without a note"]',
                        'button:has-text("Send without a note")',
                        'button[aria-label="Send now"]',
                        'button:has-text("Send now")',
                        'div[role="dialog"] button:has-text("Send")',
                    ]
                send_clicked = False
                for sel in send_sels:
                    try:
                        sb = page.locator(sel).first
                        if await sb.count() == 0:
                            continue
                        await sb.click(timeout=6000)
                        send_clicked = True
                        logger.info("Playwright: %s — clicked Send via %r", slug, sel)
                        break
                    except Exception as se:
                        logger.info("Playwright: %s — Send selector %r failed (%s)", slug, sel, repr(se)[:70])
                        continue

                if not send_clicked:
                    dump = await page.evaluate("""
                        () => [...document.querySelectorAll('button')]
                          .filter(b => b.offsetParent !== null)
                          .map(b => (b.getAttribute('aria-label')||'') + '|' + (b.innerText||'').trim().slice(0,30))
                    """)
                    logger.warning("Playwright: %s — could not click Send. Visible buttons: %s", slug, dump)
                    return {"ok": False, "error": "Send button not found after clicking Connect", "profile_urn": existing_urn}

                # Wait for the invitation network request to complete
                await page.wait_for_timeout(3000)

                # Capture the refreshed cookie jar so the caller can roll the stored
                # session forward (keeps lidc/bcookie/JSESSIONID/li_at current, so the
                # session self-renews instead of drifting until a forced re-login).
                refreshed_blob = None
                try:
                    refreshed_blob = json.dumps(await context.cookies())
                except Exception:
                    refreshed_blob = None

                inv_status = invitation_result.get("status")
                inv_body = invitation_result.get("body", "")[:200]
                inv_url = invitation_result.get("url", "")

                logger.info(
                    "Playwright: %s → invitation intercepted: status=%s url=%s body=%s",
                    slug, inv_status, inv_url, inv_body,
                )

                if inv_status in (200, 201):
                    return {"ok": True, "profile_urn": existing_urn, "auth_token": None, "error": None, "cookies_blob": refreshed_blob}
                elif inv_status == 301:
                    # Already pending
                    return {"ok": True, "profile_urn": existing_urn, "auth_token": None, "already_pending": True, "cookies_blob": refreshed_blob}
                elif inv_status is not None:
                    return {"ok": False, "error": f"Invitation API returned {inv_status}: {inv_body}", "profile_urn": existing_urn}
                else:
                    # No network interception — check DOM state via JS to avoid
                    # is_visible(timeout=) which is not a valid Playwright Python kwarg.
                    await page.wait_for_timeout(1500)
                    dom_state = await page.evaluate("""
                        () => {
                            // Modal gone = invite was submitted
                            const sendBtn = document.querySelector(
                                'button[aria-label="Send without a note"]'
                            );
                            const modalGone = !sendBtn || sendBtn.offsetParent === null;
                            // Pending button in profile action bar
                            const pendingBtn = [...document.querySelectorAll('button')].find(
                                b => (b.getAttribute('aria-label') || '').toLowerCase().includes('pending')
                                  || (b.innerText || '').trim().toLowerCase() === 'pending'
                            );
                            return {
                                modalGone: modalGone,
                                hasPending: !!pendingBtn,
                            };
                        }
                    """)
                    logger.info("Playwright: %s — post-click DOM state: %s", slug, dom_state)
                    if dom_state.get("modalGone") or dom_state.get("hasPending"):
                        logger.info("Playwright: %s — invite confirmed via DOM (modal gone=%s pending=%s)",
                                    slug, dom_state.get("modalGone"), dom_state.get("hasPending"))
                        return {"ok": True, "profile_urn": existing_urn, "auth_token": None, "error": None, "cookies_blob": refreshed_blob}
                    logger.warning("Playwright: %s — send clicked but no API call intercepted and no Pending button", slug)
                    return {"ok": False, "error": "Invitation sent click fired but could not confirm delivery", "profile_urn": existing_urn}

            except LinkedInAuthError:
                raise
            except PWTimeout:
                return {"ok": False, "error": "Page load timeout", "profile_urn": existing_urn}
            except Exception as e:
                logger.error("Playwright send failed for %s: %s", slug, e)
                return {"ok": False, "error": str(e), "profile_urn": existing_urn}
            finally:
                await page.close()
                await context.close()
                await browser.close()


async def playwright_send_message(
    li_at: str,
    jsessionid: str,
    profile_url: str,
    message: str,
    proxy_url: str | None = None,
    cookies_blob: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Send a LinkedIn DM to a 1st-degree connection via Playwright.

    Navigates to the profile, finds the Message button (only visible for 1st-degree
    connections), types the message, and clicks Send.

    Returns {"ok": True} on success, {"ok": False, "error": "..."} on failure.
    "not_connected" error means the person hasn't accepted the connection request yet.
    """
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    from services.linkedin_outreach.automation_service import LinkedInAuthError

    slug = profile_url.rstrip("/").split("/in/")[-1].split("?")[0] if "/in/" in profile_url else profile_url
    proxy_cfg = _parse_proxy(proxy_url, session_id)

    # Prime the proxy IP before acquiring the browser semaphore (pure network I/O)
    fresh_js = await _get_fresh_jsessionid(li_at, proxy_url, session_id)
    active_js = fresh_js or jsessionid
    jsessionid_val = active_js if active_js.startswith('"') else f'"{active_js}"'
    logger.info("Playwright msg: %s — using %s JSESSIONID", slug, "fresh" if fresh_js else "stored")

    async with _BROWSER_SEM:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                proxy=proxy_cfg,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-size=1280,800",
                ],
                ignore_default_args=["--enable-automation"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="Asia/Calcutta",
                java_script_enabled=True,
            )

            # Inject cookies before any navigation — same pattern as playwright_send_invitation
            injected_full_jar = False
            if cookies_blob:
                try:
                    import json as _json
                    _ss_map = {
                        "no_restriction": "None", "lax": "Lax", "strict": "Strict",
                        "none": "None", "unspecified": "Lax",
                    }
                    pw_cookies = []
                    for c in _json.loads(cookies_blob):
                        name, value = c.get("name"), c.get("value")
                        if not name or value is None:
                            continue
                        domain = c.get("domain") or ".linkedin.com"
                        ss_raw = (c.get("sameSite") or "no_restriction").lower()
                        pw_cookies.append({
                            "name": name,
                            "value": value,
                            "domain": domain,
                            "path": c.get("path") or "/",
                            "httpOnly": bool(c.get("httpOnly")),
                            "secure": bool(c.get("secure", True)),
                            "sameSite": _ss_map.get(ss_raw, "Lax"),
                        })
                    if pw_cookies:
                        await context.add_cookies(pw_cookies)
                        injected_full_jar = True
                        logger.info("Playwright msg: %s — injected full cookie jar (%d cookies)", slug, len(pw_cookies))
                except Exception as e:
                    logger.warning("Playwright msg: could not inject cookie jar: %s", e)

            if not injected_full_jar:
                # Fallback: inject li_at (domain-wide) + fresh JSESSIONID (www-scoped)
                await context.add_cookies([{
                    "name": "li_at",
                    "value": li_at,
                    "domain": ".linkedin.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }])
                if fresh_js:
                    await context.add_cookies([{
                        "name": "JSESSIONID",
                        "value": jsessionid_val,
                        "domain": "www.linkedin.com",
                        "path": "/",
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "None",
                    }])

            page = await context.new_page()
            try:
                target_url = profile_url if "/in/" in profile_url else f"https://www.linkedin.com/in/{slug}/"
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(3000)
                except PWTimeout:
                    await page.wait_for_timeout(3000)

                if "/login" in page.url or "/authwall" in page.url:
                    raise LinkedInAuthError("Session expired — redirected to login")

                title = await page.title()
                logger.info("Playwright msg: %s loaded — title=%r url=%r", slug, title, page.url)

                # Look for Message button (only appears for 1st-degree connections)
                msg_btn = None
                for selector in [
                    'button:has-text("Message")',
                    'a:has-text("Message")',
                    '[data-control-name="message"]',
                    'button[aria-label*="Message"]',
                ]:
                    try:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=3000):
                            msg_btn = btn
                            logger.info("Playwright msg: found Message button via %r", selector)
                            break
                    except Exception:
                        continue

                if not msg_btn:
                    # Scroll down a bit and retry
                    await page.evaluate("window.scrollTo(0, 200)")
                    await page.wait_for_timeout(1000)
                    for selector in ['button:has-text("Message")', 'a:has-text("Message")']:
                        try:
                            btn = page.locator(selector).first
                            if await btn.is_visible(timeout=2000):
                                msg_btn = btn
                                break
                        except Exception:
                            continue

                if not msg_btn:
                    btns = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('button')).map(b => b.textContent.trim()).filter(t => t).slice(0, 15)
                    """)
                    logger.info("Playwright msg: no Message btn — buttons=%s", btns[:8])
                    return {"ok": False, "error": "not_connected", "buttons": btns[:8]}

                await msg_btn.scroll_into_view_if_needed()
                await msg_btn.click()
                await page.wait_for_timeout(2000)

                # Find the message textarea
                textarea = None
                for selector in [
                    ".msg-form__contenteditable",
                    'div[role="textbox"][contenteditable="true"]',
                    "div[contenteditable='true']",
                    ".msg-form__msg-content-container div[contenteditable]",
                ]:
                    try:
                        el = page.locator(selector).first
                        if await el.is_visible(timeout=3000):
                            textarea = el
                            break
                    except Exception:
                        continue

                if not textarea:
                    return {"ok": False, "error": "message_textarea_not_found"}

                await textarea.click()
                await page.wait_for_timeout(500)
                await textarea.fill(message)
                await page.wait_for_timeout(800)

                # Find and click Send
                send_btn = None
                for selector in [
                    "button.msg-form__send-btn",
                    'button[aria-label*="Send" i]',
                    'button:has-text("Send")',
                ]:
                    try:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=3000):
                            send_btn = btn
                            break
                    except Exception:
                        continue

                if not send_btn:
                    return {"ok": False, "error": "send_button_not_found"}

                await send_btn.click()
                await page.wait_for_timeout(1500)
                logger.info("Playwright msg: message sent to %s", slug)
                return {"ok": True}

            except LinkedInAuthError:
                raise
            except PWTimeout:
                return {"ok": False, "error": "page_timeout"}
            except Exception as e:
                logger.error("Playwright send_message failed for %s: %s", slug, e)
                return {"ok": False, "error": str(e)}
            finally:
                await page.close()
                await context.close()
                await browser.close()


async def playwright_like_recent_post(
    li_at: str,
    jsessionid: str,
    profile_url: str,
    proxy_url: str | None = None,
    cookies_blob: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Visit a profile's recent activity and like their most recent post.

    Best-effort warmup before sending a connection request — LinkedIn's activity
    feed shows that we engaged with their content, which lifts acceptance for
    active users. Skipped silently (returns {"ok": True, "skipped": True, ...})
    when:
      - No posts found on the activity page (inactive profile)
      - The latest post is already liked
      - The Like button isn't where we expect (LinkedIn DOM drift)

    Never raises — failures here must not block the actual connect step.
    """
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    from services.linkedin_outreach.automation_service import LinkedInAuthError

    slug = profile_url.rstrip("/").split("/in/")[-1].split("?")[0] if "/in/" in profile_url else profile_url
    proxy_cfg = _parse_proxy(proxy_url, session_id)

    fresh_js = await _get_fresh_jsessionid(li_at, proxy_url, session_id)
    active_js = fresh_js or jsessionid
    jsessionid_val = active_js if active_js.startswith('"') else f'"{active_js}"'

    async with _BROWSER_SEM:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                proxy=proxy_cfg,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-size=1280,800",
                ],
                ignore_default_args=["--enable-automation"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="Asia/Calcutta",
                java_script_enabled=True,
            )

            injected_full_jar = False
            if cookies_blob:
                try:
                    import json as _json
                    _ss_map = {
                        "no_restriction": "None", "lax": "Lax", "strict": "Strict",
                        "none": "None", "unspecified": "Lax",
                    }
                    pw_cookies = []
                    for c in _json.loads(cookies_blob):
                        name, value = c.get("name"), c.get("value")
                        if not name or value is None:
                            continue
                        pw_cookies.append({
                            "name": name,
                            "value": value,
                            "domain": c.get("domain") or ".linkedin.com",
                            "path": c.get("path") or "/",
                            "httpOnly": bool(c.get("httpOnly")),
                            "secure": bool(c.get("secure", True)),
                            "sameSite": _ss_map.get(
                                (c.get("sameSite") or "no_restriction").lower(), "Lax"
                            ),
                        })
                    if pw_cookies:
                        await context.add_cookies(pw_cookies)
                        injected_full_jar = True
                except Exception as e:
                    logger.warning("Playwright like-post: jar inject failed: %s", e)

            if not injected_full_jar:
                await context.add_cookies([{
                    "name": "li_at", "value": li_at, "domain": ".linkedin.com",
                    "path": "/", "httpOnly": True, "secure": True, "sameSite": "None",
                }])
                if fresh_js:
                    await context.add_cookies([{
                        "name": "JSESSIONID", "value": jsessionid_val,
                        "domain": "www.linkedin.com", "path": "/",
                        "httpOnly": False, "secure": True, "sameSite": "None",
                    }])

            page = await context.new_page()
            try:
                activity_url = f"https://www.linkedin.com/in/{slug}/recent-activity/all/"
                try:
                    await page.goto(activity_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(3000)
                except PWTimeout:
                    return {"ok": False, "skipped": True, "error": "activity_page_timeout"}

                if "/login" in page.url or "/authwall" in page.url:
                    raise LinkedInAuthError("Session expired — redirected to login (like-post)")

                # Find the first feed update on the activity page. LinkedIn's
                # selectors change frequently, so try several patterns.
                # Returns:
                #   { found: true, alreadyLiked: bool }  on success path
                #   { found: false, reason: 'no_posts' | 'no_like_button' }
                result = await page.evaluate(
                    """
                    () => {
                        // Anchor on the post container — multiple known class hooks.
                        const post = document.querySelector([
                            'div.feed-shared-update-v2',
                            'div[data-id^="urn:li:activity"]',
                            'div.scaffold-finite-scroll__content article',
                            'div.profile-creator-shared-feed-update__container',
                        ].join(', '));
                        if (!post) return { found: false, reason: 'no_posts' };

                        // Find the Like / React button inside this post.
                        // 1st-priority: explicit React Like button (aria-label contains 'Like').
                        // 2nd: any reactions trigger button.
                        const candidates = Array.from(post.querySelectorAll('button')).filter((b) => {
                            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                            const txt = (b.innerText || '').trim().toLowerCase();
                            return (
                                aria.includes('react like') ||
                                aria === 'like' ||
                                aria.startsWith('like ') ||
                                (txt === 'like' && b.querySelector('svg, use'))
                            );
                        });
                        const btn = candidates[0];
                        if (!btn) return { found: false, reason: 'no_like_button' };

                        // Detect already-liked: aria-pressed=true, or 'active' / 'reacted' class.
                        const ariaPressed = (btn.getAttribute('aria-pressed') || '').toLowerCase();
                        const cls = (btn.className || '').toLowerCase();
                        const alreadyLiked = (
                            ariaPressed === 'true' ||
                            cls.includes('react-button--active') ||
                            cls.includes('reactions-react-button--active')
                        );

                        if (alreadyLiked) return { found: true, alreadyLiked: true };

                        // Scroll into view so the click registers, then click.
                        btn.scrollIntoView({ block: 'center' });
                        btn.click();
                        return { found: true, alreadyLiked: false };
                    }
                    """,
                )

                if not result.get("found"):
                    logger.info("like-post %s: no actionable post (%s) — skipping", slug, result.get("reason"))
                    return {"ok": True, "skipped": True, "reason": result.get("reason")}

                if result.get("alreadyLiked"):
                    logger.info("like-post %s: latest post already liked — skipping", slug)
                    return {"ok": True, "skipped": True, "reason": "already_liked"}

                # Give LinkedIn's like POST a moment to land before we tear down.
                await page.wait_for_timeout(1500)
                logger.info("like-post %s: liked the latest post", slug)
                return {"ok": True, "liked": True}

            except LinkedInAuthError:
                raise
            except Exception as e:
                logger.warning("Playwright like-post failed for %s: %s", slug, e)
                return {"ok": False, "skipped": True, "error": str(e)}
            finally:
                await page.close()
                await context.close()
                await browser.close()
