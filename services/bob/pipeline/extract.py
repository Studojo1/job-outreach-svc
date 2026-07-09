"""Extraction stage: raw item text -> 0..N structured opportunities.

The decisions run 36 got wrong happen HERE, adjacent to the text, in one
batched pass per few items (not 20 turns later against a LeadsForge list):
  - multi-role and aggregator posts FAN OUT into one opportunity per
    company+role (the SDE-Jobs post was seen 4x and yielded 1 of 8 roles);
  - apply channels (priya@sherlock.sh) and author identity/affiliation
    (Afreen Naz, Recruitment Manager; Ishan Sharma, Founder & CTO) are
    captured as structured fields the contact stage consumes first.

Code, not the model, assigns evidence_url (the item's own URL) — the model
transcribing URLs is how garbage links used to enter tables.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.bob.pipeline import llm, state
from services.bob.textrails import PLACEHOLDER_COMPANY

logger = logging.getLogger(__name__)

_BATCH = 5              # items per LLM call (~4k chars each)
_MAX_OPPS_PER_ITEM = 10
_LLM_WORKERS = 4

_SYSTEM = """You extract hiring opportunities from scraped web items for a placement-intelligence tool used by Indian training institutes.

For EACH input item return every distinct opportunity it evidences, as JSON:
[{"item_id": <int>, "why_skipped": "<why, when no opportunities>", "opportunities": [
  {"company": "", "role": "", "location": "", "stipend": "", "posted": "",
   "website": "", "evidence_quote": "", "what_they_do": "",
   "apply_email": "", "apply_person": "", "apply_url": "",
   "author_name": "", "author_headline": "", "author_affiliation": ""}]}]

Rules:
- One opportunity per company+role. An aggregator or referral post listing N companies yields N opportunities. FAN OUT ALL OF THEM, never just the first.
- Only roles the text shows are being hired NOW. Skip celebration posts, news, courses, career advice, "I got hired" posts (why_skipped explains).
- Prioritize internships / early-career roles relevant to: {keywords}. Include other clearly-hiring early-career tech roles; skip senior/staff roles.
- Copy stipend, location, posted age EXACTLY as stated ("" when not stated). NEVER invent or guess values.
- apply_email: the email the text says to apply/write to. apply_person: the person the text names as the application contact (even first-name-only, e.g. "email Priya").
- author_*: whoever authored/posted the item, when identifiable from the text.
- author_affiliation, one of: "insider" (author works at the hiring company: says we/our/my team, or headline names it), "agency" (recruiter hiring for someone else), "aggregator" (jobs-listing account or referral-only poster, including "P.S. I'm not the hiring manager"), "unknown".
- location: use "Remote" for remote-India roles. what_they_do: <=15 words from the text, "" if unclear.
- evidence_quote: <=25 words verbatim from the text proving the hiring claim.
Return ONLY the JSON array."""


def author_profile_from_url(url: str) -> str:
    """Derive the author's profile URL from a post URL (deterministic; the
    poster slug is the URL's own prefix)."""
    m = re.search(r"linkedin\.com/posts/([^_/?#]+)", url or "", re.IGNORECASE)
    if m and not re.fullmatch(r"\d+", m.group(1)):
        return f"https://www.linkedin.com/in/{m.group(1)}"
    m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/", url or "", re.IGNORECASE)
    if m:
        return f"https://x.com/{m.group(1)}"
    return ""


def _encode_items(items: list[dict]) -> str:
    parts = []
    for it in items:
        parts.append(
            f"ITEM {it['id']} | source={it['source']} | url={it['url']}\n"
            f"title: {it['title']}\ndesc: {it['description']}\n"
            f"text:\n{(it['markdown'] or '(no text)')[:3800]}"
        )
    return "\n\n=====\n\n".join(parts)


def parse_batch_output(raw_out, items_by_id: dict) -> list[tuple[int, list[dict], str]]:
    """Validate one batch reply into (item_id, opportunities, why_skipped).
    Pure function (unit-tested): enforces the fan-out cap, drops placeholder
    companies with a reason, forces evidence_url/source from the item itself,
    derives author_profile from the URL."""
    out: list[tuple[int, list[dict], str]] = []
    if not isinstance(raw_out, list):
        return out
    for entry in raw_out:
        if not isinstance(entry, dict):
            continue
        try:
            item_id = int(entry.get("item_id"))
        except (TypeError, ValueError):
            continue
        item = items_by_id.get(item_id)
        if item is None:
            continue
        opps, dropped = [], []
        for o in (entry.get("opportunities") or [])[:_MAX_OPPS_PER_ITEM]:
            if not isinstance(o, dict):
                continue
            company = str(o.get("company") or "").strip()
            if not company or PLACEHOLDER_COMPANY.search(company):
                dropped.append(f"no real company ({company or 'blank'})")
                continue
            o = {k: (str(v).strip() if v is not None else "") for k, v in o.items()}
            o["company"] = company
            o["evidence_url"] = item["url"]          # code-assigned, never transcribed
            o["source"] = item["source"]
            if not o.get("author_profile"):
                o["author_profile"] = author_profile_from_url(item["url"])
            if o.get("author_affiliation") not in ("insider", "agency", "aggregator", "unknown"):
                o["author_affiliation"] = "unknown"
            opps.append(o)
        why = str(entry.get("why_skipped") or "")
        if dropped:
            why = (why + "; " if why else "") + "; ".join(dropped)
        out.append((item_id, opps, why))
    return out


def run(db, run_id: int, chat_id: int, params: dict) -> dict:
    items = state.pending_raw(db, chat_id)
    if not items:
        return {"items": 0, "opportunities": 0}
    system = _SYSTEM.format(keywords=", ".join(params.get("keywords") or []) or "internships")
    batches = [items[i:i + _BATCH] for i in range(0, len(items), _BATCH)]

    n_opps = n_skipped = n_fanout = n_emails = n_insiders = n_errors = 0
    results: list[tuple[int, list[dict], str]] = []
    with ThreadPoolExecutor(max_workers=_LLM_WORKERS) as pool:
        futs = {pool.submit(llm.chat_json, system, _encode_items(b)): b for b in batches}
        for fut in as_completed(futs):
            batch = futs[fut]
            try:
                raw_out = fut.result()
            except Exception as e:
                n_errors += 1
                state.push_event(db, run_id, "stage", "extract: batch failed",
                                 f"{e.__class__.__name__}: {e}"[:250])
                continue  # items stay 'harvested' -> retried on resume
            results += parse_batch_output(raw_out, {it["id"]: it for it in batch})

    for item_id, opps, why in results:
        for o in opps:
            state.insert_opportunity(db, chat_id, run_id, item_id, o)
            n_emails += 1 if o.get("apply_email") else 0
            n_insiders += 1 if o.get("author_affiliation") == "insider" else 0
        n_opps += len(opps)
        n_fanout += 1 if len(opps) > 1 else 0
        if opps:
            state.mark_raw(db, item_id, "extracted", f"{len(opps)} opportunities")
        else:
            n_skipped += 1
            state.mark_raw(db, item_id, "skipped", why[:250] or "no hiring content")
    return {"items": len(items), "opportunities": n_opps, "fanout_items": n_fanout,
            "apply_emails": n_emails, "insider_authors": n_insiders,
            "skipped_items": n_skipped, "failed_batches": n_errors}
