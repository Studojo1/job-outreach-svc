# Bob (Mesa) — Knowledge Transfer Doc

_Last updated: 2026-07-09. Owner: Pranav. Status: v0 live on **staging only** (studojo.pro/bob). Production deploy happens only on Pranav's explicit go._

---

## 1. What Bob is

Bob is the working codename for **Mesa**, a chat-first AI placement-intelligence tool for the placement/BD teams of training institutes (NOT for students, NOT for colleges). A placement person types a mandate ("find 15 live AI/ML internships in Bengaluru for these two students", with resumes attached) and Bob returns a **stateful table** of companies with live hiring evidence, fit scores, and named hiring-side contacts — streamed row by row while the agent works.

- Product spec: `docs/PRD_mesa_v1.md` (read this first for the product thinking)
- Retrieval audit + lab results: `docs/AUDIT_retrieval_2026-07-07.md`
- v1 does **no outreach** and **no paid contact enrichment** (LeadsForge phone/email reveals are a later phase, deliberately not wired)

## 2. Repos and where things live

| Piece | Repo / path | Notes |
|---|---|---|
| Backend (agent, API, retrieval) | `Studojo1/job-outreach-svc`, branch **staging** | Everything below lives here |
| Agent core | `services/bob/agent.py` | System prompt, tools, quality rails, run loop |
| Free LinkedIn layer | `services/bob/livecheck.py` | Guest job search, job reader, liveness |
| Hosted boards | `services/bob/job_boards.py` | Ashby/Greenhouse/Lever public JSON |
| Unstop | `services/bob/unstop.py` | Public internships API |
| Contacts | `services/bob/leadsforge.py` | LeadsForge people search (free tier only) |
| Context.dev client | `services/bob/contextdev_client.py` | Web search + scrape, credit cache |
| File parsing | `services/bob/files.py` | PDF/DOCX/XLSX/CSV resume extraction |
| Schema bootstrap | `services/bob/schema.py` + `migrations/040_bob_mesa.sql` | Tables self-provision on first request; CI runs NO migrations |
| HTTP API | `api/routes_bob.py` | Mounted at `/bob`, reached via `/api/v1/outreach/bob/*` (ingress rewrite) |
| Frontend | `Studojo1/frontend`, branch **staging**, `app/routes/bob.tsx` | Remix; clone fresh to /tmp to edit, never edit local copies |
| Deploy workflows | `.github/workflows/deploy-staging.yml` / `deploy.yml` | Build → ACR → `kubectl set image/env` on AKS |

**Access**: shared workspace code, sent as `X-Bob-Key` header. Stored in GH Actions secret `BOB_ACCESS_CODE` (ask Pranav for the current value).

## 3. How it works (architecture)

```
user message ──► POST /bob/chats/{id}/messages ──► bob_runs row + background thread
                                                        │
                       agent loop (Azure OpenAI gpt-5-mini, function calling)
                       tools: web_search / scrape_page (Context.dev, PAID)
                              search_linkedin_jobs / read_linkedin_job (FREE)
                              check_job_board / search_unstop / find_contacts (FREE)
                              create_table / add_rows / update_rows / add_columns
                              ask_user / finish
                                                        │
                     every add_rows/update_rows passes DETERMINISTIC RAILS (see §5)
                                                        │
frontend polls GET /bob/runs/{id} every 2.5s ──► events feed + table rows stream in
```

- One run = one background thread. **Pod restarts kill in-flight runs** (no resumable queue yet) — hence the deploy rule in §7.
- Runs stuck "running" >15 min are reaped to error on the next message.
- All state is Postgres (`bob_chats`, `bob_messages`, `bob_runs`, `bob_tables`, `bob_rows`, `bob_files`, `bob_evidence_cache`, `bob_rejections`). Tables create themselves from migration 040 at first request.

## 4. The retrieval stack (which source for what, and why)

This allocation is **measured, not guessed** (A/Bs from 2026-07-09; details in the audit doc):

