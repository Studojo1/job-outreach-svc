"""Payload Builder — extracted from the candidate intelligence main.py.

Provides ``generate_payload_from_answers`` for building the final
CandidatePayload dict without importing the full intelligence FastAPI app.
"""

import re
import uuid as _uuid
from datetime import datetime as _dt

from services.candidate_intelligence.career_ontology import CAREER_ONTOLOGY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_salary(salary_text: str) -> dict:
    nums = re.findall(r'[\d.]+', salary_text)
    multiplier = 1
    text_lower = salary_text.lower()
    if "lpa" in text_lower or "lac" in text_lower or "lakh" in text_lower:
        multiplier = 100000
    elif "cr" in text_lower:
        multiplier = 10000000
    if len(nums) >= 2:
        return {"min_annual_ctc": int(float(nums[0]) * multiplier),
                "max_annual_ctc": int(float(nums[1]) * multiplier), "currency": "INR"}
    elif len(nums) == 1:
        val = int(float(nums[0]) * multiplier)
        return {"min_annual_ctc": val, "max_annual_ctc": int(val * 1.3), "currency": "INR"}
    return {"min_annual_ctc": 0, "max_annual_ctc": 0, "currency": "INR"}


def _map_work_mode(answer: str) -> str:
    a = answer.lower()
    if "remote" in a:
        return "remote"
    elif "hybrid" in a:
        return "hybrid"
    elif "on-site" in a or "office" in a:
        return "onsite"
    return "flexible"


