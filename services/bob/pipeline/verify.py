"""Verification stage: deterministic gates over extracted opportunities.

Relocated rails (spam blocklist, user rejection ledger, table dedupe,
posting-age gate, evidence liveness) now run over the WHOLE pool before any
scoring, and every rejection records stage + reason (the veto ledger).
Nothing is filtered by silence.

verdict() is pure and unit-tested; run() wires it to the DB and runs the
network liveness checks in parallel with memoization.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from services.bob.pipeline import state
from services.bob.textrails import SPAM_ORGS, parse_age_days

logger = logging.getLogger(__name__)

_LIVENESS_WORKERS = 8
# Only these URL families have a free deterministic liveness check.
_CHECKABLE = re.compile(r"linkedin\.com/jobs/view/|jobs\.ashbyhq\.com|greenhouse\.io|jobs\.lever\.co",
                        re.IGNORECASE)


def _role_key(role: str) -> str:
    r = re.sub(r"internship", "intern", (role or "").lower())
    return re.sub(r"[^a-z0-9]+", "", r)


def verdict(opp: dict, freshness_days: int, rejected: dict, table_norms: dict,
            seen_keys: set, liveness) -> tuple[str, str]:
    """('verified'|'rejected', reason). Order matters: cheap gates first.
    Unknown age is NOT fresh; it passes here only when liveness can vouch or
    the source is a live index (guest/unstop items are live by construction)."""
    company, cn = opp["company"], opp["company_norm"]
    if SPAM_ORGS.search(company):
        return "rejected", "known internship-spam org"
    if cn in rejected:
        return "rejected", "user removed this company from the mandate"
    if cn in table_norms:
        return "rejected", f"already in table (row {table_norms[cn]})"
    key = (cn, _role_key(opp.get("role") or ""))
    if key in seen_keys:
        return "rejected", "duplicate of an earlier opportunity this run"
    seen_keys.add(key)

    age = parse_age_days(opp.get("posted") or "")
    if age is not None and age > freshness_days:
        return "rejected", f"stale posting ({opp.get('posted')}); window is {freshness_days}d"

    ev = opp.get("evidence_url") or ""
    if ev and _CHECKABLE.search(ev):
        status, reason = liveness(ev)
        if status == "closed":
            return "rejected", f"dead evidence: {reason}"
    return "verified", ""


def run(db, run_id: int, chat_id: int, params: dict) -> dict:
    opps = state.opportunities(db, run_id, status="extracted")
    if not opps:
        return {"in": 0}
    freshness_days = int(params.get("freshness_days") or 7)
    rejected = state.rejected_companies(db, chat_id)
    table_norms = state.table_companies(db, int(params.get("table_id") or 0)) \
        if params.get("table_id") else {}

    # Parallel, memoized liveness for the checkable evidence URLs only.
    checkable = sorted({o["evidence_url"] for o in opps
                        if o.get("evidence_url") and _CHECKABLE.search(o["evidence_url"])})
    cache: dict = {}

    def _check(u: str):
        from services.bob import livecheck
        try:
            return u, livecheck.evidence_gate(u)
        except Exception as e:  # a gate failure never blocks a row
            return u, ("unknown", f"gate error ({e.__class__.__name__})")

    if checkable:
        with ThreadPoolExecutor(max_workers=_LIVENESS_WORKERS) as pool:
            for u, v in pool.map(_check, checkable):
                cache[u] = v

    seen: set = set()
    passed = rejected_n = 0
    reasons: dict = {}
    for o in opps:
        status, reason = verdict(o, freshness_days, rejected, table_norms, seen,
                                 lambda u: cache.get(u, ("unknown", "not checked")))
        if status == "verified":
            state.transition(db, o["id"], "verified")
            passed += 1
        else:
            state.reject(db, o["id"], "verify", reason)
            rejected_n += 1
            reasons[reason.split("(")[0].strip()[:40]] = reasons.get(reason.split("(")[0].strip()[:40], 0) + 1
    top = "; ".join(f"{k} x{v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:5])
    return {"in": len(opps), "verified": passed, "rejected": rejected_n,
            "liveness_checked": len(checkable), "_top_reasons": top}
