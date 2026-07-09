"""Harvest stage: collect the raw opportunity pool BEFORE any reasoning.

Fixed per-source query templates (run 36: 16 of the model's 28 improvised
queries returned <=2 results; the productive broad forms below were all
derivable upfront), executed in parallel, results persisted as raw items.
The LLM sees nothing here; budget is enforced by the orchestrator's Budget.

Sources: linkedin_jobs (guest index, free), unstop (free),
ctx_li_posts / ctx_x / ctx_boards (Context.dev, ~1 credit per query).
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.bob.pipeline import state

logger = logging.getLogger(__name__)

ALL_SOURCES = ("linkedin_jobs", "linkedin_posts", "x", "unstop", "boards")
_BOARD_DOMAINS = ["internshala.com", "naukri.com", "wellfound.com", "cutshort.io"]
_MAX_WORKERS = 6


def _freshness(days: int) -> str:
    if days <= 1:
        return "last_24_hours"
    if days <= 7:
        return "last_week"
    if days <= 31:
        return "last_month"
    return "last_year"


def build_query_plan(params: dict, ring: int = 0) -> list[dict]:
    """Deterministic query matrix. Ring 0 = core sweep; ring 1 widens with
    alternate phrasings and board sources. The model chooses only the mandate
    keywords upstream; it never improvises query syntax."""
    kws = [k.strip() for k in (params.get("keywords") or []) if str(k).strip()][:8]
    loc = (params.get("location") or "India").strip()
    city = loc.split(",")[0].strip()
    days = int(params.get("freshness_days") or 7)
    fresh = _freshness(days)
    sources = [s for s in (params.get("sources") or ALL_SOURCES) if s in ALL_SOURCES]
    plan: list[dict] = []

    for kw in kws:
        if "linkedin_jobs" in sources:
            plan.append({"source": "linkedin_jobs", "kind": "guest",
                         "keywords": kw, "location": loc, "hours_back": days * 24, "limit": 20})
        if "unstop" in sources:
            plan.append({"source": "unstop", "kind": "unstop", "keywords": kw,
                         "location": city, "limit": 20})
        if "linkedin_posts" in sources:
            plan.append({"source": "ctx_li_posts", "kind": "ctx", "freshness": fresh,
                         "query": f'site:linkedin.com/posts "{kw}" {city} hiring'})
            plan.append({"source": "ctx_li_posts", "kind": "ctx", "freshness": fresh,
                         "query": f'site:linkedin.com/posts "we\'re hiring" "{kw}" India'})
        if "x" in sources:
            plan.append({"source": "ctx_x", "kind": "ctx", "freshness": fresh,
                         "query": f'site:x.com "{kw}" hiring India'})
        if ring >= 1:
            if "linkedin_posts" in sources:
                plan.append({"source": "ctx_li_posts", "kind": "ctx", "freshness": fresh,
                             "query": f'site:linkedin.com/posts "{kw}" internship {city}'})
            if "boards" in sources:
                plan.append({"source": "ctx_boards", "kind": "ctx", "freshness": fresh,
                             "query": f"{kw} internship {city} stipend",
                             "include_domains": _BOARD_DOMAINS})
    # De-duplicate identical specs (overlapping keywords produce repeats).
    seen, out = set(), []
    for spec in plan:
        key = (spec["source"], spec.get("query") or spec.get("keywords"), spec.get("location", ""))
        if key not in seen:
            seen.add(key)
            out.append(spec)
    return out


def _run_guest(spec: dict) -> list[dict]:
    from services.bob import livecheck
    jobs = livecheck.search_linkedin_jobs(
        spec["keywords"], spec["location"],
        hours_back=spec.get("hours_back", 0), limit=spec.get("limit", 20),
    )
    return [{
        "url": j["url"], "title": f'{j["title"]} at {j["company"]}',
        "description": f'{j["location"]} | posted {j["posted"] or "n/a"}',
        "markdown": (
            f'LIVE LINKEDIN JOB CARD (guest index, current)\n'
            f'title: {j["title"]}\ncompany: {j["company"]}\nlocation: {j["location"]}\n'
            f'posted: {j["posted"] or "n/a"}\nurl: {j["url"]}'
        ),
    } for j in jobs]


def _run_unstop(spec: dict) -> list[dict]:
    from services.bob import unstop
    jobs = unstop.search_internships(spec["keywords"], spec["location"], limit=spec.get("limit", 20))
    return [{
        "url": j["url"], "title": f'{j["title"]} at {j["company"]}',
        "description": f'{j.get("location") or ""} | stipend {j.get("stipend") or "n/a"}',
        "markdown": (
            f'OPEN UNSTOP INTERNSHIP (oppstatus=open, structured fields trustworthy)\n'
            f'title: {j["title"]}\ncompany: {j["company"]}\nlocation: {j.get("location") or "n/a"}\n'
            f'stipend: {j.get("stipend") or "not stated"}\napply by: {j.get("deadline") or "n/a"}\n'
            f'eligible: {j.get("eligibility") or "n/a"}\nurl: {j["url"]}'
        ),
    } for j in jobs]


def _run_ctx(spec: dict) -> tuple[list[dict], int]:
    """Context.dev query in its own DB session (sessions are not thread-safe;
    the client needs one for its evidence cache)."""
    from database.session import SessionLocal
    from services.bob import contextdev_client as ctx
    db = SessionLocal()
    try:
        res = ctx.web_search(
            db, query=spec["query"], num_results=10, freshness=spec.get("freshness"),
            country="IN", include_domains=spec.get("include_domains"),
        )
        return res.get("results") or [], int(res.get("credits_consumed") or 0)
    finally:
        db.close()


def run(db, run_id: int, chat_id: int, params: dict, budget, ring: int = 0) -> dict:
    """Execute the plan in parallel; persist everything. Free sources always
    run; ctx queries are admitted only while credits remain."""
    plan = build_query_plan(params, ring)
    ctx_allowed = budget.credits_left
    admitted = []
    for spec in plan:
        if spec["kind"] == "ctx":
            if ctx_allowed <= 0:
                continue
            ctx_allowed -= 1  # worst case 1 credit per 10-result query
        admitted.append(spec)

    new = dup = credits = errors = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {}
        for spec in admitted:
            fn = {"guest": _run_guest, "unstop": _run_unstop, "ctx": _run_ctx}[spec["kind"]]
            futures[pool.submit(fn, spec)] = spec
        for fut in as_completed(futures):
            spec = futures[fut]
            label = spec.get("query") or f'{spec.get("keywords")} @ {spec.get("location")}'
            try:
                out = fut.result()
            except Exception as e:
                errors += 1
                state.push_event(db, run_id, "search", f"harvest error: {label[:60]}",
                                 f"{e.__class__.__name__}: {e}"[:300])
                continue
            items, spent = (out if isinstance(out, tuple) else (out, 0))
            credits += spent
            budget.spend(spent)
            n, d = state.insert_raw_items(db, chat_id, run_id, spec["source"], label, items)
            new += n
            dup += d
            state.push_event(db, run_id, "search", f"[{spec['source']}] {label[:70]}",
                             f"{len(items)} results, {n} new", credits=spent)
    return {"queries": len(admitted), "planned": len(plan), "new_items": new,
            "duplicates": dup, "credits": credits, "errors": errors, "ring": ring}
