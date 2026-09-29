"""
CandidateProfiler — Career Ontology
Comprehensive mapping of career clusters → specializations → entry-level roles.
This grounds the LLM so it recommends real roles, not hallucinated ones.
"""

CAREER_ONTOLOGY = {
    "Finance & Accounting": {
        "Financial Planning & Analysis": [
            "FP&A Analyst", "Budget Analyst", "Financial Modeler", "Forecasting Analyst"
        ],
        "Investment Banking & Capital Markets": [
            "IB Analyst", "M&A Analyst", "Equity Research Analyst", "Capital Markets Associate"
        ],
        "Risk & Compliance": [
            "Risk Analyst", "Compliance Analyst", "Internal Auditor", "Regulatory Affairs Associate"
        ],
        "Accounting & Audit": [
            "Staff Accountant", "Tax Associate", "Audit Associate", "Accounts Payable Specialist"
        ],
        "Treasury & Corporate Finance": [
            "Treasury Analyst", "Corporate Finance Analyst", "Cash Management Analyst"
        ],
        "Revenue Operations": [
            "RevOps Analyst", "Revenue Manager", "Billing Operations Analyst"
        ],
        "Insurance & Actuarial": [
            "Actuarial Analyst", "Underwriting Analyst", "Claims Analyst"
        ],
        "Wealth Management": [
            "Financial Advisor Associate", "Portfolio Analyst", "Client Relationship Associate"
        ],
    },

    "Technology & Engineering": {
        "Software Development": [
            "Software Engineer", "Frontend Developer", "Backend Developer", "Full-Stack Developer",
            "Mobile Developer (iOS/Android)", "DevOps Engineer"
        ],
        "Data Engineering": [
            "Data Engineer", "ETL Developer", "Database Administrator", "Data Pipeline Engineer"
        ],
        "Cloud & Infrastructure": [
            "Cloud Engineer", "Site Reliability Engineer", "Systems Administrator",
            "Infrastructure Engineer"
        ],
        "Cybersecurity": [
            "Security Analyst", "SOC Analyst", "Penetration Tester", "Security Engineer"
        ],
        "Quality Assurance": [
            "QA Engineer", "Test Automation Engineer", "QA Analyst"
        ],
        "Embedded & Hardware": [
            "Embedded Systems Engineer", "Firmware Engineer", "Hardware Engineer",
            "IoT Developer"
        ],
        "IT Support & Administration": [
            "IT Support Specialist", "Systems Administrator", "Network Engineer",
            "Help Desk Technician"
        ],
    },

    "Data & Analytics": {
        "Data Analysis & BI": [
            "Data Analyst", "BI Analyst", "Reporting Analyst", "Dashboard Developer",
            "Business Intelligence Developer"
        ],
        "Data Science & ML": [
            "Data Scientist", "Machine Learning Engineer", "AI Engineer",
            "Applied AI Engineer", "LLM Engineer", "GenAI Engineer",
            "Speech / ASR Engineer", "NLP Engineer", "Computer Vision Engineer",
            "AI Agent Engineer", "Research Scientist", "Prompt Engineer",
        ],
        "Analytics Engineering": [
            "Analytics Engineer", "Data Modeler", "Metrics Engineer"
        ],
        "Product Analytics": [
            "Product Analyst", "Growth Analyst", "User Research Analyst"
        ],
        "Quantitative Analysis": [
            "Quantitative Analyst", "Statistical Analyst", "Econometrician"
        ],
    },

    "Marketing & Growth": {
        "Digital Marketing": [
            "Digital Marketing Specialist", "SEO Specialist", "SEM Specialist",
            "Social Media Manager", "Email Marketing Specialist"
        ],
        "Content & Brand": [
            "Content Marketing Specialist", "Copywriter", "Brand Strategist",
            "Content Creator", "Technical Writer"
        ],
        "Marketing Analytics & Operations": [
            "Marketing Analyst", "Marketing Operations Specialist", "CRM Specialist",
            "Marketing Automation Specialist"
        ],
        "Growth & Performance": [
            "Growth Marketing Manager", "Performance Marketer", "Acquisition Specialist",
            "Conversion Rate Optimizer"
        ],
        "Product Marketing": [
            "Product Marketing Manager", "Go-to-Market Analyst", "Competitive Intelligence Analyst"
        ],
        "Public Relations": [
            "PR Specialist", "Communications Coordinator", "Media Relations Associate"
        ],
    },

    "Sales & Business Development": {
        "Inside Sales": [
            "Sales Development Representative (SDR)", "Business Development Representative (BDR)",
            "Inside Sales Associate", "Lead Qualification Specialist"
        ],
        "Account Management": [
            "Account Manager", "Account Executive", "Client Success Manager",
            "Key Account Associate"
        ],
        "Enterprise Sales": [
            "Enterprise Sales Associate", "Solutions Consultant", "Sales Engineer"
        ],
        "Channel & Partnerships": [
            "Channel Sales Associate", "Partnerships Manager", "Alliance Manager"
        ],
        "Sales Operations": [
            "Sales Operations Analyst", "CRM Administrator", "Revenue Analyst"
        ],
    },

    "Operations & Supply Chain": {
        "Business Operations": [
            "Operations Analyst", "Business Operations Associate", "Process Improvement Analyst",
            "Operations Coordinator"
        ],
        "Supply Chain & Logistics": [
            "Supply Chain Analyst", "Logistics Coordinator", "Procurement Analyst",
            "Inventory Analyst", "Demand Planner"
        ],
        "Project & Program Management": [
            "Project Coordinator", "Project Manager", "Program Analyst",
            "Scrum Master", "Agile Coach"
        ],
        "Quality Management": [
            "Quality Assurance Analyst", "Quality Control Inspector", "Six Sigma Analyst"
        ],
    },

    "Human Resources & People Ops": {
        "HR Generalist": [
            "HR Coordinator", "HR Associate", "People Operations Associate",
            "HR Business Partner (Junior)"
        ],
        "Talent Acquisition": [
            "Recruiter", "Technical Recruiter", "Talent Sourcer",
            "Recruitment Coordinator"
        ],
        "Learning & Development": [
            "L&D Coordinator", "Training Specialist", "Instructional Designer"
        ],
        "Compensation & Benefits": [
            "Compensation Analyst", "Benefits Coordinator", "Payroll Specialist"
        ],
        "HR Analytics": [
            "People Analytics Analyst", "Workforce Planning Analyst", "HRIS Analyst"
        ],
    },

    "Design & Creative": {
        "UX/UI Design": [
            "UX Designer", "UI Designer", "Product Designer", "Interaction Designer",
            "UX Researcher"
        ],
        "Visual & Graphic Design": [
            "Graphic Designer", "Visual Designer", "Brand Designer",
            "Packaging Designer"
        ],
        "Motion & Video": [
            "Motion Graphics Designer", "Video Editor", "Animator",
            "Multimedia Specialist"
        ],
        "Industrial & Product Design": [
            "Industrial Designer", "Product Design Engineer", "CAD Designer"
        ],
    },

    "Consulting & Strategy": {
        "Management Consulting": [
            "Management Consultant (Analyst)", "Strategy Analyst", "Business Consultant",
            "Associate Consultant"
        ],
        "Technology Consulting": [
            "Technology Consultant", "IT Consultant", "Digital Transformation Analyst",
            "ERP Consultant"
        ],
        "Financial Advisory": [
            "Financial Advisory Analyst", "Valuation Analyst", "Due Diligence Analyst"
        ],
        "Research & Insights": [
            "Market Research Analyst", "Industry Research Associate", "Competitive Intelligence Analyst"
        ],
    },

    "Legal & Compliance": {
        "Corporate Law": [
            "Legal Associate", "Corporate Paralegal", "Contract Analyst",
            "Legal Researcher"
        ],
        "Regulatory & Compliance": [
            "Compliance Officer", "Regulatory Analyst", "Policy Analyst",
            "Ethics & Compliance Associate"
        ],
        "Intellectual Property": [
            "IP Analyst", "Patent Associate", "Trademark Specialist"
        ],
    },

    "Healthcare & Life Sciences": {
        "Clinical & Medical": [
            "Clinical Research Associate", "Medical Writer", "Pharmacovigilance Associate",
            "Clinical Data Analyst"
        ],
        "Healthcare Administration": [
            "Healthcare Administrator", "Medical Billing Specialist",
            "Health Informatics Analyst", "Hospital Operations Coordinator"
        ],
        "Biotech & Pharma": [
            "Research Associate (Biotech)", "Lab Technician", "Quality Control Analyst",
            "Regulatory Affairs Associate"
        ],
    },

    "Manufacturing & Production": {
        "Production & Plant Operations": [
            "Production Supervisor", "Manufacturing Engineer", "Plant Operator",
            "Production Planner"
        ],
        "Industrial Engineering": [
            "Industrial Engineer", "Process Engineer", "Methods Engineer",
            "Lean Manufacturing Specialist"
        ],
        "Maintenance & Safety": [
            "Maintenance Technician", "Safety Officer", "EHS Coordinator",
            "Reliability Engineer"
        ],
    },

    "Media & Communications": {
        "Journalism & Editorial": [
            "Reporter", "Editor", "Journalist", "News Producer",
            "Fact-Checker"
        ],
        "Digital Media": [
            "Social Media Coordinator", "Content Strategist", "Digital Producer",
            "Podcast Producer"
        ],
        "Corporate Communications": [
            "Communications Specialist", "Internal Communications Coordinator",
            "Speechwriter", "Corporate Affairs Associate"
        ],
    },

    "Education & Training": {
        "Teaching & Instruction": [
            "Teacher", "Lecturer", "Tutor", "Curriculum Developer",
            "Academic Coordinator"
        ],
        "EdTech": [
            "EdTech Product Specialist", "Instructional Technologist",
            "Learning Experience Designer", "Ed-Tech Content Creator"
        ],
        "Research & Academia": [
            "Research Assistant", "Research Associate", "Lab Manager",
            "Academic Researcher"
        ],
    },

    "Real Estate & Construction": {
        "Real Estate": [
            "Real Estate Analyst", "Property Manager", "Leasing Consultant",
            "Real Estate Associate"
        ],
        "Construction Management": [
            "Construction Project Coordinator", "Site Engineer", "Estimator",
            "Construction Planner"
        ],
        "Architecture & Planning": [
            "Junior Architect", "Urban Planner", "Interior Designer",
            "Landscape Architect"
        ],
    },
}


