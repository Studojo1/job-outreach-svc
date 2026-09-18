# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Build & Run Commands

### Backend (FastAPI)
```bash
uvicorn api.main:app --reload --port 8000    # development
pip install -r requirements.txt               # install deps
```

### Frontend (Studojo1/frontend — Remix)
The production frontend lives in a **separate GitHub repo** (`Studojo1/frontend`). To make changes:
```bash
git clone https://github.com/Studojo1/frontend /tmp/studojo-frontend-[purpose]
cd /tmp/studojo-frontend-[purpose]
git checkout staging        # always work on staging first
# make changes, then:
git add <files>
git commit -m "feat/fix: description"
git push origin staging
# verify on studojo.pro, then merge to main for production
```

The `frontend/` directory in THIS repo is a local reference copy only. Deploying it does nothing.

### Database
SQL migration scripts in `migrations/` (sequential: `001_*.sql`, `002_*.sql`, ...). No ORM migration tool — apply manually via psql.

### Docker
```bash
docker build -t job-outreach-svc .                    # backend only
docker build -t job-outreach-frontend ./frontend      # local reference only, NOT for deployment
```

---

## Deployment Rules

**All deployments go through GitHub Actions. Never use manual docker builds or kubectl.**

| Branch | Namespace | Domain | Workflows |
|--------|-----------|--------|-----------|
| `main` | `studojo` (production) | studojo.com | `deploy.yml`, `deploy-frontend.yml` |
| `staging` | `staging` | studojo.pro | `deploy-staging.yml`, `deploy-staging-frontend.yml` |

- Images tagged with `github.sha` — no `:latest` or custom tags
- Registry: Azure Container Registry (`acrstudojo-dhfsdrfhf6a6bbg2.azurecr.io`)
- Cluster: Azure Kubernetes Service (`studojo-aks` in `rg-studojo`)
- Frontend build-args baked at build time: `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_AUTH_URL`, `NEXT_PUBLIC_PLATFORM_URL`
- Typical deploy time: ~3 minutes after push

**Workflow**: always push to `staging` first, verify on studojo.pro, then merge to `main` for production. Never push untested changes directly to `main`.

### Admin Panel (Studojo1/admin-panel) — CRITICAL BRANCH MAPPING

The admin panel has its OWN separate branch/domain mapping that is **inverted from the main platform**:

| Branch | K8s Namespace | Domain | Who uses it |
|--------|--------------|--------|-------------|
| `staging` | `staging` | **admin.studojo.pro** | You (testing) |
| `main` | `studojo` | **admin.studojo.com** | Production |

> **WARNING**: The user ALWAYS tests on `admin.studojo.pro` (staging branch).
> Pushing only to `main` of `Studojo1/admin-panel` deploys to `admin.studojo.com` — the user will see ZERO changes.

**When making any admin panel change:**
1. Clone from `https://github.com/Studojo1/admin-panel` to `/tmp/studojo-admin/`
2. Make changes on `main`
3. Push `main` → also push or merge into `staging` **in the same step**
4. Verify on `admin.studojo.pro` (staging namespace)
5. `admin.studojo.com` already has main deployed — no extra step needed

**Shortcut — always push to both branches:**
```bash
cd /tmp/studojo-admin
git add <files> && git commit -m "fix: ..."
git push origin main
git push origin staging   # ← REQUIRED or user sees nothing on admin.studojo.pro
```

Or merge approach (if branches have diverged):
```bash
git checkout staging && git merge main --no-edit && git push origin staging
git checkout main
```

---

## Architecture

### Backend
FastAPI app mounted at root path `/job-outreach`. Routes under `api/routes_*.py`, business logic in `services/` (modular, self-contained). Config validated at startup via Pydantic (`core/config.py`) — missing required env vars will crash the app.

Key services:
- `candidate_intelligence` — resume parsing via Azure OpenAI
- `lead_discovery` — Apollo.io API, finds hiring managers
- `lead_scoring` — 5-dimension heuristic scoring
- `enrichment` — contact backfill (email/phone)
- `email_campaign` — Gmail OAuth sender + AI email generation

### Frontend — Studojo1/frontend (CRITICAL)

**Repo**: `https://github.com/Studojo1/frontend`
**Framework**: Remix (React Router v7)
**Always clone fresh to a temp dir. Never edit local files and assume they deploy.**

