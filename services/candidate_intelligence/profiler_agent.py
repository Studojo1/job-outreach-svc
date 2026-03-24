"""
CandidateProfiler — Profiling Agent (Direct JSON Mode)
Uses Azure OpenAI with JSON response format (NOT instructor/tool-calling).
This is dramatically faster with reasoning models like gpt-5-mini.
"""

import os
import json
import logging
import time
from openai import AzureOpenAI
from dotenv import load_dotenv
from services.candidate_intelligence.models import AgentResponse, MCQQuestion, MCQOption, ChatMessage

load_dotenv()
logger = logging.getLogger(__name__)

# ============================================================================
# Azure OpenAI Client (direct, no instructor)
# ============================================================================

_client = None


def _get_client():
    global _client
    if _client is None:
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        api_key = os.getenv("AZURE_OPENAI_KEY")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

        logger.info(f"Initializing Azure OpenAI client: endpoint={endpoint}, api_version={api_version}, key_set={bool(api_key)}")

        if not endpoint or not api_key:
            raise ValueError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_KEY must be set")

        _client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
        logger.info("Azure OpenAI client initialized successfully")
    return _client


def _get_model():
    model = os.getenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-5-mini")
    logger.info(f"Using Azure OpenAI model: {model}")
    return model


# ============================================================================
# Career Ontology
# ============================================================================


def get_ontology_as_text() -> str:
    try:
        from services.candidate_intelligence.career_ontology import CAREER_ONTOLOGY
        lines = []
        for cluster_name, specializations in CAREER_ONTOLOGY.items():
            lines.append(f"\n### {cluster_name}")
            for spec_name, roles in specializations.items():
                lines.append(f"  {spec_name}: {', '.join(roles[:3])}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Could not load career ontology: {e}")
        return "(Career ontology not available)"


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

SYSTEM_PROMPT = """You are **StudojoProfiler**, a career profiling chatbot. Your ONLY job is to understand the candidate's background, preferences, and career goals in 7 questions, then end the conversation.

## YOUR MISSION
Collect enough information to generate a structured career profile payload. You are NOT a career counselor, mentor, or advisor. Do NOT give tips, templates, comparisons, career advice, or do anything beyond asking profiling questions.

## YOUR PERSONALITY
- Warm and concise. 1 short sentence to acknowledge, then the next question.
- NEVER use em dashes. Use commas or periods instead.
- Use emojis sparingly (max 1 per message, or none).

## MESSAGE FORMAT
- Use `|||` to separate acknowledgment from the next question. Example:
  "Great, Bengaluru it is.|||What type of company do you prefer?"
- NEVER combine both in one block without `|||`.

## QUESTION PLAN (exactly 8 questions, adapt wording to context)

### Q1: Stage (hardcoded by system, skip if already answered)
"Which of these best describes you right now?" (student/grad/experienced/switching)

### Q2: Job type (if student/grad)
"Are you looking for an internship or full-time role?" OR skip if experienced.

### Q3: Location preferences
"Which cities/regions would you prefer to work in?" (multi-select, include Remote)

### Q4: Company stage and size
"What type of company do you want to join?" (startup/growth/enterprise, multi-select)

### Q5: Industry interests
"Which industries excite you most?" (multi-select, 5-7 options based on resume/context)

### Q6: Role focus / what they enjoy
"What kind of work do you enjoy most?" (building product, analyzing data, managing people, etc.)

### Q7: Skills to use or grow
"Which skills do you want to use or develop?" (multi-select based on resume + context)

After Q7: The user will answer Q7. ONLY AFTER you receive the user's Q7 answer, respond with is_complete: true. Do NOT combine Q7's question with the completion message. Your completion response (after receiving Q7 answer) should be:
- Set `is_complete: true`, `mcq: null`, `text_input: false`
- Message: acknowledge their answer briefly, then "Generating your report now..."
- `questions_asked_so_far: 7`

## DYNAMIC MCQ RULES
- Generate options RELEVANT to the candidate (Indian cities for Indian candidates, etc.)
- Every MCQ MUST end with "Other" as the last option.
- Multi-select questions: set `allow_multiple: true`.
- Do NOT ask about salary or CTC. Skip salary questions entirely.

## CAREER ONTOLOGY (reference for recommended roles):
{career_ontology}

## FORBIDDEN (NEVER DO THESE)
- Do NOT give career advice, tips, or guidance.
- Do NOT compare roles, explain day-to-day tasks, or create templates.
- Do NOT go beyond 7 questions. After the user answers Q7, you MUST set is_complete: true.
- NEVER combine a question with the completion message. Ask the question, wait for the answer, THEN complete.
- Do NOT ask clarifying follow-ups.
- Do NOT spiral into sub-questions.
- Do NOT promise to "prepare" or "provide" anything.
- Do NOT repeat yourself or paraphrase the user's answer back at length.

## RESPONSE FORMAT
You MUST respond with a JSON object matching this exact schema:
{{
  "message": "Your text message here (use ||| to separate acknowledgment from question)",
  "current_state": "MCQ" or "PAYLOAD_READY",
  "mcq": {{
    "question": "The question text",
    "options": [
      {{"label": "A", "text": "Option text"}},
      {{"label": "B", "text": "Option text"}},
      {{"label": "C", "text": "Other"}}
    ],
    "allow_multiple": false
  }},
  "text_input": false,
  "is_complete": false,
  "questions_asked_so_far": 2
}}

For salary questions, set mcq to null and text_input to true.
When is_complete is true, set mcq to null and text_input to false.
"""


