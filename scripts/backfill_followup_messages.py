"""Backfill follow-up messages for campaign 12 and pre-generate for sent requests.

Run once on the pod:
  python scripts/backfill_followup_messages.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.session import SessionLocal
from database.models import LinkedInCampaign, LinkedInConnectionRequest


CAMPAIGN_ID = 12
# Sentinel value that tells the daemon "follow-ups are enabled, generate AI messages"
FOLLOWUP_SENTINEL = "__auto__"


async def main():
    db = SessionLocal()
    try:
        campaign = db.query(LinkedInCampaign).filter_by(id=CAMPAIGN_ID).first()
        if not campaign:
            print(f"Campaign {CAMPAIGN_ID} not found")
            return

        # Enable follow-ups for this campaign
        if not campaign.followup_message:
            campaign.followup_message = FOLLOWUP_SENTINEL
            db.commit()
            print(f"Set campaign.followup_message = {FOLLOWUP_SENTINEL!r}")
        else:
            print(f"campaign.followup_message already set: {campaign.followup_message!r}")

        # Find sent requests with no followup_message
        sent_requests = (
            db.query(LinkedInConnectionRequest)
            .filter(
                LinkedInConnectionRequest.campaign_id == CAMPAIGN_ID,
                LinkedInConnectionRequest.status.in_(["sent", "accepted"]),
                LinkedInConnectionRequest.followup_message.is_(None),
            )
            .all()
        )

        if not sent_requests:
            print("No sent/accepted requests missing followup_message")
            return

        print(f"Generating followup messages for {len(sent_requests)} request(s)...")

        from services.linkedin_outreach.message_gen import generate_followup_message

        for req in sent_requests:
            print(f"  Generating for id={req.id} name={req.name!r} company={req.company!r} ...")
            try:
                msg = await generate_followup_message(
                    person_name=req.name or "",
                    person_headline=req.headline or "",
                    person_company=req.company or "",
                    target_role=campaign.target_role or "the role",
                    student_name=None,
                )
                req.followup_message = msg
                db.commit()
                print(f"    OK: {msg[:80]}...")
            except Exception as e:
                print(f"    FAILED: {e}")

        print("Done.")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
