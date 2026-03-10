"""
CandidateProfiler — Resume Parser (Fast Mode)
Extracts text from PDF/DOCX resumes. NO LLM summarization during upload.
The raw text is passed directly to the chat agent for contextual understanding.
"""

import fitz  # PyMuPDF
import logging
import re

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract raw text from a PDF file using PyMuPDF (< 1 second)."""
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        num_pages = len(doc)
        doc.close()
        text = "\n".join(text_parts).strip()
        logger.info(f"PDF parsed: {len(text)} chars from {num_pages} pages")
        return text
    except Exception as e:
        logger.error(f"Error parsing PDF: {e}")
        raise ValueError(f"Could not parse PDF file: {str(e)}")


def extract_text_from_docx(file_bytes: bytes) -> str:
    """Extract raw text from a DOCX file."""
    try:
        import io
        import zipfile
        import xml.etree.ElementTree as ET

        with io.BytesIO(file_bytes) as f:
            with zipfile.ZipFile(f) as z:
                if "word/document.xml" not in z.namelist():
                    raise ValueError("Invalid DOCX file")
                xml_content = z.read("word/document.xml")
                tree = ET.fromstring(xml_content)
                texts = []
                for t_elem in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
                    if t_elem.text:
                        texts.append(t_elem.text)
                text = " ".join(texts).strip()
                logger.info(f"DOCX parsed: {len(text)} chars")
                return text
    except Exception as e:
        logger.error(f"Error parsing DOCX: {e}")
        raise ValueError(f"Could not parse DOCX file: {str(e)}")


def quick_extract_preview(raw_text: str) -> dict:
    """
    Fast regex-based extraction for the upload preview card.
    No LLM call. Returns basic fields in < 10ms.
    """
    preview = {
        "name": None,
        "email": None,
        "phone": None,
        "skills": [],
        "char_count": len(raw_text),
        "summary_text": None,
    }

    # Extract email
    email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', raw_text)
    if email_match:
        preview["email"] = email_match.group()

    # Extract phone
    phone_match = re.search(r'[\+]?[\d\s\-\(\)]{10,15}', raw_text)
    if phone_match:
        preview["phone"] = phone_match.group().strip()

    # Extract name — smarter heuristic
    # Skip common section headers and look for a line that looks like a person's name
    section_headers = {
        "resume", "curriculum vitae", "cv", "profile", "summary",
        "experience", "education", "skills", "projects", "contact",
        "brand", "product", "objective", "about", "professional",
        "personal", "career", "work", "portfolio", "references",
    }
    # Words that indicate the line is an organization name, NOT a person's name
    org_keywords = {
        "office", "founders", "technologies", "solutions", "pvt", "ltd",
        "inc", "llc", "corp", "group", "labs", "studio", "studios",
        "consulting", "ventures", "capital", "media", "digital",
        "academy", "institute", "university", "college", "school",
        "foundation", "services", "associates", "partners", "agency",
        "enterprises", "limited", "private", "company", "intern",
        "internship", "trainee", "assistant", "manager", "analyst",
        "developer", "engineer", "designer", "marketing", "freelance",
    }
    lines = [l.strip() for l in raw_text.split('\n') if l.strip()]
    for line in lines[:10]:  # Check first 10 lines
        # Skip if too long, contains @, http, or looks like a section header
        if len(line) > 40 or '@' in line or 'http' in line.lower():
            continue
        # Skip lines with special chars that indicate headers (&, |, :, -, numbers)
        if re.search(r'[&|:]', line) or re.match(r'^[\d\-\.\)\#]', line):
            continue
        # Skip if any word matches a section header or org keyword
        words_lower = set(line.lower().split())
        if words_lower & section_headers:
            continue
        if words_lower & org_keywords:
            continue
        # Name should be 2-4 words, mostly alphabetic
        words = line.split()
        if 2 <= len(words) <= 4 and all(w.isalpha() or w == '.' for w in words):
            preview["name"] = line
            break

    # Extract skills using word-boundary matching to avoid false positives
    skill_keywords = [
        "Python", "JavaScript", "Java", "SQL", "Excel", "React", "Node.js",
        "AWS", "Docker", "Kubernetes", "Git", "Machine Learning", "Data Analysis",
        "Figma", "Canva", "Photoshop", "Google Analytics", "SEO", "SEM",
        "Tableau", "Power BI", "TypeScript", "MongoDB",
        "PostgreSQL", "MySQL", "Redis", "GraphQL", "REST API", "Agile", "Scrum",
        "Product Management", "Marketing", "Sales", "Finance", "Accounting",
        "HTML", "CSS", "C++", "C#", "Swift", "Kotlin", "Flutter", "Django",
        "FastAPI", "Spring", "Angular", "Vue.js", "Pandas", "NumPy", "TensorFlow",
        "PyTorch", "Spark", "Hadoop", "Snowflake", "Airflow", "dbt",
        "Salesforce", "HubSpot", "Jira", "Notion", "Confluence",
        "SAP", "QuickBooks", "Bloomberg Terminal",
        "Lead generation", "Growth strategy", "Branding",
        "Stakeholder management", "Strategic consulting",
        "Communication", "Leadership", "Problem solving", "Research",
        "Negotiation", "Presentation", "Project Management",
        "Video Editing", "Content Writing", "Graphic Design",
        "Social Media", "Public Relations", "Event Management",
    ]
    for skill in skill_keywords:
        pattern = r'\b' + re.escape(skill) + r'\b'
        if re.search(pattern, raw_text, re.IGNORECASE):
            preview["skills"].append(skill)

    preview["skills"] = preview["skills"][:15]  # Cap at 15

    # Extract education from resume text
    education = []
    edu_patterns = [
        r'(?i)\b(B\.?Tech|B\.?E|B\.?Sc|B\.?Com|B\.?A|BBA|BCA|M\.?Tech|M\.?E|M\.?Sc|M\.?Com|M\.?A|MBA|MCA|Ph\.?D|Diploma)\b',
    ]
    for pat in edu_patterns:
        matches = re.findall(pat, raw_text)
        for m in matches:
            if m not in education:
                education.append(m)
    preview["education"] = education[:3]

    # Extract years of experience from resume text
    exp_match = re.search(r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience|exp)', raw_text, re.IGNORECASE)
    preview["years_experience"] = int(exp_match.group(1)) if exp_match else None

    # Generate a brief summary
    if preview["name"]:
        preview["summary_text"] = f"Resume for {preview['name']} ({preview['char_count']} characters extracted)"
    else:
        preview["summary_text"] = f"Resume parsed ({preview['char_count']} characters extracted)"

    return preview


def parse_resume(file_bytes: bytes, filename: str) -> tuple[str, dict]:
    """
    Fast resume parsing: extract text + regex preview. No LLM call.
    Returns (raw_text, preview_dict).
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext == "pdf":
        raw_text = extract_text_from_pdf(file_bytes)
    elif ext in ("docx", "doc"):
        raw_text = extract_text_from_docx(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: .{ext}. Please upload a PDF or DOCX file.")

    if not raw_text or len(raw_text.strip()) < 50:
        raise ValueError("The uploaded file appears to be empty or contains too little text to parse.")

    preview = quick_extract_preview(raw_text)
    return raw_text, preview