# ============================================================================
# Message Builder
# ============================================================================

def build_messages(
    chat_history: list[ChatMessage],
    resume_summary: dict | None = None,
    resume_raw_text: str | None = None,
) -> list[dict]:
    """Build the message list for the LLM call."""
    system_content = SYSTEM_PROMPT.format(career_ontology=get_ontology_as_text())

    # Add resume context (raw text, since we skip LLM summarization)
    if resume_raw_text:
        truncated = resume_raw_text[:4000]
        system_content += f"\n\n## CANDIDATE'S RESUME (raw text):\n{truncated}\n"

        if resume_summary and isinstance(resume_summary, dict):
            if resume_summary.get("name"):
                system_content += f"\nDetected name: {resume_summary['name']}"
            if resume_summary.get("email"):
                system_content += f"\nDetected email: {resume_summary['email']}"
            if resume_summary.get("skills"):
                system_content += f"\nDetected skills: {', '.join(resume_summary['skills'])}"

    messages = [
        {"role": "system", "content": system_content},
    ]

    if not chat_history:
        messages.append({"role": "user", "content": "Start the profiling session."})
    else:
        for msg in chat_history:
            messages.append({"role": msg.role, "content": msg.content})

    return messages


# ============================================================================
# Parse JSON Response into AgentResponse
# ============================================================================

def _try_extract_mcq_from_message(message: str) -> MCQQuestion | None:
    """Attempt to extract MCQ options from plain-text message when LLM forgot to populate the mcq field."""
    import re
    # Pattern: "A) option text" or "A. option text" or "- option text"
    patterns = [
        r'([A-H])\)\s*(.+?)(?=\n[A-H]\)|$)',  # A) Option text
        r'([A-H])\.\s*(.+?)(?=\n[A-H]\.|$)',   # A. Option text
    ]
    for pattern in patterns:
        matches = re.findall(pattern, message, re.MULTILINE)
        if len(matches) >= 2:
            options = [MCQOption(label=m[0].strip(), text=m[1].strip()) for m in matches]
            # Extract question (text before first option)
            first_opt = re.search(r'[A-H][).]', message)
            question = message[:first_opt.start()].strip() if first_opt else ""
            # Remove trailing colon or question mark artifacts
            question = question.rstrip(':').strip()
            allow_multiple = any(kw in message.lower() for kw in ['select multiple', 'choose all', 'multi-select', 'select all'])
            logger.info(f"[QUIZ] Extracted {len(options)} options from plain-text message")
            return MCQQuestion(question=question, options=options, allow_multiple=allow_multiple)
    return None


