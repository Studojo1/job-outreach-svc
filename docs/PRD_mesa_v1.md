# PRD — Mesa (working name)
## The AI Placement Intelligence Platform for Training & Placement Teams

| | |
|---|---|
| **Status** | Draft v1 — for review |
| **Owner** | Pranav Hegde |
| **Date** | 6 July 2026 |
| **Working name** | "Mesa" (already used internally for concierge runs — rename freely) |
| **Scope** | v1 product definition. No outreach execution. New product; not an evolution of job-outreach-svc. |

---

## 1. Problem

Training institutes and placement consultancies (Sharpener-type organisations) place students and professionals into companies. Their placement and BD teams answer the same questions every batch cycle:

- Which companies should we target this week for this candidate / this cohort?
- Are they actually hiring right now? What's the evidence?
- Who is the right person inside — and what's their phone number and email?
- Which companies should become recurring hiring partners?

Today this is done with LinkedIn searches, Naukri, Lusha/Apollo seats, spreadsheets, and gut feel. A BD associate takes days to build a target list that goes stale in two weeks. Contact tools answer "who matches these filters" — nobody answers **"who should we contact this week, and why."**

We have already delivered this manually (the Mesa concierge engagements: ranked companies + named contacts + phone numbers + `why_now` + `signal_rationale` + suggested openings, delivered as xlsx). This product automates that deliverable and puts it behind a chat interface the team uses daily.

## 2. Users

**Buyer**: Head of Placements / BD head at a training institute or placement consultancy (non-college).
**Users**: placement associates and BD associates. One shared workspace per institute in v1 — single login, everyone sees all chats, tables, and enriched contacts. No per-member accounts in v1.

**Jobs to be done**:
1. Place one specific candidate (junior through executive).
2. Place a cohort at volume ("40 technical support reps — who absorbs them at mass?").
3. Create opportunities for an exceptional candidate where no posting exists (reach the founder).
4. Build a hiring-partner pipeline (BD motion — companies worth a partnership/MoU).
5. Generate raw calling lists at volume (10,000 leads/month for tele-calling teams).
6. Utility enrichment: paste a LinkedIn URL or upload a CSV → phone/email. Exists so the customer can cancel Lusha/Apollo. Never the pitch.

**Budget displaced**: Lusha (₹42–55/phone, ₹8–12/email), Apollo seats, manual research hours.

## 3. Product principles

1. **Intelligence is the USP.** Enrichment is plumbing that makes us the only tool they need.
2. **Every recommendation is evidence-backed.** The agent never pretends to know; it retrieves, then reasons. No post-hoc justification of scores — evidence first, score second.
3. **Recall over precision in retrieval; precision in reasoning.** Noise is acceptable at the evidence layer. Missing a hiring company is not.
4. **Agent, not chatbot.** It asks clarifying questions when a load-bearing fact is missing, plans before executing, and states what it's doing. Stated facts always override inferred ones.
5. **Async by design.** Deep answers take 5–10+ minutes. The UI makes waiting read as an analyst working, never as latency.
6. **Click-gated spend.** Intelligence is shown; contact data costs a credit, spent knowingly (single or bulk), from a prepaid pool. Never spend a contact credit the user didn't ask for.
7. **Specificity and vastness are different machines.** 10 curated companies and 10,000 harvest leads use different pipelines; the agent picks the mode and says so.

## 4. The interface

Three panels, ChatGPT-familiar:

- **Left — history.** All chats for the workspace (shared across the team). Reopening a chat restores its tables.
- **Centre — conversation.** Prompts, clarifying questions, the agent's live plan and progress stream, summaries.
- **Right — the artifact panel.** Stateful tables the agent creates and edits. This is the deliverable.

**Right-panel table rules** (load-bearing):

- Tables are persistent objects attached to the chat, rendered from structured tool output — never parsed from prose.
- Follow-up questions **mutate the existing table**: "are these hiring right now?" adds a `hiring_evidence` column; "did they raise?" adds `funding`; "why these?" fills `why_now`. Column-by-column conversational enrichment is the core interaction.
- Rows support: select, enrich (1 credit), bulk-enrich selection/all (N credits, count shown on the button), status, owner, notes.
- Rows appear **incrementally during execution**, not all at once at the end.
- Every table exports to xlsx at any time.