#### Key Routes
| Route | Purpose |
|-------|---------|
| `/` | Homepage (home.tsx) |
| `/outreach` | Outreach tool landing page |
| `/outreach/onboarding/upload` | Resume upload — first step of the funnel |
| `/outreach/onboarding/*` | Full onboarding flow |
| `/outreach/campaign/*` | Campaign setup, dashboard, launching |
| `/outreach/leads/*` | Lead discovery and results |
| `/outreach/enrichment` | Contact enrichment |
| `/dojos/internships` | Internship Dojo (job board, free) |
| `/dojos/careers` | Resume Maker (free) |
| `/dojos/ai-risk` | AI Risk Dojo — job replacement probability tool (free) |
| `/reports` | Reports marketplace (free) |
| `/resumes/new` | Resume builder |
| `/auth` | Sign in / sign up |
| `/onboarding` | Post-signup onboarding redirect |

#### Homepage Structure (as of April 2026)
Sections in order (all in `app/routes/home.tsx` + `app/components/home/`):
1. `AnnouncementBar` — inline in home.tsx, purple strip above nav
2. `Header` — `app/components/common/header.tsx`
3. `Hero` — 2-col: copy left, 3 floating app-sim cards right
4. `TrustStrip` — "Positive replies from" + company names
5. `StepsSection` — 3 outreach steps
6. `ProblemSolution` — portal vs Studojo 2-col comparison
7. `FeaturedProductCard` — Outreach Dojo feature card (no pricing shown)
8. `FreeToolsSection` — Internship Dojo, Resume Maker, AI Risk Dojo, Reports
9. `TestimonialsSection` — 6 student testimonials
10. `CTABanner` — "Summer 2026 won't wait for you."
11. `Footer` — `app/components/common/footer.tsx`

#### Routing Intent (homepage CTAs)
- **Announcement bar + hero "Get Internship" button** → `/outreach` (landing page — user may still need convincing)
- **All other CTAs** (ProblemSolution, FeaturedProductCard, CTABanner, InternshipPopup) → `/outreach/onboarding/upload` (direct to upload — user is already convinced)
- **Nav "Outreach" link** → `/outreach` (landing page)
- **Free tools** → their own routes (not outreach)

#### Design System
- **Fonts**: Clash Display (headings, `font-['Clash_Display']`) + Satoshi (body, `font-['Satoshi']`)
- **Shadows**: brutalist hard offset — `shadow-[4px_4px_0px_0px_rgba(25,26,35,1)]`, `shadow-[6px_6px_0px_0px_rgba(25,26,35,1)]`, `shadow-[8px_8px_0px_0px_rgba(25,26,35,1)]`
- **Borders**: `border-2 border-neutral-900` everywhere
- **Border radius**: `rounded-2xl`, `rounded-[32px]`, `rounded-[40px]`, `rounded-[45px]`
- **Primary colour**: `violet-500` / `bg-violet-500`
- **Hover interaction**: `hover:translate-x-[2px] hover:translate-y-[2px] hover:shadow-[2px_2px_0px_0px_rgba(25,26,35,1)]`
- **Never** use soft shadows, thin borders, or Bricolage Grotesque/DM Sans — those belong to a design concept that was NOT adopted

#### Content Rules (homepage)
- No pricing ($20) shown anywhere on the homepage
- No em dashes anywhere in rendered text
- "AI Risk Dojo" is about **job replacement probability by AI** and smart upskilling — NOT an AI content detector
- Trust strip label: "Positive replies from" (not "Placed at" — we get replies, not placements)
- No "Assignment Dojo", "Revision Dojo", or "Humanizer Dojo" on the homepage — these are inactive/broken

#### Auth Flow
BetterAuth session cookie → `ensureAuthToken()` exchanges for JWT → stored in localStorage → Axios interceptor attaches to all API calls. The `useAuth` hook handles this; redirect to platform login only fires after loading completes (not during fetch).

### Database
PostgreSQL via SQLAlchemy 2.0. Key models in `database/models.py`:
- `User`, `Candidate`, `Lead`, `LeadScore`
- `Campaign`, `EmailSent`, `OutreachOrder`, `PaymentOrder`
- `EmailAccount` — stores Gmail OAuth tokens per user

### State Machines
- **Order**: `created → leads_generating → leads_ready → enriching → enrichment_complete → campaign_setup → email_connected → campaign_running → completed`
- **Campaign**: `draft → running → paused → completed`

