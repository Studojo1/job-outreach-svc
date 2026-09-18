"""Generate personalised LinkedIn connection messages using Azure OpenAI."""

import logging
from typing import Optional

from openai import AsyncAzureOpenAI

logger = logging.getLogger(__name__)


async def generate_connection_message(
    person_name: str,
    person_headline: str,
    person_company: str,
    target_role: str,
    student_name: Optional[str] = None,
) -> str:
    """Generate a short, personalised LinkedIn connection note (≤300 chars).

    The message sounds like a real student reaching out — not a template.
    """
    from core.config import settings

    client = AsyncAzureOpenAI(
        api_key=settings.AZURE_OPENAI_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )

    student_line = f"The student's name is {student_name}." if student_name else "Do not include the student's name."

    prompt = f"""You are helping a student write a LinkedIn connection request note.

The student is looking for: {target_role}

They want to connect with:
- Name: {person_name}
- Headline: {person_headline or "not available"}
- Company: {person_company or "not available"}

{student_line}

Write a connection note under 300 characters that:
- Feels personal and specific to this person (reference their role/company naturally)
- Explains why the student wants to connect (learning about the field, exploring {target_role} roles)
- Does NOT sound like a template or AI-generated
- Does NOT ask for a job directly
- Is warm, brief, and genuine
- No subject line, no greeting like "Hi [Name]" — start directly with the message

Return only the message text, nothing else."""

    try:
        response = await client.chat.completions.create(
            model=settings.AZURE_OPENAI_LLM_DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=4000,  # gpt-5-mini uses reasoning tokens first; needs large budget
            temperature=0.85,
        )
        message = response.choices[0].message.content or ""
        # Trim to 300 chars hard limit
        return message.strip()[:300]
    except Exception as e:
        logger.error("Failed to generate connection message for %s: %s", person_name, e)
        # Fallback generic message
        role_hint = target_role.split("/")[0].strip()
        return (
            f"I'm exploring {role_hint} roles and came across your profile. "
            f"Would love to connect and learn from your experience."
        )[:300]


async def generate_match_reason(
    person_name: str,
    person_headline: str,
    person_company: str,
    target_role: str,
    student_summary: Optional[str] = None,
) -> str:
    """Return a one-line "why this is a good match" string for the student to see.

    Plain language, under 140 chars, no buzzwords. Helps the student decide
    whether the lead is worth connecting with.
    """
    from core.config import settings

    client = AsyncAzureOpenAI(
        api_key=settings.AZURE_OPENAI_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )

    student_line = (
        f"Student's background: {student_summary}"
        if student_summary
        else f"Student is exploring: {target_role}"
    )

    prompt = f"""You are helping a student decide whether to connect with a LinkedIn lead.

{student_line}

The lead:
- Name: {person_name}
- Headline: {person_headline or "not available"}
- Company: {person_company or "not available"}

Write a SINGLE sentence under 140 characters explaining why this person is a useful match for the student. Be specific and concrete (mention the company type / role / function). No buzzwords. No "would be a great connection". Just the why.

Return only the sentence."""

    try:
        response = await client.chat.completions.create(
            model=settings.AZURE_OPENAI_LLM_DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=4000,  # gpt-5-mini uses reasoning tokens first; needs large budget
            temperature=0.6,
        )
        text = (response.choices[0].message.content or "").strip()
        # Strip any leading "Because " or "This person " padding the LLM adds
        for prefix in ("Because ", "This person ", "They "):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):]
                text = text[0].upper() + text[1:] if text else text
        return text[:140]
    except Exception as e:
        logger.error("Failed to generate match reason for %s: %s", person_name, e)
        # Sensible fallback derived from headline
        if person_headline:
            return f"{person_headline[:100]} — relevant to {target_role}"
        return f"Works in a relevant area for {target_role}"


async def generate_followup_message(
    person_name: str,
    person_headline: str,
    person_company: str,
    target_role: str,
    student_name: Optional[str] = None,
) -> str:
    """Generate a LinkedIn DM to send ~2 minutes after a connection is accepted.

    Should feel like a real first message — not a pitch, not a template.
    Under 300 chars so it doesn't read like a wall of text.
    """
    from core.config import settings

    client = AsyncAzureOpenAI(
        api_key=settings.AZURE_OPENAI_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )

    student_line = f"The student's name is {student_name}." if student_name else "Do not include the student's name."

    prompt = f"""You are helping a student write the very first LinkedIn message after a connection request was accepted.

The student is looking for: {target_role}

They just connected with:
- Name: {person_name}
- Headline: {person_headline or "not available"}
- Company: {person_company or "not available"}

{student_line}

Write a follow-up message under 300 characters that:
- Opens by acknowledging they connected (naturally, not "Thanks for accepting my request")
- References something specific about their role or company (one concrete detail)
- Expresses genuine interest in how they work / what they've built
- Ends with ONE short, thoughtful question (about their work, not asking for a job)
- Reads like a real person wrote it — curious, warm, brief
- Does NOT sound like a template, sales pitch, or AI output
- No subject line, no "Hi [Name]" opener — start with the message body

Return only the message text, nothing else."""

    try:
        response = await client.chat.completions.create(
            model=settings.AZURE_OPENAI_LLM_DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=4000,  # gpt-5-mini uses reasoning tokens first; needs large budget
            temperature=0.85,
        )
        message = response.choices[0].message.content or ""
        return message.strip()[:300]
    except Exception as e:
        logger.error("Failed to generate followup message for %s: %s", person_name, e)
        company_hint = f" at {person_company}" if person_company else ""
        return (
            f"Great to connect! I've been following what you're building{company_hint}. "
            f"Would love to hear how you think about {target_role.split('/')[0].strip()} roles — "
            f"happy to chat whenever."
        )[:300]


async def generate_messages_for_leads(
    leads: list[dict],
    target_role: str,
    student_name: Optional[str] = None,
) -> list[dict]:
    """Add suggested_message to each lead dict. Returns the updated list."""
    import asyncio

    async def enrich(lead: dict) -> dict:
        msg = await generate_connection_message(
            person_name=lead.get("name", ""),
            person_headline=lead.get("headline", ""),
            person_company=lead.get("company", ""),
            target_role=target_role,
            student_name=student_name,
        )
        return {**lead, "suggested_message": msg}

    # Run up to 5 in parallel, stagger the rest to avoid OpenAI rate limits
    results = []
    batch_size = 5
    for i in range(0, len(leads), batch_size):
        batch = leads[i : i + batch_size]
        enriched = await asyncio.gather(*[enrich(l) for l in batch])
        results.extend(enriched)

    return results