**File import**: resume PDF/DOCX (single candidate), XLSX/CSV (cohort sheets, bulk-enrich lists). Parsed by LLM into structured mandate inputs.

## 5. Flagship flows (v1 scope — all six)

Each flow: prompt → clarify (only if load-bearing facts missing, max 2–3 questions) → plan + scope statement → streamed execution → table → follow-ups.

### F1 — Single candidate placement (the demo flow)
> "Here's Rahul's resume. Find the best companies for him in Bangalore."

Agent parses resume → asks only what's missing and material (e.g. expected comp band, joining timeline) → curation mode: sweeps job postings + hiring posts + funding news for fit companies → deep-dives shortlist → identifies the right contact per company (policy table §7) → table of ~10–25 companies with evidence, fit rationale, contact (unenriched) → user enriches selectively or in bulk.

### F2 — Cohort mass placement
> "I have 40 technical support reps graduating in 3 weeks. Find companies that can absorb them at mass."

Clarify: locations, comp band, batch deadline. Absorption-focused retrieval: bulk-hiring evidence (walk-in drives, "mass hiring", BPO/support expansion news, multiple simultaneous postings for the same role), company scale signals. Output ranked by **absorption capacity** (estimated openings vs cohort size), with TA contacts. Long-running (10–30 min); async pattern.

### F3 — Opportunity creation (exceptional candidate)
> "This candidate is exceptional — 12 YOE, ex-Flipkart. Find companies where he *should* exist, even without postings."

No posting-led search. Retrieval targets: recently funded companies in his domain, founder pedigree, growth trajectory, leadership-gap inference. Contact target: **founders** (policy table). Output includes the pitch angle the TPO uses ("bro, I have a very good hire for you" — professionally worded).

### F4 — Partnership pipeline (BD motion)
> "Find 30 companies that should become recurring hiring partners for our Java/testing programs."

Scores repeat-hiring propensity: sustained posting velocity, batch-hiring history, fresher-friendliness, GCC/scale-up expansion. Contact target: **HR/TA leadership** (never engineering). Output framed as partnership targets with MoU angle.

### F5 — Direct enrichment (utility mode)
> Paste LinkedIn URL(s) or upload CSV → phone/email. No intelligence run. Straight to LeadsForge, reveal-cache checked first, credits burned per contact. Also serves "I already know the company — just get me the HR": `find_people` + enrich without the research pipeline.

### F6 — Harvest (vastness mode)
> "I need 10,000 leads this month for our calling team — IT services companies hiring freshers, all-India."

Agent states mode explicitly ("At this volume I optimise coverage over per-company evidence"), states scope and credit implications up front, runs breadth pipelines (job-board sweeps + LeadsForge/Apollo people search at scale + lookalike expansion), light scoring only. Delivers in batches to the table; bulk enrich consumes the prepaid pool with the count always visible before the click.

## 6. The intelligence pipeline (the heart of the product)

### 6.1 Stage model

```
Mandate (chat + files)
  → Clarify (only load-bearing gaps)
  → Strategy (mode: curation | harvest | hybrid; signal plan; source plan)
  → Evidence harvest (Context.dev, multi-archetype, parallel)
  → Entity resolution (evidence → canonical companies)
  → Signal extraction (structured signals w/ dates + sources)
  → Scoring & ranking (evidence-first, mandate-weighted)
  → People identification (T1 evidence-named → T2 city-scoped title → T3 title-only)
  → [user click] Contact enrichment (LeadsForge → waterfall later)
  → Living table (statuses, follow-up mutations, export)
```

The probe-evaluate-adjust loop from the B2C tool generalises here: after the first sweep, the agent samples results, judges quality against the mandate, and adjusts queries/filters before going deep. Loosening is **reasoned** (the agent explains what it relaxed and why), never a blind fallback ladder.

### 6.2 Context.dev playbook

**Endpoint**: `POST https://api.context.dev/v1/web/search`
**Cost**: 1 credit ≈ 10 results; ₹1.5 per 10 credits → **₹0.15/credit; a 40-result fanout search ≈ ₹0.45–0.60**. Cheap, but not free at scale — see §6.4.

