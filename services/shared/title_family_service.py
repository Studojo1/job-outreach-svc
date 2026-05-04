"""Title family service.

Provides function-specific title dictionaries using REAL titles
that people actually use on LinkedIn/Apollo. No fabricated compounds.
"""

from typing import Dict, List

from core.logger import get_logger

logger = get_logger(__name__)

# Each title is tagged with a seniority keyword for downstream filtering.
# These are REAL titles commonly found in Apollo/LinkedIn — no fabricated compounds.
_TITLE_FAMILIES: Dict[str, Dict[str, str]] = {
    "product": {
        "Product Manager": "manager",
        "Senior Product Manager": "senior",
        "Lead Product Manager": "lead",
        "Principal Product Manager": "principal",
        "Group Product Manager": "group",
        "Head of Product": "head",
        "Director of Product": "director",
        "VP of Product": "vp",
        "Associate Product Manager": "junior",
        "Technical Program Manager": "manager",
        "Program Manager": "manager",
    },
    "data": {
        "Data Analyst": "junior",
        "Senior Data Analyst": "senior",
        "Data Scientist": "manager",
        "Senior Data Scientist": "senior",
        "Analytics Manager": "manager",
        "Data Science Manager": "manager",
        "Data Engineering Manager": "manager",
        "Head of Data": "head",
        "Head of Analytics": "head",
        "Director of Data": "director",
        "Director of Analytics": "director",
        "VP of Data": "vp",
        "BI Analyst": "junior",
        "Business Intelligence Manager": "manager",
    },
    "engineering": {
        "Software Engineer": "junior",
        "Senior Software Engineer": "senior",
        "Staff Engineer": "senior",
        "Principal Engineer": "principal",
        "Engineering Manager": "manager",
        "Senior Engineering Manager": "senior",
        "Tech Lead": "lead",
        "Engineering Lead": "lead",
        "Head of Engineering": "head",
        "Director of Engineering": "director",
        "VP of Engineering": "vp",
        "CTO": "vp",
        "Development Manager": "manager",
        "Software Development Manager": "manager",
    },
    "business": {
        "Business Analyst": "junior",
        "Senior Business Analyst": "senior",
        "Operations Manager": "manager",
        "Business Operations Manager": "manager",
        "Strategy Manager": "manager",
        "Head of Operations": "head",
        "Head of Strategy": "head",
        "Director of Operations": "director",
        "Director of Strategy": "director",
        "Chief of Staff": "head",
        "VP of Operations": "vp",
        "General Manager": "manager",
    },
    "marketing": {
        "Marketing Manager": "manager",
        "Senior Marketing Manager": "senior",
        "Growth Marketing Manager": "manager",
        "Digital Marketing Manager": "manager",
        "Content Marketing Manager": "manager",
        "Head of Marketing": "head",
        "Head of Growth": "head",
        "Director of Marketing": "director",
        "VP of Marketing": "vp",
        "Marketing Lead": "lead",
        "Growth Manager": "manager",
        "Brand Manager": "manager",
    },
    "design": {
        "UX Designer": "junior",
        "Senior UX Designer": "senior",
        "Product Designer": "junior",
        "Senior Product Designer": "senior",
        "Design Lead": "lead",
        "Head of Design": "head",
        "Director of Design": "director",
        "UX Research Manager": "manager",
        "Design Manager": "manager",
    },
    "security": {
        "Security Engineer": "junior",
        "Senior Security Engineer": "senior",
        "Security Analyst": "junior",
        "Head of Security": "head",
        "CISO": "vp",
        "Director of Security": "director",
        "Security Manager": "manager",
        "Information Security Manager": "manager",
    },
    "blockchain": {
        "Blockchain Developer": "junior",
        "Senior Blockchain Developer": "senior",
        "Smart Contract Developer": "junior",
        "Web3 Developer": "junior",
        "Blockchain Engineer": "junior",
        "Engineering Manager": "manager",
        "Head of Engineering": "head",
        "CTO": "vp",
        "Tech Lead": "lead",
        "Director of Engineering": "director",
        "VP of Engineering": "vp",
    },
    "civil_construction": {
        "Civil Engineer": "junior",
        "Senior Civil Engineer": "senior",
        "Structural Engineer": "junior",
        "Site Engineer": "junior",
        "Project Engineer": "junior",
        "Construction Manager": "manager",
        "Project Manager": "manager",
        "Senior Project Manager": "senior",
        "Construction Project Manager": "manager",
        "Head of Construction": "head",
        "Director of Construction": "director",
        "Director of Engineering": "director",
        "VP of Construction": "vp",
        "Estimator": "junior",
        "Quantity Surveyor": "junior",
        "Civil Engineering Manager": "manager",
        "Engineering Manager": "manager",
    },
    "hr": {
        "HR Manager": "manager",
        "Senior HR Manager": "senior",
        "HR Business Partner": "manager",
        "People Operations Manager": "manager",
        "People Lead": "lead",
        "Talent Manager": "manager",
        "Talent Acquisition Manager": "manager",
        "Head of People": "head",
        "Head of HR": "head",
        "Head of Talent": "head",
        "Director of HR": "director",
        "Director of People": "director",
        "VP of HR": "vp",
        "VP of People": "vp",
        "CHRO": "vp",
        "Chief People Officer": "vp",
    },
    "writing_content": {
        "Content Manager": "manager",
        "Senior Content Manager": "senior",
        "Editor": "junior",
        "Senior Editor": "senior",
        "Editor in Chief": "head",
        "Managing Editor": "manager",
        "Editorial Director": "director",
        "Content Lead": "lead",
        "Head of Content": "head",
        "Director of Content": "director",
        "VP of Content": "vp",
        "Content Strategist": "junior",
        "Senior Content Strategist": "senior",
        "Copywriter": "junior",
        "Senior Copywriter": "senior",
    },
    "customer_success": {
        "Customer Success Manager": "manager",
        "Senior Customer Success Manager": "senior",
        "Customer Success Lead": "lead",
        "Head of Customer Success": "head",
        "Director of Customer Success": "director",
        "VP of Customer Success": "vp",
        "Customer Experience Manager": "manager",
        "Head of Customer Experience": "head",
        "Account Manager": "manager",
        "Senior Account Manager": "senior",
    },
    "cloud_ops": {
        "DevOps Engineer": "junior",
        "Senior DevOps Engineer": "senior",
        "DevOps Manager": "manager",
        "Site Reliability Engineer": "junior",
        "Senior Site Reliability Engineer": "senior",
        "SRE Manager": "manager",
        "Cloud Engineer": "junior",
        "Cloud Architect": "senior",
        "Cloud Engineering Manager": "manager",
        "Platform Engineer": "junior",
        "Platform Engineering Manager": "manager",
        "Head of Platform": "head",
        "Head of Infrastructure": "head",
        "Director of Infrastructure": "director",
        "Director of Platform": "director",
        "VP of Infrastructure": "vp",
    },
    "finance": {
        "Finance Manager": "manager",
        "Senior Finance Manager": "senior",
        "FP&A Manager": "manager",
        "Finance Lead": "lead",
        "Head of Finance": "head",
        "Director of Finance": "director",
        "VP of Finance": "vp",
        "CFO": "vp",
        "Controller": "manager",
        "Financial Controller": "manager",
        "Treasurer": "head",
        "Investment Manager": "manager",
        "Accounting Manager": "manager",
    },
    "legal": {
        "Legal Counsel": "junior",
        "Senior Legal Counsel": "senior",
        "Legal Manager": "manager",
        "Senior Counsel": "senior",
        "General Counsel": "head",
        "Head of Legal": "head",
        "Legal Director": "director",
        "Director of Legal": "director",
        "VP of Legal": "vp",
        "Chief Legal Officer": "vp",
    },
    "sales": {
        "Sales Manager": "manager",
        "Senior Sales Manager": "senior",
        "Account Executive": "junior",
        "Senior Account Executive": "senior",
        "Sales Lead": "lead",
        "Head of Sales": "head",
        "Director of Sales": "director",
        "VP of Sales": "vp",
        "CRO": "vp",
        "Chief Revenue Officer": "vp",
        "Sales Director": "director",
        "Account Manager": "manager",
        "Business Development Manager": "manager",
        "Head of Business Development": "head",
        "Director of Business Development": "director",
    },
    "operations": {
        "Operations Manager": "manager",
        "Senior Operations Manager": "senior",
        "Business Operations Manager": "manager",
        "Operations Lead": "lead",
        "Head of Operations": "head",
        "Director of Operations": "director",
        "VP of Operations": "vp",
        "COO": "vp",
        "Chief Operating Officer": "vp",
        "Supply Chain Manager": "manager",
        "Head of Supply Chain": "head",
        "Logistics Manager": "manager",
        "Head of Logistics": "head",
        "Procurement Manager": "manager",
    },
}


