"""
CandidateProfiler — Resume Parser (Fast Mode)
Extracts text from PDF/DOCX resumes. NO LLM summarization during upload.
The raw text is passed directly to the chat agent for contextual understanding.
"""

import base64
import fitz  # PyMuPDF
import logging
import re
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Threshold below which we treat PyMuPDF's text extraction as a failure and
# fall back to OCR. Canva/scanned/image-PDFs typically yield 0–10 chars even
# when the resume looks fully populated.
_OCR_FALLBACK_THRESHOLD = 50
# Hard cap on pages we OCR — most resumes are 1–2 pages, anything past 5
# is almost certainly not a resume and would burn vision tokens.
_OCR_MAX_PAGES = 5
# Render PDF pages at ~150 DPI for OCR (2.0x zoom on default 72 DPI). Higher
# is more accurate but pushes vision input tokens up fast.
_OCR_PAGE_ZOOM = 2.0


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract raw text from a PDF.

    Fast path: PyMuPDF reads the embedded text layer (< 1 second).
    Fallback: Azure OpenAI vision OCR when the text layer is empty/sparse —
    handles image-based PDFs (Canva exports, scans, screenshot-to-PDF).
    """
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        text_parts = []
        for page in doc:
            text_parts.append(page.get_text())
        num_pages = len(doc)
        text = "\n".join(text_parts).strip()
        logger.info(f"PDF parsed (text layer): {len(text)} chars from {num_pages} pages")

        if len(text) >= _OCR_FALLBACK_THRESHOLD:
            doc.close()
            return text

        # Text layer empty/sparse — almost certainly an image-based PDF.
        logger.info(
            f"PDF text layer below threshold ({len(text)} < {_OCR_FALLBACK_THRESHOLD}); "
            f"falling back to Azure vision OCR."
        )
        try:
            ocr_text = _ocr_pdf_via_azure_vision(doc, num_pages)
        finally:
            doc.close()

        if ocr_text and len(ocr_text.strip()) >= _OCR_FALLBACK_THRESHOLD:
            logger.info(f"OCR recovered {len(ocr_text)} chars from image-based PDF")
            return ocr_text.strip()
        # OCR also yielded nothing useful — return whatever text layer we had.
        return text
    except Exception as e:
        logger.error(f"Error parsing PDF: {e}")
        raise ValueError(f"Could not parse PDF file: {str(e)}")


def _ocr_pdf_via_azure_vision(doc, num_pages: int) -> str:
    """Render PDF pages to PNGs and ask gpt-4o (vision) to extract text.

    Uses the same Azure OpenAI deployment the rest of the codebase uses.
    Caps page count to keep vision token cost bounded.
    """
    from core.config import settings

    endpoint = (settings.AZURE_OPENAI_ENDPOINT or "").rstrip("/")
    api_version = settings.AZURE_OPENAI_API_VERSION
    # gpt-4o-mini is vision-capable and ~5x cheaper than gpt-4o; fine for OCR
    deployment = getattr(settings, "AZURE_OPENAI_FAST_DEPLOYMENT", None) or "gpt-4o-mini"
    api_key = settings.AZURE_OPENAI_KEY
    if not all([endpoint, api_version, deployment, api_key]):
        logger.warning("Azure OpenAI config missing — cannot run vision OCR fallback")
        return ""

    pages_to_ocr = min(num_pages, _OCR_MAX_PAGES)
    image_blocks = []
    for i in range(pages_to_ocr):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=fitz.Matrix(_OCR_PAGE_ZOOM, _OCR_PAGE_ZOOM))
        png_bytes = pix.tobytes("png")
        b64 = base64.b64encode(png_bytes).decode("ascii")
        image_blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    prompt_text = (
        "This is a resume. Extract ALL visible text exactly as written — "
        "names, contact info, headers, bullets, dates, every line. "
        "Preserve reading order. Do NOT summarise, paraphrase, or add commentary. "
        "Output the raw text only."
    )
    payload = {
        "messages": [{
            "role": "user",
            "content": [{"type": "text", "text": prompt_text}, *image_blocks],
        }],
        "temperature": 0.0,
        "max_tokens": 4000,
    }
    try:
        resp = requests.post(
            url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
    except Exception as e:
        logger.error(f"Azure vision OCR request failed: {e}")
        return ""

    if not resp.ok:
        logger.error(f"Azure vision OCR HTTP {resp.status_code}: {resp.text[:300]}")
        return ""

    data = resp.json() or {}
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        logger.error(f"Azure vision OCR returned unexpected shape: {str(data)[:300]}")
        return ""
    return content


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