def get_all_clusters() -> list[str]:
    """Return list of all career cluster names."""
    return list(CAREER_ONTOLOGY.keys())


def get_specializations(cluster: str) -> list[str]:
    """Return specializations for a given cluster."""
    return list(CAREER_ONTOLOGY.get(cluster, {}).keys())


def get_roles(cluster: str, specialization: str) -> list[str]:
    """Return roles for a given cluster and specialization."""
    return CAREER_ONTOLOGY.get(cluster, {}).get(specialization, [])


def get_all_roles_flat() -> list[str]:
    """Return all roles as a flat list."""
    roles = []
    for cluster in CAREER_ONTOLOGY.values():
        for spec_roles in cluster.values():
            roles.extend(spec_roles)
    return roles


def get_ontology_as_text() -> str:
    """Return the ontology as a readable text block for embedding in prompts."""
    lines = []
    for cluster_name, specializations in CAREER_ONTOLOGY.items():
        lines.append(f"\n## {cluster_name}")
        for spec_name, roles in specializations.items():
            role_list = ", ".join(roles)
            lines.append(f"  - {spec_name}: {role_list}")
    return "\n".join(lines)


def search_ontology(query: str) -> dict:
    """Search the ontology for matching clusters, specializations, and roles."""
    query_lower = query.lower()
    results = {"clusters": [], "specializations": [], "roles": []}

    for cluster_name, specializations in CAREER_ONTOLOGY.items():
        if query_lower in cluster_name.lower():
            results["clusters"].append(cluster_name)

        for spec_name, roles in specializations.items():
            if query_lower in spec_name.lower():
                results["specializations"].append({
                    "cluster": cluster_name,
                    "specialization": spec_name
                })

            for role in roles:
                if query_lower in role.lower():
                    results["roles"].append({
                        "cluster": cluster_name,
                        "specialization": spec_name,
                        "role": role
                    })

    return results