# ---------------------------------------------------------------------------
# Specializations — additive layer that injects modern/specific manager titles
# on top of the generic function family. Keyed by subdomain string.
# Inferred from candidate's role text via role_classifier_service.subdomain_for_role.
# Without specializations, an ML engineer gets generic "Data Analyst" titles —
# this is what drives the bad-quality leads (Operations Manager, Junior DS, etc.).
# ---------------------------------------------------------------------------
_SPECIALIZATIONS: Dict[str, Dict[str, str]] = {
    "ml_ai": {
        "Machine Learning Engineer": "junior",
        "Senior Machine Learning Engineer": "senior",
        "Staff Machine Learning Engineer": "senior",
        "Principal Machine Learning Engineer": "principal",
        "ML Engineering Manager": "manager",
        "AI Engineering Manager": "manager",
        "Applied AI Manager": "manager",
        "Head of Machine Learning": "head",
        "Head of AI": "head",
        "Head of Applied AI": "head",
        "Head of Applied Research": "head",
        "Director of Machine Learning": "director",
        "Director of AI": "director",
        "Director of Applied AI": "director",
        "VP of AI": "vp",
        "VP of Machine Learning": "vp",
        "VP of AI Engineering": "vp",
        "Chief AI Officer": "vp",
        "Applied Scientist": "junior",
        "Senior Applied Scientist": "senior",
        "Research Scientist": "junior",
        "Senior Research Scientist": "senior",
    },
    "data_engineering": {
        "Data Engineer": "junior",
        "Senior Data Engineer": "senior",
        "Staff Data Engineer": "senior",
        "Data Engineering Manager": "manager",
        "Head of Data Engineering": "head",
        "Director of Data Engineering": "director",
        "VP of Data Engineering": "vp",
    },
    "backend": {
        "Backend Engineer": "junior",
        "Senior Backend Engineer": "senior",
        "Staff Backend Engineer": "senior",
        "Backend Engineering Manager": "manager",
        "Head of Backend": "head",
        "Director of Backend": "director",
    },
    "frontend": {
        "Frontend Engineer": "junior",
        "Senior Frontend Engineer": "senior",
        "Frontend Engineering Manager": "manager",
        "Head of Frontend": "head",
        "Director of Frontend": "director",
    },
    "mobile": {
        "Mobile Engineer": "junior",
        "iOS Engineer": "junior",
        "Android Engineer": "junior",
        "Senior Mobile Engineer": "senior",
        "Mobile Engineering Manager": "manager",
        "Head of Mobile": "head",
    },
    "growth_marketing": {
        "Growth Marketing Manager": "manager",
        "Senior Growth Manager": "senior",
        "Head of Growth": "head",
        "VP of Growth": "vp",
        "Director of Growth": "director",
        "Growth Lead": "lead",
    },
    "brand_marketing": {
        "Brand Manager": "manager",
        "Senior Brand Manager": "senior",
        "Brand Director": "director",
        "Head of Brand": "head",
        "VP of Brand": "vp",
    },
    "performance_marketing": {
        "Performance Marketing Manager": "manager",
        "Head of Performance Marketing": "head",
        "Director of Performance Marketing": "director",
        "Paid Media Manager": "manager",
    },
    "product_marketing": {
        "Product Marketing Manager": "manager",
        "Senior Product Marketing Manager": "senior",
        "Head of Product Marketing": "head",
        "Director of Product Marketing": "director",
        "VP of Product Marketing": "vp",
    },
    "content_marketing": {
        "Content Marketing Manager": "manager",
        "Head of Content": "head",
        "Director of Content": "director",
    },
    "product_design": {
        "Product Designer": "junior",
        "Senior Product Designer": "senior",
        "Lead Product Designer": "lead",
        "Product Design Manager": "manager",
        "Head of Product Design": "head",
        "Director of Product Design": "director",
        "VP of Design": "vp",
    },
    "ux_research": {
        "UX Researcher": "junior",
        "Senior UX Researcher": "senior",
        "UX Research Manager": "manager",
        "Head of UX Research": "head",
        "Director of Research": "director",
    },
    "enterprise_sales": {
        "Enterprise Account Executive": "junior",
        "Senior Enterprise Account Executive": "senior",
        "Enterprise Sales Manager": "manager",
        "Head of Enterprise Sales": "head",
        "VP of Enterprise Sales": "vp",
    },
    "smb_sales": {
        "SMB Account Executive": "junior",
        "Senior SMB Account Executive": "senior",
        "SMB Sales Manager": "manager",
        "Head of SMB Sales": "head",
    },
    "appsec": {
        "Application Security Engineer": "junior",
        "Senior Application Security Engineer": "senior",
        "Head of Application Security": "head",
        "Director of Application Security": "director",
    },
    "biz_ops": {
        "Business Operations Manager": "manager",
        "Senior Business Operations Manager": "senior",
        "Head of Business Operations": "head",
        "Director of Business Operations": "director",
        "VP of Business Operations": "vp",
        "BizOps Lead": "lead",
    },
}


