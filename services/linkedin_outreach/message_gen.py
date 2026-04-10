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
            max_tokens=120,
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
