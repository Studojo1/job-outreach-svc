"""
Deterministic payload builder — zero LLM calls.

Replaces generate_final_payload() by mapping structured quiz answers
directly to the CandidatePayload schema. Takes ~1ms instead of 15-30s.
"""

import re
import uuid
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# ── Answer helpers ─────────────────────────────────────────────────────────

def _parse_multi(answer: str) -> list[str]:
    """Split a comma-separated MCQ answer into clean values."""
    if not answer:
        return []
    return [p.strip() for p in answer.split(",") if p.strip()]


def _map_seniority(career_stage: str) -> str:
    s = career_stage.lower()
    if "not graduating" in s:
        return "intern"
    if "graduating" in s or "recent graduate" in s or "0-2 years" in s:
        return "entry"
    if "experienced" in s or "3+" in s:
        return "mid"
    if "switching" in s:
        return "junior"
    return "entry"


def _map_work_mode(work_style: str) -> str:
    s = work_style.lower()
    if "fully remote" in s or "remote" in s:
        return "remote"
    if "hybrid" in s:
        return "hybrid"
    if "on-site" in s or "in office" in s or "onsite" in s:
        return "onsite"
    return "flexible"


def _map_company_size(company_stage: str) -> str:
    s = company_stage.lower()
    if "under 50" in s or "seed" in s or "early" in s:
        return "1-50"
    if "50-500" in s or "growth" in s:
        return "50-500"
    if "500-2000" in s or "mid-size" in s:
        return "500-2000"
    if "2000" in s or "enterprise" in s or "mnc" in s:
        return "2000+"
    return "any"


def _map_risk_tolerance(company_stage: str) -> str:
    s = company_stage.lower()
    if "early" in s or "seed" in s:
        return "high"
    if "growth" in s:
        return "medium"
    if "enterprise" in s or "mnc" in s or "large" in s:
        return "low"
    return "medium"


def _parse_salary(salary_text: str) -> dict:
    """Parse '₹4-6 LPA', '$80k-100k', etc. into structured dict."""
    if not salary_text or len(salary_text) < 3:
        return {"min_annual_ctc": 0, "max_annual_ctc": 0, "currency": "INR"}
    currency = "USD" if "$" in salary_text else "INR"
    numbers = re.findall(r"[\d.]+", salary_text)
    if not numbers:
        return {"min_annual_ctc": 0, "max_annual_ctc": 0, "currency": currency}
    nums = [float(n) for n in numbers[:2]]
    s = salary_text.lower()
    if "lpa" in s or "lakh" in s:
        nums = [n * 100_000 for n in nums]
    elif "k" in s and currency == "USD":
        nums = [n * 1_000 for n in nums]
    if len(nums) == 1:
        return {"min_annual_ctc": int(nums[0]), "max_annual_ctc": int(nums[0]), "currency": currency}
    return {"min_annual_ctc": int(nums[0]), "max_annual_ctc": int(nums[1]), "currency": currency}


# ── Career cluster / role logic ────────────────────────────────────────────

_GOAL_TO_CLUSTER = [
    ("Software Engineering",          ["code", "engineer", "developer", "backend", "frontend", "full-stack", "mobile", "ios", "android"]),
    ("Data & Analytics",              ["data analyst", "analytics", "sql", "insight", "dashboard", "bi ", "reporting"]),
    ("Data Science & AI",             ["machine learning", "ml ", "deep learning", "data science", "ai model", "nlp"]),
    ("Product Management",            ["product manager", "roadmap", "pm ", "product management", "product strategy"]),
    ("Marketing & Growth",            ["marketing", "growth", "gtm", "go-to-market", "brand", "content", "seo", "ads", "acquisition"]),
    ("Sales & Business Development",  ["sales", "closing", "deals", "revenue", "bd ", "business development", "account"]),
    ("Design & UX",                   ["design", "ux", "ui ", "figma", "user experience", "visual"]),
    ("Finance & Investment",          ["finance", "investment", "modeling", "banking", "valuation", "equity"]),
    ("Consulting & Strategy",         ["consulting", "strategy", "advisory", "management consulting"]),
    ("Operations",                    ["operations", "ops ", "supply chain", "logistics", "process"]),
]

_ROLE_KEYWORDS: dict[str, list[str]] = {
    "Software Engineer":         ["code", "engineer", "developer", "backend", "frontend", "full-stack"],
    "Data Analyst":              ["data analyst", "analyzing data", "sql", "data analysis", "bi "],
    "Data Scientist":            ["machine learning", "ml ", "data science", "ai model", "nlp"],
    "Product Manager":           ["product manager", "pm ", "product management", "roadmap"],
    "Marketing Manager":         ["marketing", "growth", "gtm", "brand", "acquisition"],
    "Sales Executive":           ["sales", "closing", "deals", "revenue", "account"],
    "UX Designer":               ["design", "ux", "ui ", "figma", "user experience"],
    "Business Analyst":          ["business analyst", "strategy", "planning", "process"],
    "Financial Analyst":         ["finance", "investment", "modeling", "valuation"],
    "Management Consultant":     ["consulting", "advisory", "strategy consulting"],
}


