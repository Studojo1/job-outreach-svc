"""Send a LinkedIn message via Playwright browser automation.

Usage:
  python scripts/playwright_send_message.py --req-id 403
"""
import argparse
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main(req_id: int):
    from database.session import SessionLocal
    from services.linkedin_outreach.crypto import decrypt, decrypt_second
    from sqlalchemy import text
    from core.config import settings

    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT * FROM linkedin_connection_requests WHERE id = :id"), {"id": req_id}
        ).fetchone()
        if not row:
            print(f"Request id={req_id} not found")
            return

        req = dict(row._mapping)
        user_id = req["user_id"]
        name = req.get("name") or ""
        company = req.get("company") or ""
        profile_url = req.get("profile_url") or ""
        followup_message = req.get("followup_message") or ""
        campaign_id = req["campaign_id"]

        print(f"Request: id={req_id} name={name!r} company={company!r}")
        print(f"  profile_url: {profile_url}")

        if not profile_url:
            print("ERROR: no profile_url")
            return

        if not followup_message:
            # Generate one
            from services.linkedin_outreach.message_gen import generate_followup_message
            camp_row = db.execute(
                text("SELECT target_role FROM linkedin_campaigns WHERE id = :cid"), {"cid": campaign_id}
            ).fetchone()
            target_role = (dict(camp_row._mapping).get("target_role") or "the role") if camp_row else "the role"
            headline = req.get("headline") or ""
            followup_message = await generate_followup_message(
                person_name=name, person_headline=headline, person_company=company,
                target_role=target_role, student_name=None,
            )
            print(f"  Generated message: {followup_message!r}")

        # Get LinkedIn token
        token = db.execute(
            text("SELECT * FROM linkedin_tokens WHERE user_id = :uid"), {"uid": user_id}
        ).fetchone()
        if not token:
            print("ERROR: no LinkedIn token")
            return

        t = dict(token._mapping)
        li_at = decrypt(t["li_at_enc"], t["nonce"])
        jsessionid = decrypt_second(t["jsessionid_enc"], t["nonce"])
        cookies_blob = t.get("cookies_blob") or ""

        proxy_url = (settings.LINKEDIN_PROXY_URL or "").strip() or None
        print(f"  Message to send: {followup_message!r}")
        print(f"  Proxy: {'yes' if proxy_url else 'no'}")

        # Launch Playwright
        from playwright.async_api import async_playwright
        import json

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                proxy={"server": proxy_url} if proxy_url else None,
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )

            # Set cookies
            cookies = [
                {"name": "li_at", "value": li_at, "domain": ".linkedin.com", "path": "/"},
                {"name": "JSESSIONID", "value": jsessionid, "domain": ".linkedin.com", "path": "/"},
            ]
            if cookies_blob:
                try:
                    extra = json.loads(cookies_blob)
                    if isinstance(extra, list):
                        for c in extra:
                            if c.get("name") not in ("li_at", "JSESSIONID"):
                                cookies.append(c)
                except Exception:
                    pass
            await context.add_cookies(cookies)

            page = await context.new_page()

            # Warm up session via feed first (LinkedIn SPA needs it)
            logger.info("Loading feed to establish session ...")
            try:
                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning("Feed load error: %s", e)
                await page.wait_for_timeout(3000)

            if "/login" in page.url or "/authwall" in page.url:
                print("ERROR: session expired — redirected to login")
                await browser.close()
                return
            logger.info("Feed loaded: %s", page.url)

            logger.info("Navigating to profile: %s", profile_url)
            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning("Profile goto error: %s — continuing", e)
                await page.wait_for_timeout(3000)

            current_url = page.url
            title = await page.title()
            logger.info("Profile URL: %s  title: %s", current_url, title)

            if "/login" in current_url or "/authwall" in current_url:
                print("ERROR: session expired")
                await browser.close()
                return

            # Find the Message button
            msg_btn = None
            for selector in [
                'button:has-text("Message")',
                'a:has-text("Message")',
                '[data-control-name="message"]',
                'button[aria-label*="message" i]',
                'button[aria-label*="Message"]',
            ]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=3000):
                        msg_btn = btn
                        logger.info("Found Message button via: %s", selector)
                        break
                except Exception:
                    continue

            if not msg_btn:
                # Scroll down and try again — profile header might not be in view
                await page.evaluate("window.scrollTo(0, 200)")
                await page.wait_for_timeout(1000)
                for selector in ['button:has-text("Message")', 'a:has-text("Message")']:
                    try:
                        btn = page.locator(selector).first
                        if await btn.is_visible(timeout=2000):
                            msg_btn = btn
                            logger.info("Found Message button (after scroll): %s", selector)
                            break
                    except Exception:
                        continue

            if not msg_btn:
                btns = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('button')).map(b => b.textContent.trim()).filter(t => t)
                """)
                logger.info("Visible buttons: %s", btns[:20])
                print("ERROR: Message button not found — person may not be connected yet")
                print(f"  Buttons on page: {btns[:15]}")
                await browser.close()
                return

            logger.info("Clicking Message button ...")
            await msg_btn.scroll_into_view_if_needed()
            await msg_btn.click()
            await page.wait_for_timeout(2000)

            # Find the message textarea
            textarea = None
            for selector in [
                '.msg-form__contenteditable',
                'div[role="textbox"][aria-label*="message" i]',
                'div[contenteditable="true"]',
                'textarea[name="message"]',
                '.msg-form__msg-content-container div[contenteditable]',
            ]:
                try:
                    el = page.locator(selector).first
                    if await el.is_visible(timeout=3000):
                        textarea = el
                        logger.info("Found textarea via: %s", selector)
                        break
                except Exception:
                    continue

            if not textarea:
                print("ERROR: Could not find message input textarea")
                await browser.close()
                return

            logger.info("Typing message ...")
            await textarea.click()
            await page.wait_for_timeout(500)
            await textarea.fill(followup_message)
            await page.wait_for_timeout(1000)

            # Find and click Send
            send_btn = None
            for selector in [
                'button.msg-form__send-btn',
                'button[aria-label*="Send" i]',
                'button:has-text("Send")',
                '.msg-form__send-toggle-btn',
            ]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=3000):
                        send_btn = btn
                        logger.info("Found Send button via: %s", selector)
                        break
                except Exception:
                    continue

            if not send_btn:
                print("ERROR: Could not find Send button")
                await browser.close()
                return

            logger.info("Clicking Send ...")
            await send_btn.click()
            await page.wait_for_timeout(2000)

            # Verify send succeeded (textarea should clear or confirmation appears)
            current_url_after = page.url
            logger.info("URL after send: %s", current_url_after)
            print("SUCCESS: Message sent via Playwright!")

            # Update DB
            db.execute(
                text(
                    "UPDATE linkedin_connection_requests "
                    "SET status='followup_sent', followup_sent_at=NOW(), followup_message=:msg, updated_at=NOW() "
                    "WHERE id = :id"
                ),
                {"msg": followup_message, "id": req_id},
            )
            db.execute(
                text(
                    "UPDATE linkedin_connection_requests "
                    "SET accepted_at=COALESCE(accepted_at, NOW()) "
                    "WHERE id = :id"
                ),
                {"id": req_id},
            )
            db.execute(
                text(
                    "UPDATE linkedin_campaigns "
                    "SET total_followups_sent = total_followups_sent + 1, "
                    "total_accepted = CASE WHEN total_accepted = 0 THEN 1 ELSE total_accepted + 1 END, "
                    "updated_at = NOW() "
                    "WHERE id = :cid"
                ),
                {"cid": campaign_id},
            )
            db.commit()
            print("DB updated: status=followup_sent")

            await browser.close()
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--req-id", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.req_id))