def _find_matching_roles(domains: list, specs: list, seniority: str,
                         user_skills: list | None = None) -> list:
    if user_skills is None:
        user_skills = []
    user_skills_lower = {s.lower() for s in user_skills}
    roles = []

    for cluster_name, specializations in CAREER_ONTOLOGY.items():
        domain_match = any(
            d.lower() in cluster_name.lower() or cluster_name.lower() in d.lower()
            for d in domains
        )
        if not domain_match:
            continue

        domain_rank = 0
        for i, d in enumerate(domains):
            if d.lower() in cluster_name.lower() or cluster_name.lower() in d.lower():
                domain_rank = i
                break

        for spec_name, spec_roles in specializations.items():
            spec_match = any(
                s.lower() in spec_name.lower() or spec_name.lower() in s.lower()
                for s in specs
            ) if specs else False
            if not spec_match and specs:
                continue

            for role_idx, role in enumerate(spec_roles[:3]):
                score = 0.60
                score += 0.15 if domain_rank == 0 else 0.08
                if spec_match:
                    score += 0.10
                skill_hits = sum(1 for s in user_skills_lower
                                 if s in role.lower() or role.lower() in s)
                score += min(0.10, skill_hits * 0.05)
                score -= role_idx * 0.04
                if domain_rank > 0:
                    score -= 0.03
                if len(user_skills) >= 5:
                    score += 0.03

                fit_score = round(min(0.97, max(0.55, score)), 2)

                if fit_score >= 0.90:
                    reasoning = f"Exceptional match for {role} — your skills and specialization in {spec_name} align very closely."
                elif fit_score >= 0.80:
                    reasoning = f"Strong alignment with {role} based on your interest in {cluster_name} / {spec_name}."
                elif fit_score >= 0.70:
                    reasoning = f"Good fit for {role}. Building skills in {spec_name} will strengthen this match."
                else:
                    reasoning = f"Emerging opportunity in {spec_name}. Consider upskilling to improve your competitiveness."

                roles.append({
                    "title": role, "seniority": seniority,
                    "cluster": cluster_name, "specialization": spec_name,
                    "fit_score": fit_score, "salary_alignment": True,
                    "reasoning": reasoning,
                })

    roles.sort(key=lambda x: x["fit_score"], reverse=True)
    return roles[:5]


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def generate_payload_from_answers(session: dict) -> dict:
    """Build the full CandidatePayload from collected answers + ontology."""
    answers = session.get("answers", {})
    resume_summary = session.get("resume_summary", {}) or {}

    def _to_str(val) -> str:
        if isinstance(val, list):
            return ", ".join(val)
        return str(val) if val else ""

    stage = _to_str(answers.get("stage", ""))
    job_type = _to_str(answers.get("job_type", ""))
    domains = answers.get("domain", [])
    if isinstance(domains, str):
        domains = [domains]
    specs = answers.get("specialization", [])
    if isinstance(specs, str):
        specs = [specs]
    locations = answers.get("location", [])
    if isinstance(locations, str):
        locations = [locations]
    work_style = _to_str(answers.get("work_style", ""))
    company_stage = _to_str(answers.get("company_stage", ""))
    industries = answers.get("industry", [])
    if isinstance(industries, str):
        industries = [industries]
    salary_text = _to_str(answers.get("salary", ""))
    role_focus = answers.get("role_focus", [])
    if isinstance(role_focus, str):
        role_focus = [role_focus]
    skills = answers.get("skills", [])
    if isinstance(skills, str):
        skills = [skills]
    timeline = _to_str(answers.get("timeline", ""))
    education_level = _to_str(answers.get("education_level", ""))
    years_exp = _to_str(answers.get("years_experience", ""))

    seniority = "entry"
    if "intern" in job_type.lower():
        seniority = "intern"
    elif "student" in stage.lower() and "not graduating" in stage.lower():
        seniority = "intern"
    elif "experienced" in stage.lower() or "3+" in stage.lower():
        seniority = "mid"
    elif "switch" in stage.lower():
        seniority = "junior"

    detected_skills = resume_summary.get("skills", []) if isinstance(resume_summary, dict) else []
    if not detected_skills:
        detected_skills = skills
    all_skills = list(dict.fromkeys(detected_skills + skills))

    matched_roles = _find_matching_roles(domains, specs, seniority, user_skills=all_skills)
    if not matched_roles:
        matched_roles = [
            {"title": "Business Analyst", "seniority": seniority,
             "cluster": "Consulting & Strategy",
             "specialization": "Management Consulting",
             "fit_score": 0.75, "salary_alignment": True,
             "reasoning": "General fit based on profile"},
        ]

    primary_cluster = domains[0] if domains else matched_roles[0]["cluster"]
    secondary_cluster = domains[1] if len(domains) > 1 else None

    spec_fits = []
    seen_specs = set()
    for role in matched_roles:
        if role["specialization"] not in seen_specs:
            seen_specs.add(role["specialization"])
            spec_fits.append({
                "name": role["specialization"],
                "fit_score": role["fit_score"],
                "reasoning": f"Strong match based on your interest in {role['cluster']}",
            })

    role_fits = [{"title": r["title"], "seniority": r["seniority"],
                  "fit_score": r["fit_score"],
                  "salary_alignment": r["salary_alignment"],
                  "reasoning": r["reasoning"]} for r in matched_roles]

    transitions = []
    if seniority in ("intern", "entry"):
        transitions.append(
            f"{matched_roles[0]['title']} → Senior {matched_roles[0]['title'].split()[0]} → Lead")
    if len(domains) > 1:
        transitions.append(f"Cross-domain: {domains[0]} ↔ {domains[1]}")
    transitions.append("Individual Contributor → Management track")

    risk = "medium"
    if "early" in company_stage.lower() or "seed" in company_stage.lower():
        risk = "high"
    elif "enterprise" in company_stage.lower() or "mnc" in company_stage.lower() or "government" in company_stage.lower():
        risk = "low"

    summary_parts = []
    if stage:
        summary_parts.append(stage.rstrip("."))
    if domains:
        summary_parts.append(f"interested in {', '.join(domains[:2])}")
    if locations:
        summary_parts.append(f"looking to work in {', '.join(locations[:2])}")
    profile_summary = ". ".join(summary_parts) + "." if summary_parts else "Career profile generated from conversation."

    resume_education = []
    if isinstance(resume_summary, dict):
        for edu in resume_summary.get("education", []):
            if isinstance(edu, str):
                resume_education.append({"degree": edu, "field": "From Resume"})
            elif isinstance(edu, dict):
                resume_education.append(edu)
    if not resume_education and education_level:
        resume_education = [{"degree": education_level, "field": "General"}]

    resume_yoe = None
    if isinstance(resume_summary, dict):
        resume_yoe = resume_summary.get("years_experience")
    if not resume_yoe:
        resume_yoe = years_exp if years_exp else None

    return {
        "candidate_id": str(_uuid.uuid4()),
        "timestamp": _dt.now().isoformat(),
        "profile_summary": profile_summary,
        "personal_info": {
            "name": resume_summary.get("name") if isinstance(resume_summary, dict) else None,
            "email": resume_summary.get("email") if isinstance(resume_summary, dict) else None,
            "phone": resume_summary.get("phone") if isinstance(resume_summary, dict) else None,
            "education": resume_education,
            "years_of_experience": resume_yoe,
            "skills_detected": all_skills[:15],
        },
        "preferences": {
            "locations": locations,
            "work_mode": _map_work_mode(work_style),
            "company_type": company_stage,
            "company_size": company_stage,
            "company_stage": company_stage,
            "industry_interests": industries,
            "salary_expectations": _parse_salary(salary_text),
            "risk_tolerance": risk,
            "timeline": timeline,
            "role_focus": role_focus,
        },
        "career_analysis": {
            "primary_cluster": primary_cluster,
            "secondary_cluster": secondary_cluster,
            "specializations": spec_fits[:3],
            "recommended_roles": role_fits[:5],
            "transition_paths": transitions,
        },
        "session_metadata": {
            "resume_uploaded": session.get("resume_uploaded", False),
            "questions_answered": len(answers),
            "confidence_score": min(0.95, 0.5 + len(answers) * 0.04),
        },
    }