| Source | Tool | Cost | Use for | Key facts |
|---|---|---|---|---|
| LinkedIn **jobs** | `search_linkedin_jobs` | FREE | Primary jobs source | Hits LinkedIn's logged-out (guest) endpoints — same pages Google crawls. Live-by-construction. Beat Context.dev 57 vs 12 jobs on the same mandate. ~55-77% on-function (LinkedIn fuzzy-matches), so the JD check does the cleaning. **Rate-limits with ~26-byte empty stubs** — code retries; a reported rate-limit is NOT "0 results". |
| LinkedIn **job page** | `read_linkedin_job` | FREE | MANDATORY before shipping any LinkedIn job | Returns live/closed status, full JD (fit is scored from the JD, never the title), and the **job poster** (name/headline/profile) when the hirer enabled messaging — that's a T1 contact. Closed = banner OR missing apply button; poster-module present = live Easy-Apply job. |
| Unstop | `search_unstop` | FREE | Biggest India intern pool, cross-platform breadth | Public no-auth API. **`oppstatus=open` is mandatory** — without it you get 2021-deadline roles still flagged open. Returns structured stipend, deadline, eligibility/batch, skills. Location filtering is client-side; `region=online` = remote. |
| Ashby/Greenhouse/Lever | `check_job_board` | FREE | Verify "we're hiring" claims against the real board | Public JSON; bare board roots (no org path) are invalid everywhere. |
| Naukri/Indeed/Internshala/Wellfound, LinkedIn **posts**, X, funding news | `web_search` (Context.dev) | 1 credit / 10 results, markdown included | Everything the free layer can't reach | This is where credits belong — NOT on `site:linkedin.com/jobs` (measured worse than the guest tool). Posts are gold: full text + author + often an inline email. X: only `/status/` URLs are posts. Freshness = the user's window (default 1 week, cap 1 week unless they ask); drop dead jobs from the markdown "no longer accepting" signal AFTER retrieval. |
| Contacts | `find_contacts` (LeadsForge) | FREE (search tier) | Named hiring-side contacts | See §6 — the trust rules here matter. |

Context.dev economics: ~₹0.15/credit, budget 40 credits/run, DB-backed cache (`bob_evidence_cache`) makes repeat queries free. A good curation run now spends **0-5 credits** (verification run: 16 rows, 3 credits).

## 5. Quality rails (deterministic, in code — the model cannot bypass these)

Doctrine: **every bug class gets BOTH a prompt rule AND a code rail.** The rails live in `agent.py` (`_sanitize_cells`, `add_rows`, `update_rows`, `create_table`):

1. **URL sanitizer** — strips LinkedIn authwall/signup/`company/unavailable`, `lnkd.in`, Telegram/WhatsApp links, Indeed search-slug pages, bare board roots. One URL per `*_url` cell.
2. **Column-domain integrity** — `linkedin_*` columns only accept LinkedIn URLs of the right kind (`contact_*` needs `/in/`, company needs `/company/`); `website` never accepts job boards/socials.
3. **Liveness gate** — LinkedIn-job and hosted-board evidence URLs are re-checked live at `add_rows`; dead rows are rejected outright. Liveness is point-in-time (a job open today can die tomorrow).
4. **Fit floor** — rows with `fit_score < 55` are rejected. Same-run **score-inflation catch**: a company scored below the bar can't come back with a pumped score. (Learned the hard way: fit_score is model-written, NOT objective — never trust a threshold on a self-reported number alone.)
5. **Mandate keyword gate** — `create_table` takes `target_functions`; rows whose role text matches none (word-boundary) are rejected. This is what actually blocks Founder's-Office/Ops/Data-Entry padding.
6. **Rejection ledger** (`bob_rejections`) — any row the user deletes bans that company for that chat; `add_rows` refuses re-adds; the agent sees the removed list every run.
7. **Contact-collision rail** — one person can be at most one company's contact (the "same recruiter on two rows" bug).
8. **Per-author cap** — max 2 rows sourced from one post author (X/LinkedIn aggregator accounts).
9. **Dedupe** — one row per company (parentheticals stripped: "Composio (Ashby)" == "Composio"); one table per chat unless `separate_mandate=true`.
10. **No em/en dashes anywhere** (product rule), placeholder company names ("Early-stage AI startup") rejected.

## 6. Contacts — the trust rules (important)

