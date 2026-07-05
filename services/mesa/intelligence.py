"""Mesa Signal Engine — enrichment + reasoning layer (all FREE, no Apollo).

Composes existing in-house free services to add CONTEXT and JUDGEMENT on top of
the deterministic title-signals in signals.py:

  - Company facts       via company_intelligence.llm_company_research.research_company
                        (LLM + Bing web search, "zero Apollo credits") -> funding,
                        stage, founder, domain, headcount.
  - News-based signals  via hiring_signals.news_search.get_company_news (Google
                        News RSS, free) -> fresh_funding + leader_departure.
  - Analyst brief       via shared.azure_client.generate_json (LLM) -> confidence,
                        signal narrative (the arc), outreach opener, kill-signal check.

Everything is lazy-imported and wrapped: if Azure/network is unavailable the
engine degrades to the deterministic signals and the endpoint never breaks.
Enrichment is expensive (LLM + web per company) so it runs in the BACKGROUND for
the top-N companies and is cached in-process, keyed by (search_id, company).
"""

import asyncio
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# in-process cache + status (per pod; fine for interactive use, no DB migration)
_CACHE: dict = {}
_STATUS: dict = {}
_LOCK = threading.Lock()

# news-derived families get folded into the confluence score when present
NEWS_WEIGHT = {"fresh_funding": 20, "leader_departure": 30}
NEWS_LABEL = {
    "fresh_funding": "Fresh funding (budget just unlocked)",
    "leader_departure": "Sales leader just departed (seat open now)",
}

_FUNDING_RE = re.compile(r"\b(raises?|raised|funding|series [a-e]|seed round|bags?|secures?|\$\d|₹\d|mn\b|million|crore)\b", re.I)
_DEPART_RE = re.compile(r"\b(head of sales|vp sales|vp of sales|chief revenue|cro|sales director|revenue officer|gtm)\b", re.I)
_DEPART_VERB = re.compile(r"\b(steps down|resign|resigns|resigned|departs?|left|leaving|exits?|quits?|to join|joins|moves? to|new role)\b", re.I)


def _norm(name: str) -> str:
    n = re.sub(r"\(.*?\)", " ", (name or "").lower())
    n = re.sub(r"\b(ai|technologies|technology|labs|inc|india|pvt|ltd|the|app|cloud|global|hq)\b", " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def _classify_news(headlines: list[str]) -> dict:
    """Pure: derive fresh_funding / leader_departure families from news headlines."""
    fams: dict = {}
    for h in headlines:
        if "fresh_funding" not in fams and _FUNDING_RE.search(h):
            fams["fresh_funding"] = h
        if "leader_departure" not in fams and _DEPART_RE.search(h) and _DEPART_VERB.search(h):
            fams["leader_departure"] = h
    return fams


def _fetch_news(company: str) -> list[str]:
    try:
        from services.hiring_signals.news_search import get_company_news
        items = asyncio.run(get_company_news(company, max_results=8))
        return [i.get("headline", "") for i in items if i.get("headline")]
    except Exception as e:  # noqa: BLE001
        logger.info("[MESA] news fetch failed for %s: %s", company, e)
        return []


def _facts(company: str) -> dict | None:
    try:
        from services.company_intelligence.llm_company_research import research_company
        return research_company(company)
    except Exception as e:  # noqa: BLE001
        logger.info("[MESA] company research failed for %s: %s", company, e)
        return None


_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "confidence": {"type": "integer"},
        "verdict": {"type": "string", "enum": ["strong", "worth a look", "weak", "skip"]},
        "narrative": {"type": "string"},
        "outreach_opener": {"type": "string"},
        "kill_signal": {"type": "boolean"},
    },
    "required": ["confidence", "verdict", "narrative", "outreach_opener", "kill_signal"],
}
_BRIEF_SYS = (
    "You are a sharp talent-scout analyst. For a candidate/recruiter targeting a SENIOR "
    "B2B sales / GTM leadership placement (needs a company that can pay a senior fixed comp), "
    "judge one company from the signals, facts and news provided. "
    "verdict 'skip' + kill_signal=true if they clearly just filled the sales-leadership seat, "
    "froze hiring, or cannot afford a senior leader. narrative = the signal ARC in one or two lines "
    "(e.g. 'raised in Feb, added Contact-Sales in April, VP left in May, now posting AEs'). "
    "outreach_opener = one specific first-message line tied to the strongest signal. Be concrete, no fluff."
)