def get_title_family(function: str, subdomain: str | None = None) -> Dict[str, str]:
    """Return the title → seniority mapping for a function category.

    When ``subdomain`` is provided AND matches a known specialization key
    (see ``_SPECIALIZATIONS``), specialized hiring-manager titles are merged
    on top of the generic function family. This makes the generated Apollo
    title list specific to (e.g.) ML/AI rather than generic "data".

    Args:
        function: Function category name.
        subdomain: Optional specialization key (e.g. "ml_ai", "growth_marketing",
            "product_design"). Inferred from the candidate's target role via
            ``role_classifier_service.subdomain_for_role``.

    Returns:
        Dict mapping title strings to their seniority keyword.
        Falls back to ``"business"`` if the function is unknown.
    """
    base = dict(_TITLE_FAMILIES.get(function, _TITLE_FAMILIES["business"]))
    spec = _SPECIALIZATIONS.get((subdomain or "").lower()) if subdomain else None
    if spec:
        base.update(spec)  # specialized titles take precedence
        logger.info(
            "Title family for '%s' + spec '%s': %d total titles (%d specialized)",
            function, subdomain, len(base), len(spec),
        )
    else:
        logger.info("Title family for '%s' (no spec): %d titles", function, len(base))
    return base


def filter_titles_by_seniority(
    title_family: Dict[str, str], seniority_levels: List[str]
) -> List[str]:
    """Filter titles to only those matching the desired seniority levels.

    Args:
        title_family: Dict of title → seniority keyword.
        seniority_levels: Allowed seniority keywords.

    Returns:
        List of matching title strings.
    """
    seniority_set = set(s.lower() for s in seniority_levels)
    matched = [
        title
        for title, seniority in title_family.items()
        if seniority.lower() in seniority_set
    ]
    logger.info(
        "Filtered %d titles down to %d matching seniority levels %s",
        len(title_family), len(matched), seniority_levels,
    )
    return matched