- `find_contacts` queries LeadsForge with **company + location only, never title keywords** (titles vary; a keyword filter hides the right person — "People & Culture" would never match "HR"). The model picks the best hiring-side person from the full list.
- **LeadsForge company-NAME search is dangerously fuzzy** — it has returned the identical people list for two different companies. Every result carries an `at:` field (the person's real employer); **a contact is only shippable if `at:` matches the target company**. Prefer `companyDomains` over names. In a 12-contact audit, only 2 name-search results were real.
- LinkedIn URLs are NOT in LeadsForge search results (paid enrichment only). The best T1 source is the **job poster on the LinkedIn job page** (free via `read_linkedin_job`, includes profile URL + headline).
- Paid enrichment (phones/emails) is **deliberately not wired**. Do not wire it without Pranav's go.
- API quirk: `POST /search` requires a top-level `limit` field (400 without it). Balance: `GET /balance`. Search is credit-free (verified).

## 7. Deployment (read this before touching anything)

- **All deploys via GitHub Actions.** Push to `staging` branch → build → AKS namespace `staging` → studojo.pro. ~3.5 min. Never docker build/kubectl by hand.
- **A deploy restarts the pod and KILLS any in-flight Bob run.** Check no run is active before pushing (`GET /bob/chats/{id}` → `latest_run.status`). If Pranav is running a client sheet, hold the push.
- Secrets (`CONTEXT_DEV_API_KEY`, `BOB_ACCESS_CODE`, `LEADSFORGE_API_KEY`) live in GH Actions secrets and reach the pod via `kubectl set env` **during a deploy** — rotating a secret requires re-running the deploy workflow.
- CI runs **no migrations**; schema changes go into `migrations/040_bob_mesa.sql` as idempotent statements (`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`) — `ensure_schema()` re-runs the file on pod start.
- Production (studojo.com) deploy = merge staging → main, **only when Pranav says so**.

## 8. API quick reference

Base: `https://studojo.pro/api/v1/outreach/bob` — every call needs `X-Bob-Key: <code>`.

```
POST /auth/verify                       check access code
GET  /chats            POST /chats      list / create chats
GET  /chats/{id}                        messages + latest_run (events, counters) + tables with rows
POST /chats/{id}/messages               send a mandate → {run_id} (409 if a run is active)
POST /chats/{id}/files                  attach resume/xlsx (10MB, text extracted for the agent)
GET  /runs/{id}                         poll status/events/tables
PATCH /rows/{id}        {status}        pipeline status (new/contacted/replied/meeting/dead)
PATCH /rows/{id}/cells  {cells}         direct cell repair (sanitized)
DELETE /rows/{id}                       delete + auto-ban company in this chat's ledger
DELETE /tables/{id}                     drop a table
POST /tables/{id}/rows|columns          admin append (imports/merges)
POST /tables/{id}/scrub                 re-run sanitizer over existing rows
POST /chats/{id}/guardrails             seed rejected companies / mandate keywords
GET  /tables/{id}/export                xlsx download
```

Debugging a bad run: `GET /chats/{id}` → `latest_run.events` is the full tool-call trace (every search, its query, result counts, credits, guard rejections). That trace is how every bug in this doc was found — read it before theorizing.

## 9. Known limitations / roadmap

- **No resumable runs** — deploys and pod restarts kill in-flight runs.
- **LinkedIn guest endpoints are unofficial** — they rate-limit (empty stubs) and could change or block anytime; the agent falls back to Context.dev if they choke. Signals also vary by day (a dead job can re-render an apply button).
- **Contacts at giants (Cisco/Airbus/GE) are thin** — LeadsForge's Bengaluru data for them is mostly engineers; blanks are intentional (empty beats wrong).
- Single shared login per workspace; per-member auth later.
- Deferred by explicit decision: LeadsForge paid enrichment, Apollo waterfall, prod deploy, daily-digest briefs (economics proven: ~1 credit/day).

## 10. Working on Bob — practical loop

1. Clone `Studojo1/job-outreach-svc`, checkout `staging`. Backend-only changes need no local run — the loop is: edit → `python3 -m py_compile` → commit → push staging → ~3.5 min → test on studojo.pro/bob.
2. Test through the real product (a chat + mandate) or the API directly; smoke mandates like "Find 4 Bangalore companies with LIVE frontend intern postings this week, contact for each" cost ≤5 credits.
3. When a run misbehaves: pull the event trace (§8), find the exact query/tool call that went wrong, then fix **both** the prompt rule and the code rail.
4. Free modules (`livecheck`, `unstop`, `job_boards`, `leadsforge`) are importable standalone for quick local tests (they only need `requests`).