def _build_primary_cluster(answers: dict, resume_profile: dict) -> str:
    domain = resume_profile.get("domain", "")
    if domain:
        return domain.replace("_", " ").title()
    goal = answers.get("career_goal", "").lower()
    for cluster, kws in _GOAL_TO_CLUSTER:
        if any(kw in goal for kw in kws):
            return cluster
    return "General Management"


def _build_specializations(answers: dict, resume_profile: dict) -> list[dict]:
    primary = _build_primary_cluster(answers, resume_profile)
    subdomain = resume_profile.get("subdomain", "")
    specs = []
    if subdomain:
        specs.append({"name": subdomain.replace("_", " ").title(), "fit_score": 0.85, "reasoning": "Detected from resume"})
    specs.append({"name": primary, "fit_score": 0.80, "reasoning": "Primary career cluster"})
    return specs[:3]


def _build_recommended_roles(answers: dict, resume_profile: dict) -> list[dict]:
    from services.candidate_intelligence.career_ontology import CAREER_ONTOLOGY

    seniority = _map_seniority(answers.get("career_stage", ""))
    career_goal = answers.get("career_goal", "")
    target_role = answers.get("target_role", "")
    roles: list[dict] = []
    seen: set[str] = set()

    def _add(title: str, score: float, reason: str):
        if title and title.lower() not in ("other", "skip", "") and title not in seen:
            roles.append({"title": title, "seniority": seniority, "fit_score": score, "salary_alignment": True, "reasoning": reason})
            seen.add(title)

    # 1. Resume profile likely_roles (highest signal)
    for r in resume_profile.get("likely_roles", [])[:3]:
        title = r if isinstance(r, str) else r.get("title", "")
        _add(title, 0.88, "Matched from resume profile")

    # 2. Explicit quiz answer
    for t in _parse_multi(target_role):
        _add(t, 0.85, "Explicitly selected by candidate")

    # 3. Infer from career_goal text
    if career_goal:
        goal_lower = career_goal.lower()
        for role_title, kws in _ROLE_KEYWORDS.items():
            if any(kw in goal_lower for kw in kws):
                _add(role_title, 0.78, f"Inferred from career goal")
                break

    # 4. Ontology fallback if fewer than 2 roles found
    if len(roles) < 2:
        domain = resume_profile.get("domain", "").lower()
        for cluster_name, specializations in CAREER_ONTOLOGY.items():
            if domain and (domain in cluster_name.lower() or cluster_name.lower() in domain):
                for _, role_list in list(specializations.items())[:2]:
                    for r in role_list[:2]:
                        if len(roles) < 5:
                            _add(r, 0.70, f"Matched from career domain: {cluster_name}")
                break

    if not roles:
        roles.append({"title": "Associate", "seniority": seniority, "fit_score": 0.50, "salary_alignment": True, "reasoning": "Default"})

    return roles[:5]


def _build_profile_summary(name: str, seniority: str, primary_cluster: str,
                            locations: list[str], company_stage: str,
                            top_skills: list[str], job_type: str) -> str:
    name_part = name or "The candidate"
    seniority_labels = {"intern": "a student", "entry": "a recent graduate", "junior": "a junior", "mid": "an experienced"}
    seniority_label = seniority_labels.get(seniority, "a")

    skills_part = f" with expertise in {', '.join(top_skills[:3])}" if top_skills else ""

    non_remote = [l for l in locations if l.lower() not in ("remote", "international", "other")]
    if non_remote:
        location_part = f" based in {', '.join(non_remote[:2])}"
    elif "Remote" in locations:
        location_part = " open to remote work globally"
    else:
        location_part = ""

    company_part = ""
    s = company_stage.lower()
    if "early" in s or "seed" in s:
        company_part = " early-stage startup"
    elif "growth" in s:
        company_part = " growth-stage company"
    elif "enterprise" in s or "mnc" in s:
        company_part = " enterprise"

    opportunity_type = "internship" if job_type and "intern" in job_type.lower() else "full-time"

    return (
        f"{name_part} is {seniority_label} {primary_cluster} professional{skills_part}{location_part}. "
        f"Targeting {opportunity_type} roles at{company_part} companies."
    )


# ── Answer reconstruction ───────────────────────────────────────────────────

