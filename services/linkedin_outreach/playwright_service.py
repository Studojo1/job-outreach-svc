"""LinkedIn automation via Playwright — runs a real Chromium browser routed through
a residential proxy with the user's cookies injected. LinkedIn sees a real browser
on a residential IP, identical to the user sitting at their computer.

This is how PhantomBuster, Expandi, and similar SaaS tools work server-side.
Strategy: click the Connect button directly and let LinkedIn's own React code
handle the invitation (authToken, CSRF, etc.) rather than extracting those values
from the DOM (LinkedIn no longer server-renders them).
"""

import asyncio
import logging
import re

import httpx

logger = logging.getLogger(__name__)


def _httpx_proxy(proxy_url: str | None) -> dict | None:
    """Return httpx proxy dict for a given proxy URL (or settings fallback)."""
    from core.config import settings
    url = (proxy_url or "").strip() or (settings.LINKEDIN_PROXY_URL or "").strip()
    if not url:
        return None
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


async def _get_fresh_jsessionid(li_at: str, proxy_url: str | None) -> str | None:
    """Obtain a fresh JSESSIONID for the current proxy IP via a lightweight httpx request.

    We hit /robots.txt (no auth needed, never triggers bot detection) and then
    the authenticated /feed/ to get LinkedIn to issue a JSESSIONID for this IP.
    The JSESSIONID is IP-bound; injecting a fresh one prevents the redirect-loop
    that occurs when a stale (different-IP) JSESSIONID is sent to a new proxy IP.
    """
    proxies = _httpx_proxy(proxy_url)
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
            follow_redirects=False,
            proxies=proxies,
            headers={
                "User-Agent": ua,
                "Cookie": f"li_at={li_at}",
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as client:
            for canary in _CANARY_SLUGS:
                try:
                    resp = await client.get(f"https://www.linkedin.com/in/{canary}/")
                    jsessionid = resp.cookies.get("JSESSIONID")
                    logger.info(
                        "_get_fresh_jsessionid: canary=%s status=%s jsessionid=%s",
                        canary, resp.status_code, repr(jsessionid),
                    )
                    if jsessionid:
                        return jsessionid
                    if resp.status_code not in (200, 301, 302):
                        continue  # blocked — try next canary
                    # For 301/302 with no JSESSIONID yet, the cookie may be in the jar
                    jsessionid = client.cookies.get("JSESSIONID")
                    if jsessionid:
                        return jsessionid
                except Exception as e:
                    logger.debug("_get_fresh_jsessionid: canary %s failed (%s)", canary, e)
            logger.warning("_get_fresh_jsessionid: all canaries exhausted, no JSESSIONID obtained")
            return None
    except Exception as e:
        logger.warning("_get_fresh_jsessionid: httpx client failed (%s)", e)
        return None

# Limit concurrent browser instances to prevent OOM on the pod
_BROWSER_SEM = asyncio.Semaphore(3)


def _parse_proxy(proxy_url: str | None) -> dict | None:
    """Convert proxy URL to Playwright proxy dict. Handles standard and Evomi generator formats."""
    from core.config import settings
    url = (proxy_url or "").strip() or (settings.LINKEDIN_PROXY_URL or "").strip()
    if not url:
        return None

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

        # Find Connect link by vanityName from the current URL.
        # LinkedIn's dropdown renders in a DOM portal so container-walk fails.
        # Scoping by vanityName avoids PYMK "Connect" links on the same page.
        # Use Playwright locator.click(force=True) — CDP-level click generates isTrusted=true
        # events which LinkedIn requires to open the invite modal.
        vanity = page.url.rstrip('/').split('/')[-1]

        # Get the full href of the Connect link so we can navigate to it directly.
        # Clicking the <a> (JS or CDP) opens a dropdown but LinkedIn's SPA click
        # handler doesn't reliably open the modal in headless contexts. Direct
        # page.goto(href) forces the SPA router to handle the invite route.
        connect_href = await page.evaluate(f"""
            () => {{
                const link = document.querySelector('a[href*="custom-invite"][href*="{vanity}"]');
                return link ? link.href : null;
            }}
        """)
        logger.info("Playwright: Connect href for %s: %s", vanity, connect_href)

        if connect_href:
            # Navigate directly to the invite URL — the SPA router opens the modal.
            # Do NOT click Send here; let the outer playwright_send_invitation handle it
            # so ok=True is only set after confirmed Send click + API interception.
            logger.info("Playwright: navigating to Connect href directly")
            await page.goto(connect_href, wait_until="domcontentloaded", timeout=20000)
            logger.info("Playwright: after goto Connect href, url=%s", page.url)
            await page.wait_for_timeout(1500)
            return "DROPDOWN_CONNECT_CLICKED"

        logger.info("Playwright: More dropdown opened but Connect link not found for vanity=%s", vanity)
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

    proxy = _parse_proxy(proxy_url)

    # Prime the proxy IP: make an httpx request first so LinkedIn issues a fresh
    # JSESSIONID for the current proxy IP. Injecting a stale JSESSIONID (from a
    # different IP) into the browser causes LinkedIn to redirect-loop.
    fresh_jsessionid = await _get_fresh_jsessionid(li_at, proxy_url)
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

            # Inject cookies before any navigation. These are proxy-bound fresh cookies
            # (obtained by logging in through this same proxy), so no identity warmup needed.
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
                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
                feed_url = page.url
                if "/login" in feed_url or "/uas/login" in feed_url:
                    raise LinkedInAuthError("Session expired — browser redirected to login on /feed")
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
                    raise LinkedInAuthError("Session expired — browser redirected to login")
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

                # Wait up to 20s for the Send button — covers both in-page modal (fast)
                # and navigation-then-modal cases (slower). After the button appears,
                # wait another 2s for LinkedIn to enable it (custom-invite page loads
                # invite context async and the button starts disabled).
                try:
                    await page.wait_for_selector(
                        'button[aria-label="Send without a note"], button:has-text("Send without a note"), '
                        'button[aria-label="Add a note"], button[aria-label="Send now"]',
                        timeout=20000,
                    )
                    logger.info("Playwright: %s — invite modal is open, waiting for button to enable", slug)
                    await page.wait_for_timeout(2500)
                except Exception:
                    logger.info("Playwright: %s — modal not detected after 20s (url=%s)", slug, page.url)

                # Dump modal state AND click in a single evaluate so LinkedIn's React SPA
                # cannot re-render the DOM between the find and the click.
                # Strictly limit to invite-specific labels — never "Send" alone which
                # is LinkedIn's messaging-overlay compose button.
                send_result = await page.evaluate("""
                    (noteText) => {
                        const all = [...document.querySelectorAll('button')];
                        const visible = b => b.offsetParent !== null;

                        // Debug: capture all visible buttons for logging
                        const dump = all.filter(visible).map(b => ({
                            label: b.getAttribute('aria-label') || '',
                            text: b.innerText.trim().slice(0, 40),
                        }));

                        if (noteText) {
                            // Click "Add a note" to open the textarea
                            const addNote = all.find(b => visible(b) && (
                                (b.getAttribute('aria-label') || '').trim() === 'Add a note' ||
                                (b.innerText || '').trim().toLowerCase() === 'add a note'
                            ));
                            if (addNote) {
                                addNote.click();
                                return { action: 'add_note_clicked', dump };
                            }
                        }

                        // Find and click the invite-specific send button.
                        // "Send without a note" and "Send now" are invite modal buttons.
                        const inviteLabels = ['Send without a note', 'Send now'];
                        for (const lbl of inviteLabels) {
                            const btn = all.find(b => visible(b) && (
                                (b.getAttribute('aria-label') || '').trim() === lbl ||
                                (b.innerText || '').trim() === lbl
                            ));
                            if (btn) {
                                btn.click();
                                return { action: 'send_clicked', button: lbl, dump };
                            }
                        }

                        return { action: 'not_found', dump };
                    }
                """, note or "")
                logger.info("Playwright: %s — send_result: action=%s dump=%s",
                            slug, send_result.get("action"), send_result.get("dump"))

                send_clicked = False
                if send_result.get("action") == "send_clicked":
                    logger.info("Playwright: %s — JS click fired for invite button: %s",
                                slug, send_result.get("button"))
                    send_clicked = True
                elif send_result.get("action") == "add_note_clicked":
                    await page.wait_for_timeout(800)
                    for ta_selector in [
                        'textarea[name="message"]',
                        'textarea.connect-button-send-invite__custom-message',
                        'textarea',
                    ]:
                        try:
                            await page.fill(ta_selector, note[:300], timeout=2000)
                            break
                        except Exception:
                            pass
                    # Now submit — "Send" aria-label is safe here (textarea is open)
                    js_send_note = await page.evaluate("""
                        () => {
                            const all = [...document.querySelectorAll('button')];
                            const btn = all.find(b => b.offsetParent !== null && (
                                (b.getAttribute('aria-label')||'').trim() === 'Send' ||
                                (b.getAttribute('aria-label')||'').trim() === 'Done'
                            ));
                            if (btn) { btn.click(); return btn.getAttribute('aria-label'); }
                            return null;
                        }
                    """)
                    if js_send_note:
                        send_clicked = True

                if not send_clicked:
                    logger.warning("Playwright: %s — could not find Send button. Dump: %s",
                                   slug, send_result.get("dump"))
                    return {"ok": False, "error": "Send button not found after clicking Connect", "profile_urn": existing_urn}

                # Wait for the invitation network request to complete
                await page.wait_for_timeout(3000)

                inv_status = invitation_result.get("status")
                inv_body = invitation_result.get("body", "")[:200]
                inv_url = invitation_result.get("url", "")

                logger.info(
                    "Playwright: %s → invitation intercepted: status=%s url=%s body=%s",
                    slug, inv_status, inv_url, inv_body,
                )

                if inv_status in (200, 201):
                    return {"ok": True, "profile_urn": existing_urn, "auth_token": None, "error": None}
                elif inv_status == 301:
                    # Already pending
                    return {"ok": True, "profile_urn": existing_urn, "auth_token": None, "already_pending": True}
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
                        return {"ok": True, "profile_urn": existing_urn, "auth_token": None, "error": None}
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
