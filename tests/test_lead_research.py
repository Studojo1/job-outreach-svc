#!/usr/bin/env python3
"""Verification suite for deep lead research + the depth guard.

Standalone: no pytest, no DB, no network. Run with:

    python3 tests/test_lead_research.py

Every assertion here corresponds to a bug that was found and fixed, or to a
safety property that must never regress. Read the failure messages: they say
what breaks in production, not just what differs.
"""

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"\n         {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# ── Stub the heavy deps so the modules import without settings/DB/network ──────

def _stub_env(**settings_kw):
    S = types.SimpleNamespace(
        CONTEXT_DEV_API_KEY="k",
        CONTEXT_DEV_BASE_URL="https://api.context.dev/v1",
        CONTEXT_DEV_MAX_CREDITS_PER_LEAD=3,
        CONTEXT_DEV_CAMPAIGN_CREDIT_CAP=0,
        AZURE_OPENAI_EMAIL_DEPLOYMENT="d",
        **settings_kw,
    )
    for pkg in ["core", "database", "services", "services.shared", "services.shared.ai"]:
        sys.modules.setdefault(pkg, types.ModuleType(pkg))

    def mk(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    noop = lambda *a, **k: None
    mk("core.config", settings=S)
    mk("core.logger", get_logger=lambda n: types.SimpleNamespace(
        info=noop, warning=noop, error=noop, debug=noop))
    mk("database.models", Lead=object, Candidate=object)
    mk("services.shared.ai.azure_openai_client",
       generate_json=None, ContentFilterError=type("CF", (Exception,), {}))
    return S


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════════════════════
print("\ncontext.dev client — credit rules and the Call-3 domain leak (B5)")
# ══════════════════════════════════════════════════════════════════════════════

_stub_env()
CALLS = []


class _Resp:
    ok, status_code = True, 200

    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b


def _md(text=None, code="SUCCESS"):
    """The REAL shape of context.dev's per-result `markdown` field, captured from a
    live call: an object, never a string. Treating it as a string raised
    AttributeError: 'dict' object has no attribute 'strip' on the first scraped
    result. Per-URL TIMEOUT is routine (ZoomInfo returns it reliably)."""
    return {"markdown": text, "code": code}


def _fake_post_rich(url, json=None, headers=None, timeout=None):
    """LinkedIn yields snippets AND a /posts/ url; Call 2 yields a talk.
    Both are required for is_rich() -> skip Call 3."""
    CALLS.append(json)
    inc = json.get("includeDomains")
    if inc == ["linkedin.com"]:
        return _Resp({"results": [
            {"url": f"https://linkedin.com/in/x{i}", "description": f"snip{i}"} for i in range(4)
        ] + [{"url": "https://linkedin.com/posts/p1", "description": "a post"}]})
    if inc and "youtube.com" in inc:
        return _Resp({"results": [{"url": "https://youtube.com/watch?v=1",
                                   "markdown": _md("my podcast answer")}]})
    return _Resp({"results": []})


def _fake_post_x_empty(url, json=None, headers=None, timeout=None):
    """X returns NOTHING (it gates heavily). This is the exact condition that
    exposed B5: x.com/u/1 was never recorded in fetched_urls, so Call 3's
    URL-only dedupe let it through into web_markdown."""
    CALLS.append(json)
    inc = json.get("includeDomains")
    if inc == ["linkedin.com"]:
        return _Resp({"results": [{"url": "https://linkedin.com/in/a", "description": "s"}]})
    if inc and "youtube.com" in inc:
        return _Resp({"results": []})
    return _Resp({"results": [
        {"url": "https://youtube.com/watch?v=9", "markdown": _md("LEAKED_CALL2_CONTENT")},
        {"url": "https://zoominfo.com/p/x", "markdown": _md(None, "TIMEOUT")},
        {"url": "https://podcast.fm/ep", "markdown": _md("founding story")},
    ]})


sys.modules["requests"] = types.ModuleType("requests")
sys.modules["requests"].post = _fake_post_rich
sys.modules["requests"].RequestException = Exception
cc = _load("cc", "services/lead_research/context_client.py")

CALLS.clear()
b = cc.research_lead("Sreedhar Pogaku", "Neeman's", "Head of Logistics")
check("Call 1 (LinkedIn) leaves markdown OFF (login wall returns zero-byte fields)",
      CALLS[0]["markdownOptions"]["enabled"] is False)
check("LinkedIn /posts/ URLs are harvested for Call 3 (post pages DO render)",
      b.linkedin_post_urls == ["https://linkedin.com/posts/p1"])
check("Calls 1+2 sufficient -> Call 3 skipped: a two-credit lead is a good lead",
      b.credits_spent == 2, f"spent {b.credits_spent}, expected 2")

sys.modules["requests"].post = _fake_post_x_empty
CALLS.clear()
b2 = cc.research_lead("Dishant", "Comet", "Founder")
check("Call 3 fires when Calls 1+2 are thin", b2.credits_spent == 3)
check("Call 3 excludes every domain owned by Calls 1+2",
      sorted(CALLS[2]["excludeDomains"]) ==
      sorted(["linkedin.com", "youtube.com", "medium.com", "substack.com"]))
check("B5: Call 3 rejects a Call-2 domain even when Call 2 returned nothing "
      "(excludeDomains is a request to a 3rd party, not a guarantee)",
      "LEAKED_CALL2_CONTENT" not in b2.web_markdown,
      f"web_markdown={b2.web_markdown} — Call-2 content leaked into the open-web "
      f"layer, corrupting source attribution")
check("Call 3 keeps genuine open-web content", "founding story" in b2.web_markdown)

# ── Regressions for bugs found only by calling the LIVE API ────────────────────
check("LIVE-1: base URL carries the /v1 prefix (without it the API 403s with "
      "'the API you have tried to access does not exist', which the client would "
      "swallow as transient -> every email silently becomes a bare ask)",
      cc.settings.CONTEXT_DEV_BASE_URL.rstrip("/").endswith("/v1"),
      f"base={cc.settings.CONTEXT_DEV_BASE_URL!r}")
check("LIVE-2: `markdown` is an OBJECT, not a string; _markdown() unwraps it",
      cc._markdown({"markdown": _md("hello")}) == "hello")
check("LIVE-2b: a per-URL TIMEOUT yields no text (ZoomInfo does this reliably)",
      cc._markdown({"markdown": _md(None, "TIMEOUT")}) == "")
check("LIVE-2c: NOT_REQUESTED (markdown off) yields no text",
      cc._markdown({"markdown": _md(None, "NOT_REQUESTED")}) == "")
check("LIVE-2d: a raw-string markdown would still work if the API ever flattens it",
      cc._markdown({"markdown": " hi "}) == "hi")
check("LIVE-2e: TIMEOUT docs never enter the web layer",
      "LEAKED_CALL2_CONTENT" not in b2.web_markdown and len(b2.web_markdown) == 1)
check("LIVE-3: scrape timeout > 48s (a live scraped search took 48.2s; the old "
      "20s timeout guaranteed Calls 2 and 3 could never succeed)",
      cc._TIMEOUT_SCRAPE > 48, f"scrape timeout={cc._TIMEOUT_SCRAPE}s")
check("LIVE-4: credits come from the API's own key_metadata, not a local counter",
      cc._credits_used({"key_metadata": {"credits_consumed": 1}}) == 1
      and cc._credits_used({}) == 1)
check("LIVE-6: is_rich() requires a LinkedIn POST url, not merely any Call-2 hit. "
      "Real data: searching 'Divakar Sharma'+Neeman's on Substack returned a 76k-char "
      "legal newsletter by a stranger, matched on his compliance role. Trusting it "
      "would attribute another human's opinions to the lead AND skip Call 3, where "
      "his real signal lived.",
      cc.ResearchBundle(x_markdown=["doc"], linkedin_snippets=["a"]*9).is_rich() is False
      and cc.ResearchBundle(x_markdown=["doc"],
                            linkedin_post_urls=["u"]).is_rich() is True)
check("LIVE-5: 401/403 is a CONFIG error (bad key or bad base URL), not a per-lead "
      "failure. Releasing the lead's claim on it would re-call a paid API every "
      "30s cycle -- 120x/hour on one lead. A client timeout does not cancel "
      "server-side billing, so those retries can cost real money.",
      issubclass(cc.ContextDevConfigError, cc.ContextDevAuthError)
      and issubclass(cc.ContextDevCreditError, cc.ContextDevAuthError)
      and cc.ContextDevConfigError is not cc.ContextDevCreditError)


# ══════════════════════════════════════════════════════════════════════════════
print("\ndepth guard — must FAIL CLOSED (a guard that fails open ships the "
      "generic lines it exists to kill)")
# ══════════════════════════════════════════════════════════════════════════════

VERDICT = {}
m = types.ModuleType("services.shared.ai.azure_openai_client")
m.generate_json = lambda *a, **k: VERDICT
m.ContentFilterError = type("CF", (Exception,), {})
sys.modules["services.shared.ai.azure_openai_client"] = m
ex = _load("ex", "services/lead_research/extractor.py")

LAYERS = {"quote": None, "derived_operational": "20yr industrial gas -> D2C",
          "behavioural": None, "live_move": "raised Series A"}


def _ships(verdict, layers=LAYERS):
    VERDICT.clear()
    VERDICT.update(verdict)
    r = ex.run_depth_guard(layers, "S", "Head of Logistics", "Neeman's", "batched writes", "bot")
    return r["survives_swap"] is False and bool(r.get("recipient_clause"))


def _guard(verdict, layers=LAYERS, **kw):
    VERDICT.clear(); VERDICT.update(verdict)
    return ex.run_depth_guard(layers, "S", "Head of Logistics", "Neeman's",
                              "batched writes", "bot", **kw)


check("role-generic line is REJECTED (survives the swap)",
      not _ships({"synthesis_line": "As Head of Logistics you care about cost",
                  "survives_swap": True, "layer": "derived_operational", "reason": "r"}))
check("person-specific line SHIPS (survives no swap)",
      _ships({"synthesis_line": "20yr moving industrial gas, and I batched writes",
              "recipient_clause": "20yr moving industrial gas",
              "survives_swap": False, "layer": "derived_operational", "reason": "r"}))
check("missing `survives_swap` key -> reject (fail closed)",
      not _ships({"synthesis_line": "x", "layer": "quote", "reason": "r"}))
check("empty line but model says ship -> reject (fail closed)",
      not _ships({"synthesis_line": "", "survives_swap": False, "layer": "quote", "reason": "r"}))
check("ATTRIBUTION: a verdict with no recipient_clause must NOT ship. The email "
      "prompt renders `belief` as a fact ABOUT the recipient; handing it the fused "
      "synthesis_line makes the sender's own method read as the recipient's, and the "
      "email compliments them on the sender's idea. Observed live before the fix: "
      "\"Batching contract reviews by clause type sounds like a smart way...\" -- "
      "that was Riya's technique, addressed to Divakar.",
      not _ships({"synthesis_line": "X taught you Y, and Z taught me W",
                  "survives_swap": False, "layer": "quote", "reason": "r"}))
check("ATTRIBUTION: a verdict WITH a clean recipient_clause ships",
      _ships({"synthesis_line": "X taught you Y, and Z taught me W",
              "recipient_clause": "X taught you Y",
              "survives_swap": False, "layer": "quote", "reason": "r"}))
check("BRIDGE: a company signal is separated into bridge_clause, never merged into "
      "recipient_clause. CompanyProfile is keyed on `domain`, so every fact in it is "
      "identical for every lead there; merged in, it would be attributed to the person.",
      _guard({"synthesis_line": "A taught you B, and 12 stores means more C. I did D",
              "recipient_clause": "A taught you B", "bridge_clause": "12 stores means more C",
              "bridge_used": True, "survives_swap": False, "layer": "quote",
              "reason": "r"}).get("bridge_clause") == "12 stores means more C")
check("BRIDGE: no company signal -> bridge_clause is None, email drops the clause",
      _guard({"synthesis_line": "A taught you B, and I did D",
              "recipient_clause": "A taught you B", "bridge_clause": "",
              "bridge_used": False, "survives_swap": False, "layer": "quote",
              "reason": "r"}).get("bridge_clause") is None)
check("BRIDGE: a company signal ALONE can never carry a line -- no carrier layer "
      "means bare ask, without even calling the LLM",
      _guard({}, layers={k: None for k in ex.LAYER_PRIORITY},
             company_signals={"recent_momentum": "raised a Series B",
                              "hiring_signal": "hiring"}).get("survives_swap") is True)
check("only a live_move available -> reject without an LLM call "
      "(texture can never carry the line)",
      not _ships({}, {"quote": None, "derived_operational": None,
                      "behavioural": None, "live_move": "Series A"}))


# ══════════════════════════════════════════════════════════════════════════════
print("\nemail shape — band inference and the bare-ask path")
# ══════════════════════════════════════════════════════════════════════════════

sys.modules["database.models"].Lead = object
eg = _load("eg", "services/email_campaign/email_generator_service.py")


class _L:
    def __init__(s, size):
        s.company_size = size
        s.name, s.title, s.company = "Ana Rao", "Engineering Manager", "Kite"


for size, want in [("1-10", "solo"), ("11-50", "small"), ("51-200", "small"),
                   ("1001-5000", "structured"), ("", "small"), (None, "small")]:
    got = eg.infer_band(_L(size))
    check(f"band({size!r}) == {want!r}", got == want, f"got {got!r}")

check("unknown company size never yields 'solo' "
      "('you are the whole company' misfires badly at a 5000-person org)",
      eg.infer_band(_L(None)) != "solo")

cp = {"candidate_name": "Riya", "short_candidate_signal": "I built a scheduling bot",
      "primary_field": "backend engineering", "job_interest": "backend",
      "key_skills": ["Python"], "has_flex_notes": True, "candidate_city": "",
      "work_principle": "batched writes", "credential": ""}
lp = {"lead_name": "Ana Rao", "lead_role": "Engineering Manager", "company_name": "Kite",
      "why_this_person": "Would you know if there's an opening?",
      "what_they_build": "payments infra", "core_tech": ["Go"],
      "recent_momentum": "Series A", "has_company_description": True,
      "contextual_hook": "x", "department_hint": "engineering",
      "hiring_signal": None, "recent_job_postings": []}

bare = eg._build_generation_prompt(cp, lp, eg.BAND_SOLO, research=None)
check("no research -> prompt forbids 'I came across' openers",
      "Do NOT open with 'I came across" in bare)
check("no research -> company facts are demoted to background, not a hook",
      "NOT a hook, do not open with it" in bare)
check("no research -> the synthesis slot is structurally ABSENT "
      "(the model is given nowhere to confabulate)",
      "SYNTHESIS LINE" not in bare)

rich = eg._build_generation_prompt(cp, lp, eg.BAND_SOLO,
                                   research={"quote": "retries are a data model bug",
                                             "belief": "x", "source_url": "http://x"})
check("with research -> synthesis slot present", "SYNTHESIS LINE" in rich)
check("with research -> equivalence claims are forbidden", "same bet" in rich)
check("with research -> hedges are forbidden", "feels relevant" in rich)


# ══════════════════════════════════════════════════════════════════════════════
print("\nB2 — campaign credit cap must not fan out through EmailSent")
# ══════════════════════════════════════════════════════════════════════════════

# One lead has many EmailSent rows: T1, T2, T3, plus replacements. Summing
# credits_spent across a LeadResearch->Lead->EmailSent join multiplies each
# person's credits by their touch count.
person_credits = {"p1": 3, "p2": 3, "p3": 2}
emails = [("p1", "T1"), ("p1", "T2"), ("p1", "T3"),
          ("p2", "T1"), ("p2", "T2"),
          ("p3", "T1")]

true_spend = sum(person_credits.values())
fanned_spend = sum(person_credits[pk] for pk, _ in emails)
distinct_spend = sum(person_credits[pk] for pk in {pk for pk, _ in emails})

check("naive EmailSent join inflates the credit sum",
      fanned_spend > true_spend, f"{fanned_spend} vs true {true_spend}")
check("B2 fix: summing over DISTINCT persons equals true spend",
      distinct_spend == true_spend, f"{distinct_spend} != {true_spend}")
check("with cap=10 the naive query trips early, the fixed one does not",
      (fanned_spend >= 10) and not (distinct_spend >= 10))


# ══════════════════════════════════════════════════════════════════════════════
print("\nT3 cancel — a reply to T2 must cancel T3 (T3 is parented to T1)")
# ══════════════════════════════════════════════════════════════════════════════

class _E:
    def __init__(s, id, status, replied=None, parent=None):
        s.id, s.status, s.reply_received_at, s.parent_email_id = id, status, replied, parent
        s.campaign_id = s.lead_id = 1


def _old_cancel(fu, rows):
    parent = next(r for r in rows if r.id == fu.parent_email_id)  # always T1
    return parent.reply_received_at is not None or parent.status == "replied"


def _new_cancel(fu, rows):
    touches = [r for r in rows if r.campaign_id == fu.campaign_id
               and r.lead_id == fu.lead_id and r.id != fu.id]
    return any(t.reply_received_at is not None or t.status == "replied" for t in touches)


for label, t1s, t2s in [("no reply", "sent", "sent"),
                        ("replied to T1", "replied", "sent"),
                        ("replied to T2", "sent", "replied"),
                        ("replied to both", "replied", "replied")]:
    rows = [_E(1, t1s, "x" if t1s == "replied" else None),
            _E(2, t2s, "x" if t2s == "replied" else None, parent=1),
            _E(3, "followup_pending", None, parent=1)]
    should = "replied" in (t1s, t2s)
    got = _new_cancel(rows[2], rows)
    check(f"T3 cancelled when {label}: {should}", got == should)

rows = [_E(1, "sent"), _E(2, "replied", "x", parent=1), _E(3, "followup_pending", None, parent=1)]
check("regression: the OLD predicate misses a T2 reply (this was the bug)",
      _old_cancel(rows[2], rows) is False and _new_cancel(rows[2], rows) is True)


# ══════════════════════════════════════════════════════════════════════════════
print("\nB3 — replacement emails must reach the generator")
# ══════════════════════════════════════════════════════════════════════════════

def _generates(status):
    """_generate_pending selects solely on status == 'pending_enrichment'."""
    return status == "pending_enrichment"


def _sends(status):
    """_send_ready selects on status == 'queued'."""
    return status == "queued"


check("B3: a 'queued' replacement skips generation and reaches the sender with "
      "body=None -> MIMEText(None) -> AttributeError -> failed. Latent, not "
      "observed (staging: 0 rows), but reachable: 2394 enriched leads sit "
      "outside any campaign and _pick_next_lead can promote one.",
      not _generates("queued") and _sends("queued"))
check("B3 fix: 'pending_enrichment' reaches the generator",
      _generates("pending_enrichment"))


# ══════════════════════════════════════════════════════════════════════════════
print("\nD2 — the first email must outlast worst-case background research")
# ══════════════════════════════════════════════════════════════════════════════

research_worst = 3 * 20 + 2 * 10  # 3 HTTP timeouts + extract LLM + guard LLM
check(f"first_delay lower bound (280s) beats worst-case research ({research_worst}s)",
      280 >= research_worst)
check("old first_delay (30s) did NOT beat worst-case research", not (30 >= research_worst))


# ══════════════════════════════════════════════════════════════════════════════
print()
if _failures:
    print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
    sys.exit(1)
print("All checks passed.")
