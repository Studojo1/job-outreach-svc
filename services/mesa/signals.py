"""Mesa Signal Engine — turn a pile of scraped jobs into ranked company SIGNALS.

Same idea the paid tools use (Honeylead / dfy.outreachai / TheirStack): a single
job posting is noise, but the *combination* of independent signals a company emits
is intent. This scores every company across independent signal families and
rewards CONFLUENCE (2+ families = a real signal). Pure function over the standard
Mesa job dicts — no external calls, no auth, no Apollo — so it runs instantly over
whatever a search already scraped, from any source.

Signal families (computable from title / company / posted_date / source / post_text):
  leader_seat   a live Head/VP/Director/Chief of Sales/Revenue/GTM role
  no_leader     'no leader yet' language in a title/post (founding / first / 0-to-1 / build GTM)
  army          hiring junior sales roles (SDR/AE/BDR) but NO leadership sales role
  surge         3+ open sales roles at once
  revops        a RevOps / Sales Ops hire = they're systematizing sales; a leader usually follows
  founder_post  the role came from a founder/recruiter hiring POST (source=linkedin_posts) = high intent
"""

import re
from collections import defaultdict

_LEADER = re.compile(
    r"\b(head of|head[,\- ]|vp\b|v\.p|vice president|director of|director[,\- ]|chief|cro\b|"
    r"national head|country head|country manager|zonal head|regional head|business head|"
    r"global head|svp|founding (gtm|sales|revenue))\b", re.I)
_SALES = re.compile(
    r"\b(sales|revenue|gtm|go[- ]to[- ]market|growth|partnership|business development|"
    r"enterprise|commercial|account)\b", re.I)
_JUNIOR = re.compile(
    r"\b(sdr|bdr|account executive|\bae\b|inside sales|sales development|sales executive|"
    r"business development (rep|associate|executive)|sales associate|sales representative|telecaller)\b", re.I)
_INTENT = re.compile(
    r"\b(founding|first (sales|commercial|gtm|revenue) hire|0[ -]?to[ -]?1|0-1|from scratch|"
    r"build (the|our|out) (sales|gtm|go[- ]to[- ]market|revenue|commercial)|establish (the )?sales|"
    r"player[- ]coach|set up (the )?sales|reports? (directly )?to (the )?(founder|ceo))\b", re.I)
_REVOPS = re.compile(
    r"\b(revenue operations|revops|rev ops|sales operations|sales ops|gtm operations|"
    r"crm (admin|manager)|salesforce admin|hubspot admin|sales enablement|deal desk)\b", re.I)

# ── Additional signal families (all computed from the same scraped job data) ──
# Funding / scaling: an explicit fundraise or "we're scaling" marker in the post.
_FUNDING = re.compile(
    r"\b(series [a-e]\b|seed round|pre[- ]?seed|just raised|recently raised|raised (\$|usd|inr|₹|€|£)|"
    r"well[- ]funded|backed by|y[- ]?combinator|yc ?[swf]?\d{2}|angel[- ]backed|newly funded|"
    r"fresh (round of )?funding|closed our (seed|series)|hyper[- ]?growth|scaling (fast|rapidly))\b", re.I)
# Org maturity / churn: a seat opened by turnover or team expansion, not greenfield.
_CHURN = re.compile(
    r"\b(back[- ]?fill|replacing|replacement for|maternity cover|parental cover|"
    r"due to (growth|expansion|attrition)|re[- ]?hir(e|ing)|newly vacated|stepping (down|into))\b", re.I)
# GTM build: marketing / demand-gen / growth LEADERSHIP alongside sales = full engine.
_MKTG_LEAD = re.compile(
    r"\b(head of (marketing|growth|demand|brand|content)|vp (of )?(marketing|growth)|"
    r"marketing director|director of (marketing|growth|demand)|cmo\b|demand gen(eration)?|"
    r"growth (lead|head|manager)|brand (lead|head|director)|head of performance)\b", re.I)
# Geo expansion language: entering a new market / first person on the ground.
_GEO = re.compile(
    r"\b(first (hire|employee|team member|person) in|expanding (in|into|to)|"
    r"new (office|market|region) in|opening (our|a)[^.]{0,25}office|launching in|"
    r"establish(ing)? (our )?presence in|ground zero for)\b", re.I)
# "Remote"/generic locations that must NOT count as distinct geos for expansion.
_GENERIC_LOC = re.compile(r"^(remote|anywhere|flexible|worldwide|global|india|—|n/?a|)$", re.I)

_FAMILY_WEIGHT = {
    "leader_seat": 25, "no_leader": 18, "army": 18, "surge": 10, "revops": 12, "founder_post": 20,
    "funding": 16, "geo_expansion": 14, "org_maturity": 12, "gtm_build": 14,
}
_FAMILY_LABEL = {
    "leader_seat": "Leadership sales seat open (live)",
    "no_leader": "'No leader yet' language",
    "army": "Hiring an army, no general",
    "surge": "Hiring surge (3+ sales roles)",
    "revops": "RevOps hire (systematizing sales)",
    "founder_post": "Founder/recruiter hiring post (high intent)",
    "funding": "Funded / scaling fast",
    "geo_expansion": "Expanding into a new market",
    "org_maturity": "Team churn or seniority ladder (org maturing)",
    "gtm_build": "Building the full GTM engine (marketing + sales)",
}


