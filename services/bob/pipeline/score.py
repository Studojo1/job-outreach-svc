"""Scoring stage: mandate fit for every verified opportunity.

Two layers, cheap first:
  1. deterministic function pre-gate (word-boundary keyword match, the rail
     that kept 'Founder's Office' padding out of AI tables) rejects obvious
     off-function roles for free;
  2. one batched LLM rubric call per ~20 opportunities scores the rest.
The floor lives in CODE (min_fit, default 55). Scoring runs once per
opportunity; there is no path to re-score with a pumped number (the run-12
score-inflation bug is structurally impossible here).
"""

import logging
import re

from services.bob.pipeline import llm, state

logger = logging.getLogger(__name__)

_BATCH = 20
DEFAULT_MIN_FIT = 55
# Generic tokens that would make the pre-gate pass everything.
_GENERIC = {"intern", "interns", "internship", "internships", "fresher", "freshers",
            "engineer", "developer", "graduate", "trainee", "job", "role", "roles"}

_SYSTEM = """You score internship opportunities for fit against a placement mandate. 0-100:
90+: exact function match, stated stipend, target city/Remote, credible product company.
70-89: right function, minor gaps (stipend unstated, adjacent stack, Pan-India).
55-69: plausible but weak (function partially matches, thin evidence, unknown org).
<55: off-function, senior role, training/pay-to-intern vibe, or wrong geography.
Judge ONLY from the provided fields. Do not reward big brand names for off-mandate roles.
Return JSON: [{"id": <int>, "fit": <int>, "reason": "<= 12 words"}] for EVERY input id."""


def gate_tokens(keywords: list[str]) -> list[str]:
    """Full phrases + their non-generic words; the pre-gate needs at least
    one to match on a word boundary."""
    toks: list[str] = []
    for kw in keywords or []:
        kw = str(kw).strip().lower()
        if not kw:
            continue
        toks.append(kw)
        toks += [w for w in re.split(r"[^a-z0-9+#]+", kw)
                 if len(w) >= 2 and w not in _GENERIC]
    seen: set = set()
    return [t for t in toks if not (t in seen or seen.add(t))]


def off_function(opp: dict, tokens: list[str]) -> bool:
    """True when the role text matches NO mandate token on a word boundary
    ('ai' must not match inside 'email')."""
    if not tokens:
        return False
    hay = " ".join(str(opp.get(k) or "") for k in
                   ("role", "evidence_quote", "what_they_do")).lower()
    if not hay.strip():
        return False
    return not any(re.search(rf"\b{re.escape(t)}\b", hay) for t in tokens)


def apply_scores(opps: list[dict], llm_out, min_fit: int) -> list[tuple[int, str, int | None, str]]:
    """Merge LLM verdicts with the floor. Returns (opp_id, action, fit, reason)
    where action is 'scored' or 'rejected'. Opportunities the model failed to
    score are REJECTED with an explicit reason, never silently passed."""
    verdicts: dict[int, tuple[int, str]] = {}
    if isinstance(llm_out, list):
        for v in llm_out:
            if not isinstance(v, dict):
                continue
            try:
                oid, fit = int(v.get("id")), int(v.get("fit"))
            except (TypeError, ValueError):
                continue
            verdicts[oid] = (max(0, min(100, fit)), str(v.get("reason") or "")[:120])
    out = []
    for o in opps:
        oid = o["id"]
        if oid not in verdicts:
            out.append((oid, "rejected", None, "scoring returned no verdict for this opportunity"))
            continue
        fit, reason = verdicts[oid]
        if fit < min_fit:
            out.append((oid, "rejected", fit, f"fit {fit} below floor {min_fit}: {reason}"))
        else:
            out.append((oid, "scored", fit, reason))
    return out


def _encode(opps: list[dict]) -> str:
    rows = []
    for o in opps:
        rows.append(
            f'{{"id": {o["id"]}, "company": "{o["company"][:60]}", "role": "{(o.get("role") or "")[:80]}", '
            f'"location": "{(o.get("location") or "")[:40]}", "stipend": "{(o.get("stipend") or "")[:40]}", '
            f'"evidence": "{(o.get("evidence_quote") or "")[:150]}", '
            f'"what_they_do": "{(o.get("what_they_do") or "")[:80]}", "source": "{o.get("source") or ""}"}}'
        )
    return "[\n" + ",\n".join(rows) + "\n]"


def run(db, run_id: int, chat_id: int, params: dict) -> dict:
    opps = state.opportunities(db, chat_id=chat_id, status="verified")
    if not opps:
        return {"in": 0}
    min_fit = int(params.get("min_fit") or DEFAULT_MIN_FIT)
    tokens = gate_tokens(params.get("keywords") or [])

    # Layer 1: free deterministic pre-gate.
    pregated = 0
    to_score = []
    for o in opps:
        if off_function(o, tokens):
            state.reject(db, o["id"], "score", f"role matches no mandate function {tokens[:6]}")
            pregated += 1
        else:
            to_score.append(o)

    # Layer 2: batched rubric.
    mandate = (f"MANDATE: functions={params.get('keywords')}, location={params.get('location')}, "
               f"freshness window={params.get('freshness_days', 7)}d. "
               f"CANDIDATE(S): {str(params.get('candidate_profile') or 'tech interns, India')[:400]}")
    passed = floored = unscored = 0
    for i in range(0, len(to_score), _BATCH):
        batch = to_score[i:i + _BATCH]
        try:
            out = llm.chat_json(_SYSTEM, mandate + "\n\nOPPORTUNITIES:\n" + _encode(batch))
        except Exception as e:
            out = None
            state.push_event(db, run_id, "stage", "score: batch failed",
                             f"{e.__class__.__name__}: {e}"[:250])
        for oid, action, fit, reason in apply_scores(batch, out, min_fit):
            if action == "scored":
                state.transition(db, oid, "scored", fit_score=fit, fit_reason=reason)
                passed += 1
            else:
                state.reject(db, oid, "score", reason)
                floored += 1 if fit is not None else 0
                unscored += 1 if fit is None else 0
    return {"in": len(opps), "pre_gated_off_function": pregated, "scored": passed,
            "below_floor": floored, "unscored": unscored, "min_fit": min_fit}
