"""Rescue emails that failed due to Apollo credit exhaustion.

Resets emails stuck in failed/skipped enrichment back to pending so the
campaign worker picks them up on the next cycle. Also shifts scheduled_at
forward for emails whose send window has already passed.

Run once against prod and staging after adding the new Apollo API key:
    python -m scripts.rescue_failed_emails
"""

import logging
from datetime import datetime, timedelta

from database.session import SessionLocal
from database.models import EmailSent, Campaign

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rescue")


def rescue_failed_emails() -> None:
    db = SessionLocal()
    try:
        now = datetime.utcnow()

        # Find all emails that failed enrichment while the campaign is still running
        failed = (
            db.query(EmailSent)
            .join(Campaign, Campaign.id == EmailSent.campaign_id)
            .filter(
                Campaign.status == "running",
                EmailSent.enrichment_status.in_(["skipped", "failed"]),
                EmailSent.status == "failed",
                EmailSent.sent_at.is_(None),  # not actually sent
            )
            .all()
        )

        logger.info("Found %d failed emails to rescue", len(failed))
        if not failed:
            logger.info("Nothing to do.")
            return

        # Group by campaign to recompute scheduling gaps
        campaigns: dict[int, list[EmailSent]] = {}
        for email in failed:
            campaigns.setdefault(email.campaign_id, []).append(email)

        total_rescheduled = 0
        for campaign_id, emails in campaigns.items():
            # Sort by original scheduled_at so we preserve relative order
            emails.sort(key=lambda e: e.scheduled_at or now)

            # Find the last successfully sent/queued email in this campaign
            # to anchor the reschedule window after it.
            last_sent = (
                db.query(EmailSent)
                .filter(
                    EmailSent.campaign_id == campaign_id,
                    EmailSent.status.in_(["sent", "queued", "pending_enrichment"]),
                    EmailSent.scheduled_at.isnot(None),
                )
                .order_by(EmailSent.scheduled_at.desc())
                .first()
            )

            # Start rescheduling 30 minutes from now, or after the last good email
            anchor = max(
                now + timedelta(minutes=30),
                (last_sent.scheduled_at if last_sent and last_sent.scheduled_at else now) + timedelta(hours=1),
            )

            # Space rescued emails 45 minutes apart (safe daily limit spacing)
            gap = timedelta(minutes=45)
            cursor = anchor

            for email in emails:
                # If original scheduled_at is still in the future, keep it;
                # only push forward emails whose window has already passed.
                if email.scheduled_at and email.scheduled_at > now + timedelta(minutes=5):
                    cursor = email.scheduled_at
                else:
                    email.scheduled_at = cursor
                    cursor += gap

                email.enrichment_status = "pending"
                email.status = "pending_enrichment"
                email.error_message = None
                total_rescheduled += 1

            logger.info(
                "Campaign %d: rescheduled %d emails, first slot at %s",
                campaign_id, len(emails), anchor.isoformat(),
            )

        db.commit()
        logger.info("Done. %d emails rescued and rescheduled.", total_rescheduled)

    except Exception as e:
        db.rollback()
        logger.error("Rescue failed: %s", e)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    rescue_failed_emails()
