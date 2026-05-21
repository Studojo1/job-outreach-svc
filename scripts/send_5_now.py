"""
One-shot script: resolve + send 5 LinkedIn connection requests for campaign 12.
Bypasses IST time window. Uses Evomi proxy for Voyager resolution.
"""
import asyncio
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CAMPAIGN_ID = 12
LIMIT = 5


async def main():
    from database.session import SessionLocal
    from database.models import LinkedInCampaign, LinkedInConnectionRequest
    from sqlalchemy import text
    from services.linkedin_outreach.crypto import decrypt, decrypt_second
    from services.linkedin_outreach.voyager import resolve_linkedin_url
    from services.linkedin_outreach.automation_service import (
        send_connection_request,
        resolve_profile_urn,
        _decrypt_cookies_blob,
    )
    from core.config import settings
    from datetime import datetime
    import random

    db = SessionLocal()
    try:
        # Load campaign
        campaign = db.query(LinkedInCampaign).filter(LinkedInCampaign.id == CAMPAIGN_ID).first()
        if not campaign:
            logger.error("Campaign %d not found", CAMPAIGN_ID)
            return

        logger.info("Campaign %d status=%s total_leads=%d total_sent=%d",
                    campaign.id, campaign.status, campaign.total_leads, campaign.total_sent)

        # Load token for campaign's user
        row = db.execute(text(
            "SELECT id, user_id, li_at_enc, jsessionid_enc, nonce, proxy_session_id, "
            "connection_mode, cookies_blob_enc, cookies_blob_nonce "
            "FROM linkedin_tokens WHERE user_id = :uid ORDER BY updated_at DESC LIMIT 1"
        ), {"uid": campaign.user_id}).fetchone()

        if not row:
            logger.error("No LinkedIn token for user %s", campaign.user_id)
            return

        if (row[6] or "proxy") == "extension":
            logger.error("Token is extension-mode — server-side send won't work")
            return

        try:
            li_at = decrypt(row[2], row[4])
            jsessionid = decrypt_second(row[3], row[4])
        except Exception as e:
            logger.error("Credential decryption failed: %s", e)
            return

        session_id = row[5]
        # Build a mock token_row-like object for _decrypt_cookies_blob
        class FakeRow:
            cookies_blob_enc = row[7]
            cookies_blob_nonce = row[8]
            nonce = row[4]
        cookies_blob = _decrypt_cookies_blob(FakeRow())

        proxy_url = (settings.LINKEDIN_PROXY_URL or "").strip() or None
        logger.info("Proxy: %s", proxy_url or "NONE (will use server IP)")

        # Load 5 pending requests
        pending = (
            db.query(LinkedInConnectionRequest)
            .filter(
                LinkedInConnectionRequest.campaign_id == CAMPAIGN_ID,
                LinkedInConnectionRequest.status.in_(["pending"]),
            )
            .limit(LIMIT)
            .all()
        )

        if not pending:
            logger.warning("No pending requests found for campaign %d", CAMPAIGN_ID)
            return

        logger.info("Found %d pending requests to process", len(pending))

        sent_count = 0
        for req in pending:
            logger.info("--- Processing request id=%d name=%s company=%s", req.id, req.name, req.company)

            # Step 1: Resolve profile URL via Voyager
            if not req.profile_url:
                first_name = (req.name or "").split()[0] if req.name else ""
                logger.info("  Resolving via Voyager: first_name=%s company=%s headline=%s",
                            first_name, req.company, req.headline)
                try:
                    resolved = await resolve_linkedin_url(
                        first_name, req.company or "", req.headline or "",
                        li_at, jsessionid,
                        proxy_url=proxy_url,
                    )
                except Exception as e:
                    logger.warning("  Voyager resolve error: %s", e)
                    resolved = None

                if resolved:
                    logger.info("  Resolved: %s", resolved)
                    req.profile_url = resolved
                    db.commit()
                else:
                    logger.warning("  Could not resolve profile URL — skipping")
                    req.status = "error"
                    req.error = "Could not resolve LinkedIn profile"
                    req.updated_at = datetime.utcnow()
                    db.commit()
                    continue
            else:
                logger.info("  profile_url already set: %s", req.profile_url)

            # Step 2: Resolve URN (best-effort)
            if not req.profile_urn:
                try:
                    urn = await resolve_profile_urn(
                        li_at, jsessionid, req.profile_url, session_id, cookies_blob
                    )
                    if urn:
                        logger.info("  URN resolved: %s", urn)
                        req.profile_urn = urn
                    else:
                        logger.info("  URN not resolved — Playwright fallback will be used")
                except Exception as e:
                    logger.warning("  URN resolve error: %s", e)
                    urn = None

            # Step 3: Send
            logger.info("  Sending connection request...")
            try:
                ok = await send_connection_request(
                    li_at, jsessionid,
                    req.profile_urn or "",
                    req.connection_note or "",
                    session_id,
                    profile_url=req.profile_url,
                    cookies_blob=cookies_blob,
                )
            except Exception as send_err:
                logger.warning("  Send error: %s", send_err)
                ok = False

            if ok:
                logger.info("  SUCCESS — request sent")
                req.status = "sent"
                req.sent_at = datetime.utcnow()
                campaign.total_sent = (campaign.total_sent or 0) + 1
                sent_count += 1
            else:
                logger.warning("  FAILED — marking error")
                req.status = "error"
                req.error = "Send failed"

            req.updated_at = datetime.utcnow()
            db.commit()

            # Small delay between sends
            if req != pending[-1]:
                delay = random.uniform(8, 15)
                logger.info("  Sleeping %.1fs before next send...", delay)
                await asyncio.sleep(delay)

        logger.info("=== Done: %d/%d sent successfully ===", sent_count, len(pending))

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