**Parameters doctrine** (from our benchmark runs):
| Param | Doctrine |
|---|---|
| `markdownOptions` | **Always on**: `{enabled: true, useMainContentOnly: true, includeLinks: true}`. We want scraped content, never link-only. Links preserved — they carry author/company URLs used for identification. |
| `country` | `"IN"` for all India mandates. |
| `queryFanout` | On for sweeps (recall). Off for surgical lookups (company deep-dive) where fanout wastes credits. |
| `freshness` | Enum: `last_24_hours / last_week / last_month / last_year`. Policy per signal type below. |
| `numResults` | 30–40 for sweeps; 10 for deep-dives. |

**Freshness policy by signal**: job postings & hiring posts → `last_month` (a 21-day mandate uses `last_month`, the closest superset); founder "we're hiring" posts → `last_week`/`last_month`; funding & momentum → `last_year` (decays slowly); company facts → no freshness constraint.

**Source × signal matrix (India-first)**:

| Signal | Primary sources (site: stack) |
|---|---|
| Job postings | `linkedin.com/jobs`, `naukri.com`, `indeed.com`, `wellfound.com`, `cutshort.io`, `instahyre.com`, `boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `ycombinator.com` |
| Hiring announcements (named humans!) | `linkedin.com/posts`, `in.linkedin.com` — **proven in our benchmarks**: full post markdown returns with author identity, sometimes embedded emails/phones. X/Twitter posts as secondary. |
| Funding & momentum | `inc42.com`, `entrackr.com`, `yourstory.com`, `techcrunch.com`, `moneycontrol.com`, economictimes tech. Crunchbase/Tracxn are paywalled — their facts surface reliably through these news mirrors instead. |
| Mass hiring / absorption | `naukri.com` + news: "walk-in drive", "mega hiring", "hiring freshers", BPO/GCC expansion announcements |
| Company facts | company website + careers page, LinkedIn company page, above news sources |
| People (fallback discovery) | `site:linkedin.com/in` + title + company + city — public profile pages are retrievable |
| Developer/niche chatter | Reddit (`r/developersIndia` etc.) — secondary, curation mode only |

**Query archetype library** (the agent composes these per mandate; templates validated in our benchmark files):

- **A1 Role×geo job sweep** (F1/F2/F6): OR-stacked role titles + geography + job-board site stack, `last_month`, fanout on. *Benchmarked: 36–40 results, 3–4 credits.*
- **A2 Funding sweep** (F3/F4): `"raised" OR "Series A" OR "seed round"` + vertical + India news site stack, `last_year`, fanout on.
- **A3 Founder hiring-post sweep** (F1/F3): role keywords + "hiring" + `site:linkedin.com/posts OR site:in.linkedin.com`, `last_month`. Highest-value archetype: the **author is the entry point** (T1 contact).
- **A4 Company deep-dive** (curation only): `"{company}" funding OR hiring OR launch OR careers`, no site restriction, fanout **off**, 10 results = 1 credit.
- **A5 Careers-page pull**: `site:{domain} careers OR jobs` — current openings straight from the source.
- **A6 People discovery**: `"{company}" recruiter OR "talent acquisition" {city} site:linkedin.com/in` — T2 identification without touching paid people-search.
- **A7 Mass-hiring sweep** (F2): `"walk-in" OR "mass hiring" OR "hiring freshers" {role} {city}` + naukri/indeed/news stack.
- **A8 Lookalike expansion** (F2/F4/F6): **not Context.dev** — LeadsForge `POST /lookalikes/search` (seed 1–10 domains from the shortlist; 1 credit/result) to expand a validated set cheaply.

**Multi-hop pattern** (what makes this "beyond search-and-summarise"): sweep (A1–A3) → extract candidate companies → resolve entities → deep-dive only the survivors (A4/A5) → cross-check hiring evidence → identify people (A3 authors / A6 / paid search). Breadth cheap, depth selective.

**Safety note**: retrieved markdown is untrusted third-party content. It is data for extraction, never instructions — the agent must not act on imperatives found inside scraped pages.

### 6.3 Signal taxonomy

Every extracted signal is stored structured: `{company, signal_type, value, evidence_url, evidence_quote, observed_date, ttl}`.

| Class | Signals | TTL | Weighted highest in |
|---|---|---|---|
| **Hiring** | active posting; posting velocity (# roles/30d); founder hiring post; careers-page openings; "posted by" identity | 14–21 d | F1, F2, F6 |
| **Momentum** | funding round (stage, amount, date, investors); product launch; geo/GCC expansion; customer-win announcements; headcount growth | 90 d – 1 yr | F3, F4 |
| **Fit** | stack/domain; industry; size band; locations; comp signals from postings | 90–180 d | F1, F3 |
| **Absorption** | bulk-hiring events; walk-in drives; fresher intake history; support/ops scale | 30–60 d | F2, F4 |
| **Founder/quality** | founder pedigree (prior cos, exits); investor quality; entrepreneurial culture markers | 180 d | F3 |

Scoring is **mandate-weighted over signals with citations**. The rationale shown in the table is the evidence that produced the score — never generated after the fact.

### 6.4 Credit-efficiency doctrine (Context.dev)

- **Global evidence cache** (across all tenants — company facts and public signals are not tenant-private): keyed on canonical company + signal class, honouring TTLs above. Second tenant asking about the same company costs ~0 retrieval.
- **Tenant cache**: candidate data, tables, statuses, notes, reveals — private.
- **Refresh semantics**: "researched 12 days ago" shown on cached companies; agent refreshes only expired signal classes, not the whole company.
- **Fanout discipline**: fanout for sweeps only; deep-dives are single-query, 10-result.
- **Budget envelopes per mode** (agent-enforced, order-of-magnitude):
  - Curation (≤25 companies): ~2–4 sweeps (≈12 cr) + ~15 deep-dives (≈15 cr) + people discovery (≈10 cr) ≈ **~40 credits ≈ ₹6** + LLM tokens. Negligible against a single ₹10 phone reveal.
  - Harvest (10k leads): retrieval via wide sweeps + paid people-search + lookalikes; Context.dev spend stays < ₹500. **Enrichment dominates**: 10k × (₹10 + ₹1.1) ≈ ₹1.11L COGS — the harvest price floor is an enrichment number, not an intelligence number.
- Dedup queries within a run; never re-issue an archetype whose results are already in the run's evidence set.

### 6.5 Entity resolution & the company graph

The unglamorous load-bearing layer. One company appears as a LinkedIn job URL, a Naukri listing, an inc42 article, and a careers page — these must collapse to one entity or every downstream feature (dedup, caching, scoring, "already contacted") breaks.

- Canonical key: normalised domain where known, else normalised name + city with LLM-assisted matching for the ambiguous tail.
- Each entity accumulates: facts, signals (dated, cited), identified people, reveal history (tenant-scoped), table appearances, statuses.
- This graph **is** the cross-chat memory: chat #14 knows what chat #3 researched and what the team already contacted.

## 7. People identification

Three quality tiers, always labelled in the table:

| Tier | Meaning | How found |
|---|---|---|
| **T1 — evidence-named** | This human is attached to the hiring evidence: job-post author, "hiring team" member on the LinkedIn job page, named in the JD/post, careers-page contact | Extracted during evidence harvest (A3 authors, job-page markdown, careers pages) |
| **T2 — city-scoped title match** | Right function, right location: "TA Manager, Bangalore" for a Bangalore posting | A6 (LinkedIn profile discovery) or paid people-search with location filter |
| **T3 — title-only** | Right function at the company, location unconfirmed | Paid people-search |

T1 beats T2 beats T3 — the agent always attempts the tiers in order, and Apollo/Lusha-class competitors structurally cannot do T1. **The Deloitte rule**: for a posting in Bangalore, the fallback is the TA person *operating out of Bangalore*, not the global TA head.

**Paid people-search tools** (identification, not enrichment — no reveal credits): LeadsForge `POST /search` (filters: company domains, job titles, seniorities, lead locations, `maxContactsPerCompany`) with Apollo people-search as alternate. `POST /search/count` sizes harvest runs before committing.

**Decision-maker policy table** (who to identify — mandate × company size). This is TPO-lens, not job-seeker-lens: a BD team pitching partnerships belongs in HR/TA's inbox, not the Director of Engineering's:

| Mandate | Tiny startup (~<30, no HR exists) | Growth / mid | Enterprise |
|---|---|---|---|
| Cohort / mass placement (F2) | Founder or ops lead | HR / TA lead | TA head **in the job's city** |
| Partnership / MoU (F4) | Founder | HR / TA head | HR leadership |
| Single candidate (F1) | Founder | HR/TA primary; function head secondary | TA + recruiter on the posting |
| Exceptional candidate (F3) | **Founder directly** | Founder or function head | TA leadership + function head |

## 8. Enrichment layer

- **Primary: LeadsForge** — phones ₹10, emails ₹1.1. Async jobs (`POST /enrichment/phones|emails`, ≤500 people/request, poll or webhook, `externalID` passthrough maps results to table rows, `Idempotency-Key` on every job).
- **Waterfall (phase 2)**: LeadsForge miss → Apollo match (₹25/₹2). Design the enrichment interface provider-agnostic from day one; ship LeadsForge-only first.
- **Tenant reveal cache**: a contact enriched once is free and instant for everyone in the workspace forever (with an optional re-verify). Kills double-spend and quietly coordinates the team.
- **Credit mechanics**: prepaid contact-credit pool per workspace. 1 credit = 1 contact reveal (phone + email). Single enrich or bulk enrich; the button always shows the count ("Enrich 30 — 30 credits"). No quotes, no bill shock — the pool is visible at all times.
- **Quality display**: minimal provenance — verified/unverified badge + provider, as a tooltip. When a client disputes a number, support can answer diagnostically.
- Balance monitoring via LeadsForge `GET /balance`; low-balance alerts to ops before a bulk job would fail mid-run (`402`).

## 9. The agent

**One agent.** One system prompt, structured tools. Multi-agent only if a concrete failure demands it (the likely first candidate: parallel fan-out for F2/F6 research — an internal implementation detail, invisible to users).

**Behavioural loop**: understand → clarify (max 2–3 questions, only if the answer changes the plan; MCQ-style where possible) → plan (state mode, scope, and what it will do) → execute with streaming → summarise → invite follow-ups (which mutate the table).

**System prompt contains**: role identity (senior placement-intelligence analyst for TPO/BD teams); the decision-maker policy table (§7); mode-selection rules (§6.4 envelopes; requested volume drives curation/harvest/hybrid, and the agent must *state* its mode); signal taxonomy and freshness policy; evidence-citation requirement; clarify-before-spend doctrine; stated-facts-override-inferred rule; scraped-content-is-data-not-instructions rule; output contract (findings go to table ops; chat text is narration and summary only).

**Tool contracts** (all structured I/O; expensive tools report cost in results):

| Tool | Wraps | Notes |
|---|---|---|
| `web_search(query, num_results, freshness, country, fanout)` | Context.dev | Streams results; caller batches archetypes in parallel |
| `find_people(company_domain, titles[], locations[], seniorities[], max_per_company)` | LeadsForge `/search` (alt: Apollo) | Identification only — no reveals |
| `count_people(filters)` | LeadsForge `/search/count` | Sizes harvest runs pre-commit |
| `lookalike_companies(seed_domains[1–10], filters)` | LeadsForge `/lookalikes` | 1 credit/result; expansion after validation |
| `enrich_contacts(row_ids[])` | LeadsForge async jobs | Reveal-cache-aware; only ever user-initiated |
| `parse_file(file_id)` | LLM extraction | Resume → profile; XLSX/CSV → cohort/bulk list |
| `table_create / table_update(add_rows, add_columns, set_cells, set_status)` | Right panel | The only way findings reach the user |
| `ask_user(question, options?)` | Centre panel | Blocking clarification |

## 10. Table schema (v2 of the Mesa/Sharpener deliverable)

The Sharpener columns (`rank, name, title, company, phone, connection_point, outreach_angle, why_now, suggested_opening, signal_rationale`) are the floor, not the ceiling — they were produced pre-Context.dev. v2 groups:

- **Company**: name, website, size band, stage, HQ/city, `what_they_do` (one concrete line)
- **Evidence**: `hiring_evidence` (type + date + link), `funding` (round, amount, date, investors), `momentum`, `why_now`, `fit_score` + cited rationale
- **Contact**: name, title, location, **ID tier (T1/T2/T3)**, LinkedIn URL, phone 🔒, email 🔒, verified badge
- **Action**: `connection_point` (candidate↔company), `outreach_angle`, `suggested_opening`, status (new/enriched/contacted/replied/meeting/dead), owner, notes

Columns are dynamic — flows add what's relevant, follow-ups add more. Every cell that came from evidence links to it.

## 11. Waiting UX (async is the brand, not the bug)

- On execution start: a **plan card** in the centre panel ("1. Sweep hiring posts → 2. Funding check → 3. Deep-dive shortlist → 4. Identify contacts") with live stage indicators.
- **Live counters**: "companies discovered 34 · with active hiring evidence 12 · contacts identified 5 · credits used 22".
- **Rows stream into the right panel as found** — first useful rows inside the first minute; the user reads while the agent works.
- Narration lines as stages complete ("Found 9 companies that raised in the last 6 months — deep-diving the top 15 now").
- Long runs (F2/F6) continue if the user navigates away; chat list shows a running indicator; completion notification.
- Failures degrade gracefully: partial tables are delivered with a note about what's missing, never a blank error.

## 12. Credits & commercial mechanics (v1 mechanics only — pricing itself deferred)

- **Contact credits**: prepaid pool per workspace; 1 credit = 1 reveal; visible balance; single + bulk spend, count always shown.
- **Intelligence credits**: meter research tasks, sized by scope (companies researched), not per question. High-volume deals: intelligence bundled free (COGS ₹6/mandate is negligible — §6.4). Low-volume deals: charged.
- COGS anchors for pricing later: curation mandate ≈ ₹6 intelligence + ₹11.1/contact enriched; 10k harvest ≈ ₹1.11L enrichment COGS. Lusha-equivalent value of one revealed contact: ₹50–67.

## 13. Non-goals (v1)

- Outreach execution of any kind (email/LinkedIn/calls) — the institute does its own outreach.
- Per-member accounts, roles, permissions.
- Colleges as a customer segment.
- Chrome extension / LinkedIn overlay (v2 candidate for utility mode).
- Weekly briefs / standing watchlists (v1.5 candidate — the signal TTLs and cache are designed so this drops in later).
- Apollo waterfall (phase 2; interface is provider-agnostic from day one).
- Automatic outcome logging. Statuses exist for team coordination; anything the team records is exhaust we learn from, never a gate.

## 14. Success metrics

| Metric | Target intuition |
|---|---|
| Time to first row in the table | < 60 s |
| Curation mandate completion | < 10 min |
| **T1 identification rate** (companies with an evidence-named contact) | The quality differentiator — track from day one |
| Email validity / phone connect rate | > 85% / > 70% — the number the buyer judges us on |
| % of surfaced contacts enriched | Activation: are recommendations worth paying for? |
| Weekly active mandates per workspace | Retention: daily tool or episodic report generator? |
| Intelligence COGS per mandate | Stay inside §6.4 envelopes |

## 15. Risks & open questions

**Risks**
1. **LinkedIn retrievability via Context.dev** is the backbone of T1 identification and A1/A3 — coverage/scrape success must be monitored continuously (scrape_code rates); degrade to T2/T3 gracefully.
2. **LeadsForge India phone accuracy** — unproven at volume. Measure connect rates from week one; the Apollo waterfall is the hedge.
3. **Harvest-mode quality** — 10k leads with light scoring can disappoint if sold as "intelligence". Mode transparency (§5 F6) is the mitigation; the agent must set expectations in-chat.
4. **Prompt injection via scraped content** — treated as data, never instructions (§6.2).
5. **Shared-login workspace** — no per-user attribution of credit spend; acceptable for v1, revisit with first multi-team customer.

**Open questions**
1. Context.dev full endpoint inventory beyond `/v1/web/search` (dedicated scrape/contents endpoint? rate limits? exact credit table per endpoint) — determines whether A4/A5 use search or direct fetch.
2. LeadsForge `/search` coverage for Indian SMBs vs Apollo — decides the default identification provider.
3. Real name for the product.
4. Intelligence-credit unit final definition (proposal in §12: per research task, scope-sized).

## 16. Rollout

1. **Phase 0 — dogfood**: run the next 3–5 Mesa concierge engagements *through the product* (internal use). Every gap found is a real client's gap. The concierge xlsx deliverables become the regression baseline: the product must beat them.
2. **Phase 1 — design partners**: 2–3 Sharpener-type institutes on the shared workspace, prepaid credit pools, weekly feedback loop. Demo flow: F1 (candidate → companies → contacts, live).
3. **Phase 2 — GA** for training institutes and placement consultancies; utility mode (F5) marketed as "cancel your Lusha seat" only after accuracy metrics clear §14 targets.