def reconstruct_answers(chat_history_dicts: list[dict], candidate) -> dict:
    """
    Replay chat history through the question sequence to recover the structured
    answers dict. Same logic used in the chat/stream endpoint.
    """
    from services.candidate_intelligence.question_engine import build_question_sequence

    raw_user_msgs = [
        m["content"] for m in chat_history_dicts
        if m.get("role") == "user" and m.get("content") != "__start__"
    ]

    resume_profile = candidate.resume_profile if isinstance(candidate.resume_profile, dict) else {}
    resume_text = candidate.resume_text or ""
    parsed_json = candidate.parsed_json if isinstance(candidate.parsed_json, dict) else {}

    answers: dict[str, str] = {}
    for answer in raw_user_msgs:
        state = {
            "answers": answers.copy(),
            "resume_profile": resume_profile,
            "resume_text": resume_text,
            "parsed_json": {**parsed_json, "_resume_text": resume_text},
        }
        seq = build_question_sequence(state)
        idx = len(answers)
        if idx < len(seq):
            answers[seq[idx]["key"]] = answer

    return answers


# ── Main entry point ────────────────────────────────────────────────────────

def build_payload_from_answers(answers: dict, candidate, resume_uploaded: bool = True) -> dict:
    """
    Build the full parsed_json payload from structured quiz answers.
    Zero LLM calls — pure deterministic mapping. ~1ms vs 15-30s.
    """
    resume_profile = candidate.resume_profile if isinstance(candidate.resume_profile, dict) else {}
    parsed_json = candidate.parsed_json if isinstance(candidate.parsed_json, dict) else {}

    # ── Personal info from resume (extracted at upload time) ───────────────
    name = (
        resume_profile.get("name")
        or parsed_json.get("personal_info", {}).get("name")
        or ""
    )
    email = (
        resume_profile.get("email")
        or parsed_json.get("personal_info", {}).get("email")
        or ""
    )
    resume_skills = list(resume_profile.get("top_skills", []))
    existing_skills = (
        parsed_json.get("personal_info", {}).get("skills_detected", [])
        or parsed_json.get("skills", [])
    )
    all_skills = resume_skills + [s for s in existing_skills if s not in resume_skills]

    # ── Map answers to fields ───────────────────────────────────────────────
    career_stage   = answers.get("career_stage", "")
    job_type       = answers.get("job_type", "")
    company_stage  = answers.get("company_stage", "")
    salary_text    = answers.get("salary", "")
    timeline_raw   = answers.get("timeline", "")

    locations      = _parse_multi(answers.get("location", ""))
    industries     = _parse_multi(answers.get("industry", ""))
    industry_interests = [
        i.split(" / ")[0].strip() for i in industries
        if i and i.lower() not in ("other", "skip")
    ]

    seniority      = _map_seniority(career_stage)
    work_mode      = _map_work_mode(answers.get("work_style", ""))
    company_size   = _map_company_size(company_stage)
    salary         = _parse_salary(salary_text)
    risk_tolerance = _map_risk_tolerance(company_stage)

    timeline = timeline_raw
    tl = timeline_raw.lower()
    if "immediately" in tl or "1 month" in tl:
        timeline = "Immediately (within 1 month)"
    elif "1-3" in tl:
        timeline = "In 1-3 months"
    elif "3-6" in tl:
        timeline = "In 3-6 months"
    elif "6+" in tl or "exploring" in tl:
        timeline = "6+ months (exploring)"

    # ── Career analysis ─────────────────────────────────────────────────────
    primary_cluster   = _build_primary_cluster(answers, resume_profile)
    specializations   = _build_specializations(answers, resume_profile)
    recommended_roles = _build_recommended_roles(answers, resume_profile)

    profile_summary = _build_profile_summary(
        name=name, seniority=seniority, primary_cluster=primary_cluster,
        locations=locations, company_stage=company_stage,
        top_skills=all_skills[:3], job_type=job_type,
    )

    questions_answered = len([v for v in answers.values() if v])

    payload = {
        "candidate_id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "profile_summary": profile_summary,
        "personal_info": {
            "name": name or None,
            "email": email or None,
            "education": resume_profile.get("education_level") or [],
            "skills_detected": all_skills[:20],
        },
        "preferences": {
            "locations": locations,
            "work_mode": work_mode,
            "company_size": company_size,
            "company_stage": company_stage,
            "industry_interests": industry_interests,
            "salary_expectations": salary,
            "risk_tolerance": risk_tolerance,
            "timeline": timeline,
        },
        "career_analysis": {
            "primary_cluster": primary_cluster,
            "secondary_cluster": specializations[1]["name"] if len(specializations) > 1 else None,
            "specializations": specializations,
            "recommended_roles": recommended_roles,
            "transition_paths": [],
        },
        "session_metadata": {
            "resume_uploaded": resume_uploaded,
            "questions_answered": questions_answered,
            "confidence_score": round(min(0.60 + questions_answered * 0.04, 0.95), 2),
        },
    }

    logger.info(
        f"[PAYLOAD-BUILDER] Built payload in <1ms: name={name!r}, cluster={primary_cluster}, "
        f"roles={[r['title'] for r in recommended_roles]}, locations={locations}"
    )
    return payload