# ── Nearest real title (OP-N09) ─────────────────────────────────────────────
# The resume prompt asks for an archetype_label that is "NOT a job title from a
# job board" ("Zero-to-One Growth Systems Builder"), and it used to be offered
# as quiz option A, so about half of students picked it as a target role and it
# went into Apollo's q_organization_job_titles, where nobody holds it.

# Real titles the resume prompt's role-cluster reference names that the
# ontology above lacks. Without these, "Product Manager" would have no match.
TITLES_OUTSIDE_ONTOLOGY = (
    "Product Manager", "Associate Product Manager", "AI Product Manager",
    "Founding Product Manager", "Founder's Office", "ML Engineer", "Growth Hacker",
    "SDR", "BDR", "Business Development Representative", "VC Analyst",
    "Mobile Developer", "Management Consultant",
)

# Words that describe a style or level, not a function. A coined label is full
# of them ("High-Volume", "Zero-to-One", "Systems Builder"), and matching on
# them picks titles like "Systems Administrator".
_DESCRIPTOR_WORDS = frozenset({
    "and", "of", "the", "for", "to", "a", "an", "in", "with", "at", "on",
    "senior", "junior", "lead", "principal", "head", "chief", "founding",
    "entry", "level", "high", "volume", "zero", "one", "first", "led", "native",
    "systems", "system", "solutions", "builder", "operator", "generalist",
    "hybrid", "hire", "focused", "driven", "oriented", "end", "full", "cycle",
    "intern", "trainee", "fresher", "hygienist",
})
# Function nouns shared by many titles. They count, but only alongside a word
# that actually names the field.
_ROLE_NOUNS = frozenset({
    "engineer", "analyst", "manager", "associate", "specialist", "developer",
    "designer", "consultant", "coordinator", "executive", "representative",
    "officer", "administrator", "architect", "scientist", "researcher",
})
_ABBREVIATIONS = {"gtm": "go to market", "pm": "product manager", "ml": "machine learning"}