### Data Pipeline
Resume upload → candidate profiling (Azure OpenAI) → lead discovery (Apollo) → scoring → enrichment → email campaign (Gmail OAuth) → reply tracking

---

## Active Products

| Product | Status | Route | Price |
|---------|--------|-------|-------|
| Outreach Dojo | Active, paid | `/outreach` | $20 one-time |
| Internship Dojo | Active, free | `/dojos/internships` | Free |
| Resume Maker (Careers Dojo) | Active, free | `/dojos/careers` or `/resumes/new` | Free |
| AI Risk Dojo | Active, free | `/dojos/ai-risk` | Free |
| Reports | Active, free | `/reports` | Free |
| Assignment Dojo | Inactive / broken | `/dojos/assignment` | — |
| Revision Dojo | Inactive / coming soon | — | — |
| Humanizer Dojo | Inactive / coming soon | — | — |

Do not surface inactive products anywhere on the homepage or in navigation.

---

## Key Patterns

- Auth dependency injection: `get_current_user` in `api/dependencies.py`
- Background jobs: enrichment and discovery run async, polled from frontend
- `STUDOJO_BASE` (`NEXT_PUBLIC_PLATFORM_URL`): prefix for links to main platform (empty string in production = same origin)
- Navbar Home/brand/sign-out links hardcode `/` (not basePath-prefixed) to navigate to root domain
- Frontend API calls use relative paths (`/api/v1/outreach/*`) — requires ingress routing on the domain
- Reply tracking: campaign worker scans inbox using `after_epoch=last_reply_check_at`. To backfill old replies, fetch threads directly via `GET /gmail/v1/users/me/threads/{thread_id}` stored on `emails_sent`

---

# PROJECT ENFORCEMENT RULES (CRITICAL)

## SOURCE OF TRUTH

- GitHub is the ONLY source of truth
- No local-only changes allowed
- All deployments MUST go through GitHub Actions

### Studojo Main Platform (Studojo1/frontend)

When working on the main Studojo platform (studojo.com), ALWAYS:
- Clone from GitHub (`Studojo1/frontend`) to a temp directory
- Read files from the clone, NOT from any local path
- Make changes in the clone, commit, and push to GitHub
- NEVER reference local files unless the user explicitly says "here is a reference file on my PC"

### Admin Panel (Studojo1/admin-panel)

When working on the admin panel, ALWAYS:
- Clone from GitHub (`Studojo1/admin-panel`) to `/tmp/studojo-admin/`
- The user tests on `admin.studojo.pro` = **staging branch** = `-n staging` K8s namespace
- ALWAYS push to BOTH `main` and `staging` branches — pushing only `main` deploys to `admin.studojo.com` and the user will see no changes
- See the "Admin Panel branch mapping" table in the Deployment Rules section above

---

## DEPLOYMENT RULES

### Staging first, always
1. Push changes to `staging` branch
2. Verify on studojo.pro
3. Merge to `main` via PR
4. Verify on studojo.com

### Never push directly to main without staging verification.

---

## FORBIDDEN ACTIONS

Claude MUST NEVER:

- Run docker build locally for deployment
- Run kubectl set image manually
- Modify running pods directly
- Bypass GitHub Actions
- Push to `main` without first verifying on staging
- Show pricing on the homepage
- Add em dashes to any rendered homepage text
- Surface Assignment Dojo, Revision Dojo, or Humanizer on the homepage

If a forbidden deployment action is required:
→ STOP and ask for confirmation

---

## API RULES

- Only use relative paths: `/api/v1`, `/api/auth`
- Never hardcode: `https://api.studojo.com`

---

## DEBUGGING RULES

Before fixing anything:
1. Identify root cause
2. Check environment (staging vs prod)
3. Check GitHub Actions run status: `gh run list --branch [branch] --limit 5`
4. Inspect Network tab + Console logs

Never apply blind fixes.

---

## CHANGE POLICY

Every change must follow:

1. Minimal scope — don't touch what wasn't asked
2. Commit to `staging` branch first
3. Push to GitHub, let CI/CD deploy
4. Verify on studojo.pro
5. Merge to `main`, verify on studojo.com

---

## COUPONS

Admin coupon for Google OAuth verification: `OAuth100` (100% off, unlimited uses, active). Do not deactivate this coupon.
