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
            "Data Scientist", "ML Engineer", "AI Engineer", "Research Scientist",
            "NLP Engineer", "Computer Vision Engineer"
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
