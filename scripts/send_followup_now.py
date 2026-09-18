"""Send the follow-up message for a specific accepted connection request.

Use when you know someone accepted but the daemon hasn't picked it up yet.

Usage:
  python scripts/send_followup_now.py --req-id 403
"""
import argparse
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

async def main(req_id: int, force: bool = False):
    from database.session import SessionLocal
    from services.linkedin_outreach.automation_service import send_message
    from services.linkedin_outreach.message_gen import generate_followup_message
    from services.linkedin_outreach.crypto import decrypt, decrypt_second

    db = SessionLocal()
    try:
        # Import the model via the full path to avoid any stale state
        from sqlalchemy import text
        row = db.execute(
            text("SELECT * FROM linkedin_connection_requests WHERE id = :id"), {"id": req_id}
        ).fetchone()
        if not row:
            print(f"Request id={req_id} not found")
            return

        req_dict = dict(row._mapping)
        campaign_id = req_dict["campaign_id"]
        user_id = req_dict["user_id"]
        status = req_dict["status"]
        name = req_dict.get("name") or ""
        headline = req_dict.get("headline") or ""
        company = req_dict.get("company") or ""
        profile_urn = req_dict.get("profile_urn") or ""
        followup_message = req_dict.get("followup_message") or ""

        print(f"Request: id={req_id} name={name!r} company={company!r} status={status!r}")
        print(f"  profile_urn: {profile_urn}")
        print(f"  followup_message: {followup_message[:80]!r}" if followup_message else "  followup_message: (none)")

        if not profile_urn:
            print("ERROR: no profile_urn — can't send message without URN")
            return

        # Get LinkedIn token
        token = db.execute(
            text("SELECT * FROM linkedin_tokens WHERE user_id = :uid"), {"uid": user_id}
        ).fetchone()
        if not token:
            print("ERROR: no LinkedIn token found for this user")
            return

        token_dict = dict(token._mapping)
        li_at = decrypt(token_dict["li_at_enc"], token_dict["nonce"])
        jsessionid = decrypt_second(token_dict["jsessionid_enc"], token_dict["nonce"])

        # Check if actually accepted (optional, skip if --force)
        if not force:
            print("Checking connection status via Voyager ...")
            from core.config import settings
            import httpx

            def _headers(li_at, jsessionid):
                raw_csrf = jsessionid.strip('"')
                return {
                    "Cookie": f"li_at={li_at}; JSESSIONID={jsessionid}",
                    "Csrf-Token": raw_csrf,
                    "X-Li-Lang": "en_US",
                    "X-RestLi-Protocol-Version": "2.0.0",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                }

            proxy_url = (settings.LINKEDIN_PROXY_URL or "").strip() or None
            proxy_kwargs = {"proxy": proxy_url} if proxy_url else {}
            async with httpx.AsyncClient(timeout=15, **proxy_kwargs) as client:
                res = await client.get(
                    f"https://www.linkedin.com/voyager/api/identity/profiles/{profile_urn}",
                    headers=_headers(li_at, jsessionid),
                )
            print(f"  Profile endpoint status: {res.status_code}")
            if res.status_code == 200:
                data = res.json()
                dist = (data.get("distance") or {}).get("value") or ""
                print(f"  Distance: {dist!r}")
                if dist == "DISTANCE_1":
                    print("  -> ACCEPTED (1st degree)")
                elif dist:
                    print(f"  -> Not yet accepted (distance={dist})")
                    if not force:
                        print("  Use --force to send anyway")
                        return
            else:
                print(f"  Profile check failed: {res.status_code}. Use --force to skip check.")
                if not force:
                    return

        # Generate followup message if not already set
        if not followup_message:
            print("Generating followup message via Azure OpenAI ...")
            campaign_row = db.execute(
                text("SELECT target_role FROM linkedin_campaigns WHERE id = :cid"), {"cid": campaign_id}
            ).fetchone()
            target_role = (dict(campaign_row._mapping).get("target_role") or "the role") if campaign_row else "the role"
            followup_message = await generate_followup_message(
                person_name=name,
                person_headline=headline,
                person_company=company,
                target_role=target_role,
                student_name=None,
            )
            print(f"  Generated: {followup_message!r}")
            db.execute(
                text("UPDATE linkedin_connection_requests SET followup_message = :msg WHERE id = :id"),
                {"msg": followup_message, "id": req_id},
            )
            db.commit()

        print(f"Sending follow-up to {name} ({profile_urn}) ...")
        print(f"  Message: {followup_message!r}")

        import httpx
        from core.config import settings as _s

        proxy_url = (_s.LINKEDIN_PROXY_URL or "").strip() or None
        proxy_kwargs = {"proxy": proxy_url} if proxy_url else {}
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

        # Refresh JSESSIONID — stale JSESSIONID causes CSRF 400
        print("Refreshing JSESSIONID ...")
        fresh_js = jsessionid
        for canary in ("williamhgates", "jeffweiner08", "reidhoffman"):
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True, **proxy_kwargs) as c:
                    seed = await c.get(
                        f"https://www.linkedin.com/in/{canary}/",
                        headers={"Cookie": f"li_at={li_at}", "User-Agent": ua, "Accept": "text/html"},
                    )
                    fresh_js = seed.cookies.get("JSESSIONID") or c.cookies.get("JSESSIONID") or fresh_js
                    if fresh_js and fresh_js != jsessionid:
                        print(f"  Got fresh JSESSIONID from {canary}: {fresh_js[:30]}...")
                        break
            except Exception as seed_err:
                print(f"  Seed {canary} failed: {seed_err}")
                continue

        def _h(la, js):
            raw_csrf = js.strip('"')
            return {
                "Cookie": f"li_at={la}; JSESSIONID={js}",
                "Csrf-Token": raw_csrf,
                "X-Li-Lang": "en_US",
                "X-RestLi-Protocol-Version": "2.0.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": ua,
            }

        payload = {
            "keyVersion": "LEGACY_INBOX",
            "conversationCreate": {
                "eventCreate": {
                    "value": {
                        "com.linkedin.voyager.messaging.create.MessageCreate": {
                            "attributedBody": {"text": followup_message, "attributes": []},
                            "attachments": [],
                        }
                    }
                },
                "recipients": [f"urn:li:fsd_profile:{profile_urn}"],
                "subtype": "MEMBER_TO_MEMBER",
            },
        }
        async with httpx.AsyncClient(timeout=20, **proxy_kwargs) as client:
            msg_res = await client.post(
                "https://www.linkedin.com/voyager/api/messaging/conversations",
                headers=_h(li_at, fresh_js),
                json=payload,
            )
        print(f"  Messaging API status: {msg_res.status_code}")
        try:
            print(f"  Messaging API body: {msg_res.json()}")
        except Exception:
            print(f"  Messaging API body: {msg_res.text[:500]}")

        ok = msg_res.status_code in (200, 201)
        print(f"  send_message result: {ok}")

        if ok:
            db.execute(
                text(
                    "UPDATE linkedin_connection_requests "
                    "SET status='followup_sent', followup_sent_at=NOW(), updated_at=NOW() "
                    "WHERE id = :id"
                ),
                {"id": req_id},
            )
            db.execute(
                text(
                    "UPDATE linkedin_campaigns SET total_followups_sent = total_followups_sent + 1, updated_at=NOW() "
                    "WHERE id = :cid"
                ),
                {"cid": campaign_id},
            )
            # Also mark accepted if not already
            if status != "accepted":
                db.execute(
                    text(
                        "UPDATE linkedin_connection_requests "
                        "SET status='followup_sent', accepted_at=NOW() "
                        "WHERE id = :id AND status != 'followup_sent'"
                    ),
                    {"id": req_id},
                )
                db.execute(
                    text(
                        "UPDATE linkedin_campaigns SET total_accepted = total_accepted + 1, updated_at=NOW() "
                        "WHERE id = :cid"
                    ),
                    {"cid": campaign_id},
                )
            db.commit()
            print("SUCCESS: follow-up sent and DB updated")
        else:
            print("FAILED: send_message returned False (they may not be connected yet, or session expired)")

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--req-id", type=int, required=True, help="linkedin_connection_requests.id")
    parser.add_argument("--force", action="store_true", help="Skip acceptance check and send anyway")
    args = parser.parse_args()
    asyncio.run(main(args.req_id, args.force))