def _brief(company: str, families: list[str], facts: dict | None, news: list[str]) -> dict | None:
    try:
        from services.shared.azure_client import generate_json
        ctx = {
            "company": company,
            "signals_firing": families,
            "company_facts": facts or "unknown",
            "recent_news_headlines": news[:6],
        }
        import json as _json
        prompt = "Company signal packet:\n" + _json.dumps(ctx, ensure_ascii=False, indent=1)
        return generate_json(prompt, _BRIEF_SCHEMA, temperature=0.2, system_prompt=_BRIEF_SYS)
    except Exception as e:  # noqa: BLE001
        logger.info("[MESA] analyst brief failed for %s: %s", company, e)
        return None


def enrich_one(company: str, families: list[str]) -> dict:
    """Full enrichment for one company: news families + facts + analyst brief.
    Never raises. Returns a partial dict on any failure."""
    news = _fetch_news(company)
    news_fams = _classify_news(news)
    facts = _facts(company)
    all_fams = list(families) + list(news_fams.keys())
    brief = _brief(company, all_fams, facts, news)
    return {
        "news_families": news_fams,          # {family: evidence_headline}
        "facts": facts,                      # LLM+Bing company facts or None
        "brief": brief,                      # analyst brief or None
        "news_headlines": news[:5],
    }


def _run_enrich(search_id: int, companies: list[dict]):
    total = len(companies)
    with _LOCK:
        _STATUS[search_id] = {"running": True, "done": 0, "total": total}

    def work(c):
        try:
            enr = enrich_one(c["company"], c.get("families", []))
        except Exception as e:  # noqa: BLE001
            logger.warning("[MESA] enrich_one crashed for %s: %s", c.get("company"), e)
            enr = {"news_families": {}, "facts": None, "brief": None, "news_headlines": []}
        with _LOCK:
            _CACHE[(search_id, _norm(c["company"]))] = enr
            _STATUS[search_id]["done"] += 1
        return True

    # bounded parallelism — external LLM/web per company
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(work, companies))
    with _LOCK:
        _STATUS[search_id]["running"] = False
    logger.info("[MESA] enrichment done for search %s (%d companies)", search_id, total)


def start_enrichment(search_id: int, companies: list[dict]):
    """Kick off background enrichment for the given (already ranked) companies."""
    threading.Thread(target=_run_enrich, args=(search_id, companies), daemon=True).start()


def enrichment_status(search_id: int) -> dict:
    with _LOCK:
        return dict(_STATUS.get(search_id, {"running": False, "done": 0, "total": 0}))


def attach_enrichment(search_id: int, companies: list[dict]) -> list[dict]:
    """Merge cached enrichment into ranked companies and RE-SCORE with news
    families (fresh_funding / leader_departure). Companies with no cache pass
    through unchanged."""
    out = []
    for c in companies:
        enr = None
        with _LOCK:
            enr = _CACHE.get((search_id, _norm(c["company"])))
        if not enr:
            out.append(c)
            continue
        news_fams = enr.get("news_families") or {}
        merged = dict(c)
        if news_fams:
            fams = sorted(set(c.get("families", [])) | set(news_fams.keys()))
            merged["families"] = fams
            merged["signals"] = c.get("signals", []) + [NEWS_LABEL[f] for f in news_fams if f in NEWS_LABEL]
            merged["n_families"] = len([f for f in fams])
            merged["confluence"] = merged["n_families"] >= 2
            bonus = sum(NEWS_WEIGHT.get(f, 0) for f in news_fams)
            conf = 25 if merged["n_families"] >= 3 else (15 if merged["n_families"] >= 2 else 0)
            merged["score"] = min(100, c["score"] + bonus + max(0, conf - (15 if c["confluence"] else 0)))
        merged["facts"] = enr.get("facts")
        merged["brief"] = enr.get("brief")
        merged["news_headlines"] = enr.get("news_headlines")
        merged["news_evidence"] = news_fams
        merged["enriched"] = True
        out.append(merged)
    out.sort(key=lambda x: (-x["score"], x["company"]))
    return out