def _norm(name: str) -> str:
    n = re.sub(r"\(.*?\)", " ", (name or "").lower())
    n = re.sub(r"\b(ai|technologies|technology|labs|inc|india|pvt|ltd|the|app|cloud|global|hq)\b", " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def _read(families: set, top_leader: str) -> str:
    if "founder_post" in families:
        return "A founder/recruiter is personally posting the hire — reach them directly, the intent is explicit."
    bits = []
    if "leader_seat" in families:
        bits.append(f"Live leadership role ({top_leader[:40]}) — apply direct and own the function.")
    if "army" in families:
        bits.append("Building a sales team with no leader — pitch the Head-of-Sales seat.")
    if "revops" in families:
        bits.append("A RevOps hire means they're systematizing sales; a VP Sales hire usually follows within a quarter.")
    if "no_leader" in families:
        bits.append("Job language says the sales org is being built from zero — greenfield leadership seat.")
    if "surge" in families and not bits:
        bits.append("Hiring across sales at volume — a leader to run it is the natural next hire.")
    if "funding" in families:
        bits.append("Fresh funding or explicit scaling language — budget is unlocked and hiring is a priority right now.")
    if "geo_expansion" in families:
        bits.append("Opening the same role in a new market — they're expanding and need people on the ground.")
    if "gtm_build" in families:
        bits.append("Hiring marketing/growth leadership next to sales — they're building the whole GTM engine, not one seat.")
    if "org_maturity" in families and not bits:
        bits.append("Backfills and a junior-to-senior ladder — the org is maturing and formalizing its team.")
    return " ".join(bits) or "Sales hiring detected — watch for a second signal before investing time."


def score_jobs(jobs: list[dict]) -> list[dict]:
    """Group jobs by company and score each on the signal families above.
    Returns companies ranked by score (highest first). Confluence (2+ families)
    earns a bonus; single-signal companies are kept but ranked below."""
    by_company: dict[str, dict] = {}
    role_titles: dict[str, list] = defaultdict(list)
    for j in jobs:
        company = (j.get("company") or "").strip()
        if not company or company == "—":
            continue
        k = _norm(company)
        if not k:
            continue
        rec = by_company.setdefault(k, {"company": company, "sources": set(), "roles": []})
        rec["sources"].add(j.get("source") or "")
        title = j.get("title") or ""
        text = f"{title} {j.get('post_text') or ''}"
        rec["roles"].append({
            "title": title, "url": j.get("url"), "source": j.get("source"),
            "posted_date": j.get("posted_date"), "location": j.get("location") or "",
            "_text": text,
        })
        role_titles[k].append(title)

    out = []
    for k, rec in by_company.items():
        titles = [r["title"] for r in rec["roles"]]
        texts = [r["_text"] for r in rec["roles"]]
        fams: set = set()
        leaders = [t for t in titles if _LEADER.search(t) and _SALES.search(t)]
        if leaders:
            fams.add("leader_seat")
        if any(_INTENT.search(t) for t in texts):
            fams.add("no_leader")
        juniors = [t for t in titles if _JUNIOR.search(t)]
        if juniors and not leaders:
            fams.add("army")
        if len([t for t in titles if _SALES.search(t)]) >= 3:
            fams.add("surge")
        if any(_REVOPS.search(t) for t in titles):
            fams.add("revops")
        if "linkedin_posts" in rec["sources"] and any(_SALES.search(t) for t in titles):
            fams.add("founder_post")
        # Funding / scaling: fundraise or hyper-growth language anywhere in the posts.
        if any(_FUNDING.search(x) for x in texts):
            fams.add("funding")
        # GTM build: a marketing/growth leadership role present (ideally alongside sales).
        if any(_MKTG_LEAD.search(t) for t in titles):
            fams.add("gtm_build")
        # Org maturity: churn/backfill language, OR a junior->senior ladder in sales.
        if any(_CHURN.search(x) for x in texts) or (leaders and juniors):
            fams.add("org_maturity")
        # Geo expansion: explicit expansion language, OR the SAME role open in 2+ real geos.
        if any(_GEO.search(x) for x in texts):
            fams.add("geo_expansion")
        else:
            by_role_geo: dict = defaultdict(set)
            for r in rec["roles"]:
                loc = re.sub(r"\(.*?\)", " ", (r.get("location") or "")).strip()
                city = re.split(r"[,/|]", loc)[0].strip()
                if city and not _GENERIC_LOC.match(city):
                    by_role_geo[_norm(r["title"])].add(city.lower())
            if any(len(geos) >= 2 for geos in by_role_geo.values()):
                fams.add("geo_expansion")
        if not fams:
            continue
        base = sum(_FAMILY_WEIGHT[f] for f in fams)
        n = len(fams)
        conf = 25 if n >= 3 else (15 if n >= 2 else 0)
        score = min(100, base + conf)
        dates = [r["posted_date"] for r in rec["roles"] if r.get("posted_date")]
        out.append({
            "company": rec["company"],
            "score": score,
            "n_families": n,
            "confluence": n >= 2,
            "families": sorted(fams),
            "signals": [_FAMILY_LABEL[f] for f in sorted(fams)],
            "top_role": leaders[0] if leaders else (titles[0] if titles else ""),
            "role_count": len(rec["roles"]),
            "sample_roles": titles[:6],
            "sources": sorted(s for s in rec["sources"] if s),
            "freshest_posted": max(dates) if dates else None,
            "read": _read(fams, leaders[0] if leaders else ""),
            "enriched": False,
        })
    out.sort(key=lambda x: (-x["score"], x["company"]))
    return out