def _parse_llm_json(raw_text: str, chat_history: list[ChatMessage]) -> AgentResponse:
    """Parse the LLM's JSON string into an AgentResponse model."""
    logger.info(f"[DEBUG] Raw LLM response: {raw_text}")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        # Try to extract JSON from markdown code blocks
        import re
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_text, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
        else:
            logger.error(f"[QUIZ] Could not parse LLM response as JSON: {raw_text[:300]}")
            return AgentResponse(
                message="Could you repeat that? I had trouble processing.",
                current_state="MCQ",
                questions_asked_so_far=len([m for m in chat_history if m.role == "assistant"]),
            )

    # Build MCQ if present
    mcq = None
    if data.get("mcq"):
        mcq_data = data["mcq"]
        options = [MCQOption(label=o.get("label", chr(65 + i)), text=o["text"])
                   for i, o in enumerate(mcq_data.get("options", []))]
        if options:
            mcq = MCQQuestion(
                question=mcq_data.get("question", ""),
                options=options,
                allow_multiple=mcq_data.get("allow_multiple", False),
            )
            logger.info(f"[QUIZ] Parsed MCQ with {len(options)} options, allow_multiple={mcq.allow_multiple}")
        else:
            logger.warning("[QUIZ] mcq field present but options array is empty")

    is_complete = data.get("is_complete", False)
    text_input = data.get("text_input", False)
    current_state = data.get("current_state", "MCQ")
    message = data.get("message", "")
    questions_asked = data.get("questions_asked_so_far", 0)

    # Validation: If state is MCQ but no mcq and no text_input, try to extract options from message
    if current_state == "MCQ" and mcq is None and not text_input and not is_complete:
        extracted = _try_extract_mcq_from_message(message)
        if extracted:
            mcq = extracted
            # Clean the options text out of the message (keep just the question/acknowledgment)
            logger.warning(f"[QUIZ] LLM put options in message text instead of mcq field — auto-extracted {len(extracted.options)} options")
        else:
            logger.warning(f"[QUIZ] MCQ state but no structured options and could not extract from message. Message: {message[:200]}")

    # Validation: Don't allow is_complete before enough questions are asked
    if is_complete and questions_asked < 7:
        logger.warning(f"[QUIZ] LLM tried to complete after only {questions_asked} questions (need 7) — forcing continuation")
        is_complete = False
        current_state = "MCQ"

    logger.info(f"[QUIZ] Response: state={current_state}, has_mcq={mcq is not None}, "
                f"text_input={text_input}, is_complete={is_complete}, q_count={questions_asked}")

    return AgentResponse(
        message=message,
        current_state=current_state,
        mcq=mcq,
        text_input=text_input,
        is_complete=is_complete,
        questions_asked_so_far=questions_asked,
    )


# ============================================================================
# Agent Response (Direct JSON mode - NO instructor)
# ============================================================================

def get_agent_response(
    chat_history: list[ChatMessage],
    resume_summary: dict | None = None,
    resume_raw_text: str | None = None,
) -> AgentResponse:
    """
    Get the next agent response using direct JSON mode.
    Much faster than instructor's tool-calling for reasoning models.
    """
    t_start = time.perf_counter()
    client = _get_client()
    model = _get_model()
    messages = build_messages(chat_history, resume_summary, resume_raw_text)
    t_prep = time.perf_counter()
    logger.info(f"[TIMING] Message prep: {(t_prep - t_start)*1000:.0f}ms, history_len={len(chat_history)}")

    max_retries = 3
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            t_llm_start = time.perf_counter()
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=2000,
            )
            t_llm_end = time.perf_counter()
            raw = completion.choices[0].message.content
            logger.info(f"[TIMING] LLM call (attempt {attempt}): {(t_llm_end - t_llm_start)*1000:.0f}ms, response_len={len(raw) if raw else 0}")

            if not raw:
                logger.error("LLM returned empty response")
                return AgentResponse(
                    message="Could you repeat that? I had trouble processing.",
                    current_state="MCQ",
                    questions_asked_so_far=len([m for m in chat_history if m.role == "assistant"]),
                )

            response = _parse_llm_json(raw, chat_history)
            t_total = time.perf_counter()
            logger.info(f"[TIMING] Total get_agent_response: {(t_total - t_start)*1000:.0f}ms | "
                        f"state={response.current_state}, has_mcq={response.mcq is not None}, "
                        f"complete={response.is_complete}")
            return response

        except Exception as e:
            last_error = e
            logger.warning(f"[QUIZ] LLM attempt {attempt}/{max_retries} failed: {type(e).__name__}: {e}")
            if attempt < max_retries:
                time.sleep(1.5 * attempt)  # backoff: 1.5s, 3s
            continue

    logger.error(f"Agent error after {max_retries} attempts, {(time.perf_counter() - t_start)*1000:.0f}ms: {last_error}", exc_info=True)
    return AgentResponse(
        message="I hit a snag processing that. Could you try again?",
        current_state="MCQ",
        questions_asked_so_far=len([m for m in chat_history if m.role == "assistant"]),
    )


