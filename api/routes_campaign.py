"""Campaign Routes — Campaign lifecycle, templates, and analytics."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database.session import get_db
from database.models import User, Campaign
from services.email_campaign.campaign_service import (
    create_campaign,
    transition_campaign,
    get_campaign_metrics,
)
from api.dependencies import get_current_user

router = APIRouter(prefix="/campaign", tags=["Campaign"])

# Pre-built email templates
EMAIL_TEMPLATES = [
    {
        "id": 1,
        "name": "Warm Introduction",
        "subject": "Quick intro - {name}",
        "body": "Hi {name},\n\nI came across your profile at {company} and was impressed by your work as {title}.\n\nI'm a motivated professional looking to contribute to teams like yours. I'd love to have a brief conversation about potential opportunities.\n\nWould you be open to a quick 10-minute chat this week?\n\nBest regards",
    },
    {
        "id": 2,
        "name": "Skills Highlight",
        "subject": "Skilled candidate for {company}",
        "body": "Hi {name},\n\nI noticed {company} is doing great things in the industry. As someone with hands-on experience in the field, I believe I could add value to your team.\n\nI'd appreciate the chance to discuss how my background aligns with what you're building.\n\nWould you have 10 minutes for a quick conversation?\n\nThank you",
    },
    {
        "id": 3,
        "name": "Company Interest",
        "subject": "Excited about {company}'s mission",
        "body": "Hi {name},\n\nI've been following {company}'s growth and I'm genuinely excited about your direction. Your work as {title} caught my attention.\n\nI'm exploring opportunities where I can make a meaningful impact and would love to learn more about your team's needs.\n\nCould we connect briefly this week?\n\nBest",
    },
    {
        "id": 4,
        "name": "Mutual Connection",
        "subject": "Connecting with you, {name}",
        "body": "Hi {name},\n\nI hope this message finds you well. I'm reaching out because I'm actively exploring roles in the space where {company} operates.\n\nGiven your role as {title}, I thought you might have insights into opportunities that could be a good fit.\n\nI'd be grateful for even a brief conversation. Would that work for you?\n\nWarm regards",
    },
    {
        "id": 5,
        "name": "Value Proposition",
        "subject": "How I can help {company}",
        "body": "Hi {name},\n\nI'm reaching out because I believe my skills and experience could directly benefit your team at {company}.\n\nI've been working on projects that align closely with what {company} does, and I'm eager to bring that expertise to a role where I can contribute from day one.\n\nWould you be open to a quick chat? I'd love to share more about what I can bring to the table.\n\nThank you for your time",
    },
]


class CampaignCreateRequest(BaseModel):
    candidate_id: int
    email_account_id: int
    name: str
    template_id: int = 1
    subject_template: str = ""
    body_template: str = ""
    selected_styles: list[str] = []  # Email styles for AI generation


class CampaignTransitionRequest(BaseModel):
    target_status: str


@router.get("/templates")
async def get_templates(current_user: User = Depends(get_current_user)):
    """Return pre-built email templates."""
    return {"templates": EMAIL_TEMPLATES}


@router.get("/validate")
async def validate_campaign_readiness(
    candidate_id: int,
    email_account_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pre-launch validation: check leads, Gmail, and profile are ready."""
    from database.models import Candidate, Lead, EmailAccount

    # Check candidate profile exists
    candidate = db.query(Candidate).filter_by(id=candidate_id, user_id=current_user.id).first()
    if not candidate:
        return {"valid": False, "reason": "Candidate profile not found. Complete the quiz first."}

    if not candidate.parsed_json or not candidate.parsed_json.get("career_analysis"):
        return {"valid": False, "reason": "Candidate profile incomplete. Please complete the career quiz."}

    # Check Gmail account
    account = db.query(EmailAccount).filter_by(id=email_account_id).first()
    if not account:
        return {"valid": False, "reason": "Gmail account not connected. Please connect your Gmail first."}

    if not account.access_token:
        return {"valid": False, "reason": "Gmail access token missing. Please reconnect your Gmail."}

    # Check enriched leads with verified emails
    enriched_count = db.query(Lead).filter(
        Lead.candidate_id == candidate_id,
        Lead.email.isnot(None),
        Lead.email_verified == True,
    ).count()

    if enriched_count == 0:
        return {"valid": False, "reason": "No leads with verified emails found. Run lead discovery first."}

    return {
        "valid": True,
        "enriched_leads": enriched_count,
        "email_account": account.email_address,
    }