def _title_tokens(title: str) -> list[str]:
    import re
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    out: list[str] = []
    for w in words:
        out.extend(_ABBREVIATIONS.get(w, w).split())
    return [w for w in out if len(w) > 1]


def _known_titles() -> list[str]:
    seen, titles = set(), []
    for t in [*get_all_roles_flat(), *TITLES_OUTSIDE_ONTOLOGY]:
        if t.lower() not in seen:
            seen.add(t.lower())
            titles.append(t)
    return titles


# Words that only set the level of a real title: "Software Engineer Intern" is
# still a real title, and Apollo should search it as the student wrote it.
_LEVEL_WORDS = frozenset({
    "intern", "internship", "trainee", "apprentice", "graduate", "fresher",
    "senior", "junior", "sr", "jr", "lead", "associate", "entry", "level",
    "ii", "iii", "remote", "part", "time",
})


def is_known_title(title: str) -> bool:
    """True when `title` is a real title as written (OP-N09): an ontology role
    or prompt reference title, a substring of one, or one of them plus level
    words only ("Software Engineer Intern", "Senior Data Analyst")."""
    t = (title or "").strip().lower()
    if not t:
        return False
    known = _known_titles()
    if any(t == k.lower() for k in known) or search_ontology(t)["roles"]:
        return True
    q_set = set(_title_tokens(t))
    for k in known:
        k_set = set(_title_tokens(k))
        if k_set and k_set <= q_set and (q_set - k_set) <= _LEVEL_WORDS:
            return True
    return False


def nearest_real_title(title: str) -> str | None:
    """The closest real job title to `title`, or None when nothing is close.

    In order: an exact known title; the ontology's own substring search (the
    match the quiz always used); a known title whose every word appears in
    `title` ("Analytics Engineer / Power BI Developer" -> "Analytics
    Engineer"); then the known title sharing the most field words, with role
    nouns counting half and style words not at all. Ties go to the shorter
    title, then ontology order.
    """
    raw = (title or "").strip()
    if not raw:
        return None
    known = _known_titles()
    for k in known:
        if k.lower() == raw.lower():
            return k
    hits = search_ontology(raw)["roles"]
    if hits:
        return hits[0]["role"]

    q_tokens = _title_tokens(raw)
    q_set = set(q_tokens)
    if not q_set:
        return None

    contained = [k for k in known if _title_tokens(k) and set(_title_tokens(k)) <= q_set]
    if contained:
        return max(contained, key=lambda k: len(_title_tokens(k)))

    best, best_key = None, None
    for i, k in enumerate(known):
        k_tokens = set(_title_tokens(k))
        shared = q_set & k_tokens
        field_words = {w for w in shared if w not in _DESCRIPTOR_WORDS and w not in _ROLE_NOUNS}
        if not field_words:
            continue
        score = len(field_words) + 0.5 * len(shared & _ROLE_NOUNS)
        key = (score, -len(k_tokens), -i)
        if best_key is None or key > best_key:
            best, best_key = k, key
    return best


# Words a job board title does not use but the archetype prompt does
# ("Zero-to-One", "High-Volume", "Systems Builder", "AI-Native ... Hire"). A
# title with none of them is left alone even when the ontology lacks it:
# "Marketing Intern" and "Data Science Intern" are real, only unlisted.
_COINED_MARKERS = frozenset({
    "founding", "high", "volume", "zero", "one", "first", "led", "native",
    "systems", "system", "solutions", "builder", "operator", "generalist",
    "hybrid", "hire", "focused", "driven", "oriented", "end", "full", "cycle",
    "hygienist",
})


def looks_coined(title: str) -> bool:
    """A title nobody holds (OP-N09): not a known title, and carrying a word
    only a coined archetype would use."""
    if is_known_title(title):
        return False
    return bool(set(_title_tokens(title)) & _COINED_MARKERS)


def real_title_for(role: str) -> str | None:
    """`role` if it is a title people hold, else its nearest real title, else
    None (a coined title with nothing close)."""
    role = (role or "").strip()
    if not role:
        return None
    if not looks_coined(role):
        return role
    return nearest_real_title(role)


def to_real_titles(roles: list[str]) -> list[str]:
    """Roles as Apollo should search them (OP-N09).

    A real title is kept exactly as the student chose it. A coined one (the
    quiz archetype, an LLM invention) is replaced by its nearest real title.
    A coined one with nothing close is kept: these are OR lists, so it cannot
    zero the search on its own. Deduplicated, order kept.
    """
    out: list[str] = []
    seen: set[str] = set()
    for role in roles or []:
        role = (role or "").strip()
        if not role:
            continue
        mapped = real_title_for(role) or role
        if mapped.lower() not in seen:
            seen.add(mapped.lower())
            out.append(mapped)
    return out