# ============================================================================
# Final Payload Generation
# ============================================================================

PAYLOAD_PROMPT = """You are a career analysis AI. Read the entire conversation and extract ALL relevant information into a structured JSON payload.

## INSTRUCTIONS
- Fill in ALL fields based on what was discussed.
- For salary, use numbers mentioned or estimate based on role and seniority.
- Recommend 3-5 roles with fit scores (0-1) and reasoning.
- If information was not discussed (e.g. name, email), use null or empty values.
- profile_summary should be a concise 2-3 sentence overview.

## REQUIRED JSON SCHEMA
Return a JSON object with these fields:
{
  "candidate_id": "auto-generated UUID",
  "timestamp": "ISO datetime string",
  "profile_summary": "2-3 sentence overview",
  "personal_info": {
    "name": "string or null",
    "email": "string or null",
    "education": [],
    "skills_detected": ["skill1", "skill2"]
  },
  "preferences": {
    "locations": ["city1"],
    "work_mode": "remote|hybrid|onsite|flexible",
    "company_size": "string",
    "company_stage": "string",
    "industry_interests": ["industry1"],
    "salary_expectations": {"min_annual_ctc": 0, "max_annual_ctc": 0, "currency": "INR"},
    "risk_tolerance": "high|medium|low",
    "timeline": "string"
  },
  "career_analysis": {
    "primary_cluster": "string",
    "secondary_cluster": "string or null",
    "specializations": [{"name": "string", "fit_score": 0.8, "reasoning": "string"}],
    "recommended_roles": [{"title": "string", "seniority": "intern|entry|junior|mid", "fit_score": 0.8, "salary_alignment": true, "reasoning": "string"}],
    "transition_paths": ["path1"]
  },
  "session_metadata": {
    "resume_uploaded": false,
    "questions_answered": 7,
    "confidence_score": 0.85
  }
}
"""