@router.post("/create")
async def api_create_campaign(
    request: CampaignCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new outreach campaign with AI-generated or template-based emails.

    If selected_styles is provided (non-empty list), generates fully AI-personalized emails.
    Otherwise, uses legacy template substitution.
    """
    try:
        # Use AI generation if styles are selected
        if request.selected_styles:
            result = create_campaign(
                db=db,
                user_id=current_user.id,
                name=request.name,
                email_account_id=request.email_account_id,
                candidate_id=request.candidate_id,
                selected_styles=request.selected_styles,
            )
        else:
            # Fall back to template mode
            subject = request.subject_template
            body = request.body_template
            if not subject or not body:
                template = next(
                    (t for t in EMAIL_TEMPLATES if t["id"] == request.template_id),
                    EMAIL_TEMPLATES[0],
                )
                subject = subject or template["subject"]
                body = body or template["body"]

            result = create_campaign(
                db=db,
                user_id=current_user.id,
                name=request.name,
                email_account_id=request.email_account_id,
                candidate_id=request.candidate_id,
                subject_template=subject,
                body_template=body,
            )
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class EmailPreviewRequest(BaseModel):
    candidate_id: int
    selected_styles: list[str] = ["value_prop"]


@router.post("/preview-email")
async def preview_email(
    request: EmailPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a sample email preview so the user can see quality before launching."""
    from database.models import Candidate, Lead
    from services.email_campaign.email_generator_service import (
        assign_style,
        generate_email_for_lead,
    )

    candidate = db.query(Candidate).filter_by(id=request.candidate_id, user_id=current_user.id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Pick one enriched lead to generate a sample email for
    sample_lead = (
        db.query(Lead)
        .filter(Lead.candidate_id == request.candidate_id, Lead.email.isnot(None), Lead.email_verified == True)
        .first()
    )

    if not sample_lead:
        # Fall back to any lead
        sample_lead = db.query(Lead).filter(Lead.candidate_id == request.candidate_id).first()

    if not sample_lead:
        raise HTTPException(status_code=404, detail="No leads found for preview")

    try:
        style = assign_style(sample_lead, request.selected_styles)
        subject, body = generate_email_for_lead(sample_lead, candidate, style)
        return {
            "subject": subject,
            "body": body,
            "lead_name": sample_lead.name,
            "company": sample_lead.company,
            "style": style,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview generation failed: {str(e)}")


@router.post("/{campaign_id}/transition")
async def api_transition_campaign(
    campaign_id: int,
    request: CampaignTransitionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Transition a campaign to a new state."""
    try:
        result = transition_campaign(db, campaign_id, request.target_status)
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{campaign_id}/send")
async def api_start_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start sending emails for a campaign."""
    try:
        result = transition_campaign(db, campaign_id, "running")
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get campaign details."""
    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return {
        "id": campaign.id,
        "name": campaign.name,
        "status": campaign.status,
        "subject_template": campaign.subject_template,
        "body_template": campaign.body_template,
        "daily_limit": campaign.daily_limit,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
    }


@router.get("/{campaign_id}/metrics")
async def get_campaign_analytics(
    campaign_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get campaign analytics and metrics."""
    try:
        metrics = get_campaign_metrics(db, campaign_id)
        return {"status": "success", **metrics}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