def generate_final_payload(
    chat_history: list[ChatMessage],
    resume_summary: dict | None = None,
    resume_raw_text: str | None = None,
    resume_uploaded: bool = False,
) -> "CandidatePayload":
    """
    Generate the final candidate profile payload using direct JSON mode.
    """
    from services.candidate_intelligence.models import CandidatePayload
    import uuid
    from datetime import datetime

    client = _get_client()
    model = _get_model()

    transcript = ""
    for msg in chat_history:
        role_label = "Counselor" if msg.role == "assistant" else "Candidate"
        transcript += f"\n{role_label}: {msg.content}\n"

    resume_context = ""
    if resume_raw_text:
        resume_context = f"\n\n## CANDIDATE'S RESUME:\n{resume_raw_text[:4000]}\n"
    if resume_summary and isinstance(resume_summary, dict):
        if resume_summary.get("name"):
            resume_context += f"\nName: {resume_summary['name']}"
        if resume_summary.get("email"):
            resume_context += f"\nEmail: {resume_summary['email']}"
        if resume_summary.get("skills"):
            resume_context += f"\nSkills: {', '.join(resume_summary['skills'])}"

    messages = [
        {"role": "system", "content": PAYLOAD_PROMPT + resume_context},
        {"role": "user", "content": f"Conversation transcript:\n{transcript}\n\nGenerate the JSON payload."},
    ]

    max_retries = 3
    raw = None
    for attempt in range(1, max_retries + 1):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=4000,
            )
            raw = completion.choices[0].message.content
            logger.info(f"Payload raw response length (attempt {attempt}): {len(raw) if raw else 0} chars")
            break
        except Exception as e:
            logger.warning(f"[PAYLOAD] LLM attempt {attempt}/{max_retries} failed: {type(e).__name__}: {e}")
            if attempt < max_retries:
                time.sleep(2 * attempt)
            else:
                raise

    try:

        if not raw:
            raise ValueError("LLM returned empty payload response")

        data = json.loads(raw)

        # Ensure required fields have defaults
        data.setdefault("candidate_id", str(uuid.uuid4()))
        data.setdefault("timestamp", datetime.now().isoformat())
        data.setdefault("profile_summary", "Profile generated from conversation.")
        data.setdefault("personal_info", {})
        data.setdefault("preferences", {})
        data.setdefault("career_analysis", {})
        data.setdefault("session_metadata", {})

        # Ensure nested defaults
        prefs = data["preferences"]
        prefs.setdefault("locations", [])
        prefs.setdefault("work_mode", "flexible")
        prefs.setdefault("company_size", "any")
        prefs.setdefault("company_stage", "any")
        prefs.setdefault("industry_interests", [])
        prefs.setdefault("salary_expectations", {"min_annual_ctc": 0, "max_annual_ctc": 0, "currency": "INR"})
        prefs.setdefault("risk_tolerance", "medium")
        prefs.setdefault("timeline", "flexible")

        career = data["career_analysis"]
        career.setdefault("primary_cluster", "General")
        career.setdefault("specializations", [{"name": "General", "fit_score": 0.5, "reasoning": "Based on conversation"}])
        career.setdefault("recommended_roles", [{"title": "Associate", "seniority": "entry", "fit_score": 0.5, "salary_alignment": True, "reasoning": "Based on conversation"}])
        career.setdefault("transition_paths", [])

        meta = data["session_metadata"]
        meta.setdefault("resume_uploaded", resume_uploaded)
        meta.setdefault("questions_answered", len([m for m in chat_history if m.role == "user"]))
        meta.setdefault("confidence_score", 0.7)

        payload = CandidatePayload(**data)
        logger.info(f"Payload generated: {payload.candidate_id}")
        return payload

    except Exception as e:
        logger.error(f"Payload generation error: {e}")
        raise


# ============================================================================
# Fast Streaming Mode — compact context, 2 LLM questions (Q6 + Q7)
# ============================================================================

SYSTEM_PROMPT_FAST = """You are StudojoProfiler. You are asking the FINAL 2 profiling questions.

The candidate already answered: career stage, job type, location, work style, company preference.
Their answers and background are shown below.

YOUR TASK:
- If this is Q6 (first LLM question): Ask ONE open descriptive question about what kind of work excites them. Set text_input: true, mcq: null. Keep it short and specific to their background.
  Example style: "What kind of problems do you love solving day-to-day — building things, analyzing data, convincing people, or something else?"
- If this is Q7 (you just received their Q6 answer): Ask ONE MCQ about industries or specific skills they want to develop. Generate 7-8 tailored options based on their profile and domain. Set allow_multiple: true.
- After Q7 answer is received: set is_complete: true, message: "Perfect, that's everything I need. Generating your profile now..."

RULES:
- Warm 1-sentence acknowledgment, then the question. Use ||| as separator: "Great answer.|||Next question?"
- For MCQ: generate options specific to their domain. Always include "Other" as the last option.
- Do NOT repeat any questions already asked. Do NOT give advice.

JSON RESPONSE (strict schema):
{"message":"ack|||question","current_state":"MCQ or TEXT","mcq":{"question":"...","options":[{"label":"A","text":"..."}],"allow_multiple":true} or null,"text_input":true or false,"is_complete":false,"questions_asked_so_far":6}
"""


def _detect_career_domain(skills: list) -> str:
    """Simple keyword matching to detect career domain from skills list."""
    skill_text = " ".join(str(s).lower() for s in skills)
    domains = {
        "tech/engineering": ["python", "javascript", "java", "react", "node", "aws", "sql", "backend", "frontend", "devops", "docker", "typescript", "golang", "c++", "kotlin", "swift"],
        "data/analytics": ["machine learning", "data science", "pandas", "tensorflow", "analytics", "tableau", "statistics", "r programming", "power bi", "excel", "numpy"],
        "marketing/growth": ["seo", "sem", "content marketing", "social media", "google analytics", "hubspot", "email marketing", "copywriting", "brand", "growth hacking", "ads"],
        "finance/consulting": ["financial modeling", "accounting", "cfa", "equity", "valuation", "financial analysis", "consulting", "excel", "bloomberg", "investment"],
        "design/ux": ["figma", "ui/ux", "adobe", "user research", "wireframe", "sketch", "illustrator", "photoshop", "prototyping"],
        "product": ["product management", "agile", "scrum", "roadmap", "user stories", "jira", "product strategy", "okrs"],
        "sales/bd": ["crm", "salesforce", "b2b", "lead generation", "account management", "business development", "cold outreach"],
    }
    best, best_score = "general", 0
    for domain, keywords in domains.items():
        score = sum(1 for kw in keywords if kw in skill_text)
        if score > best_score:
            best, best_score = domain, score
    return best


def build_messages_fast(
    chat_history: list[ChatMessage],
    resume_summary: dict | None = None,
) -> list[dict]:
    """Compact message builder — small context window for fast LLM inference."""
    system = SYSTEM_PROMPT_FAST

    # Compact resume profile (~250 chars max)
    if resume_summary and isinstance(resume_summary, dict):
        parts = []
        if resume_summary.get("name"):
            parts.append(f"Name: {resume_summary['name']}")
        skills = resume_summary.get("skills", [])
        if skills:
            parts.append(f"Skills: {', '.join(str(s) for s in skills[:10])}")
            domain = _detect_career_domain(skills)
            parts.append(f"Domain: {domain}")
        exp = resume_summary.get("experience_years")
        if exp is not None:
            parts.append(f"Exp: {exp}y")
        edu = resume_summary.get("education", [])
        if edu:
            edu_str = str(edu[0])[:50] if isinstance(edu, list) and edu else str(edu)[:50]
            parts.append(f"Education: {edu_str}")
        if parts:
            system += f"\n\nCANDIDATE PROFILE: {' | '.join(parts)}"

    # Compact summary of static question answers (Q1–Q5)
    user_msgs = [m for m in chat_history if m.role == "user" and m.content not in ("__start__",)]
    labels = ["Career stage", "Job type", "Location", "Work style", "Company preference", "Career goal"]
    prior = [f"{labels[i]}: {user_msgs[i].content[:100]}" for i in range(min(6, len(user_msgs)))]
    if prior:
        system += "\n\nPREVIOUS ANSWERS:\n" + "\n".join(prior)

    messages = [{"role": "system", "content": system}]

    # Include only the LLM-phase turns (after the static Q&A block)
    # Static questions generate ~12 messages (6 Q&A pairs). Take only what's after.
    static_qa_count = min(len(user_msgs), 6) * 2
    llm_turns = chat_history[static_qa_count:] if len(chat_history) > static_qa_count else []
    recent = llm_turns[-4:] if len(llm_turns) > 4 else llm_turns

    if recent:
        for msg in recent:
            messages.append({"role": msg.role, "content": msg.content})
    else:
        messages.append({"role": "user", "content": "Please ask the next profiling question."})

    return messages


def stream_agent_response(
    chat_history: list[ChatMessage],
    resume_summary: dict | None = None,
):
    """
    Stream the final 2 LLM profiling questions with reduced context.
    Yields: ('chunk', str) for text tokens | ('done', AgentResponse) | ('error', str)
    """
    t_start = time.perf_counter()
    client = _get_client()
    model = _get_model()
    messages = build_messages_fast(chat_history, resume_summary)

    accumulated = ""
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            max_completion_tokens=600,
            stream=True,
        )

        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                delta = chunk.choices[0].delta.content
                accumulated += delta
                yield "chunk", delta

        t_llm = time.perf_counter()
        logger.info(f"[STREAM] LLM done: {(t_llm - t_start)*1000:.0f}ms, {len(accumulated)} chars")

        response = _parse_llm_json(accumulated, chat_history)

        # Guard: don't allow completion before Q7 answer
        user_answer_count = len([m for m in chat_history if m.role == "user" and m.content not in ("__start__",)])
        if response.is_complete and user_answer_count < 7:
            logger.warning(f"[STREAM] Premature completion at {user_answer_count} answers — forcing continuation")
            response.is_complete = False

        yield "done", response

    except Exception as e:
        logger.error(f"[STREAM] Error: {e}", exc_info=True)
        yield "error", str(e)
