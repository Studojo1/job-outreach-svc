# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run Commands

### Backend (FastAPI)
```bash
uvicorn api.main:app --reload --port 8000    # development
pip install -r requirements.txt               # install deps
```

### Frontend (Next.js 14, basePath: /outreach)
```bash
cd frontend
npm ci --legacy-peer-deps    # install deps
npm run dev                  # development (localhost:3000/outreach)
npm run build                # production build
npm run lint                 # eslint
```

### Database
SQL migration scripts in `migrations/` (sequential: 001_*.sql, 002_*.sql, ...). No ORM migration tool — apply manually via psql.

### Docker
```bash
docker build -t job-outreach-svc .                    # backend
docker build -t job-outreach-frontend ./frontend      # frontend (multi-stage, standalone output)
```

## Deployment Rules

**All deployments go through GitHub Actions. Never use manual docker builds or kubectl.**

| Branch | Namespace | Workflows |
|--------|-----------|-----------|
| `main` | `studojo` (production) | `deploy.yml`, `deploy-frontend.yml` |
| `staging` | `staging` | `deploy-staging.yml`, `deploy-staging-frontend.yml` |

- Images tagged with `github.sha` — no `:latest` or custom tags
- Registry: Azure Container Registry (`acrstudojo-dhfsdrfhf6a6bbg2.azurecr.io`)
- Cluster: Azure Kubernetes Service (`studojo-aks` in `rg-studojo`)
- Frontend build-args baked at build time: `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_AUTH_URL`, `NEXT_PUBLIC_PLATFORM_URL`

## Architecture

### Backend
FastAPI app mounted at root path `/job-outreach`. Routes under `api/routes_*.py`, business logic in `services/` (modular, self-contained). Config validated at startup via Pydantic (`core/config.py`) — missing required env vars will crash the app.

Key services: `candidate_intelligence` (resume parsing via Azure OpenAI), `lead_discovery` (Apollo.io API), `lead_scoring` (5-dimension heuristic), `enrichment` (contact backfill), `email_campaign` (Gmail sender + AI email generation).

### Frontend
Next.js App Router with `basePath: '/outreach'` and `output: 'standalone'`. All pages use `'use client'` and are force-dynamic (no static generation). State via Zustand with localStorage persistence (`useAppStore`).

Auth flow: BetterAuth session cookie → `ensureAuthToken()` exchanges for JWT → stored in localStorage → Axios interceptor attaches to all API calls. The `useAuth` hook handles this; redirect to platform login only fires after loading completes (not during fetch).

### Database
PostgreSQL via SQLAlchemy 2.0. Key models in `database/models.py`: User, Candidate, Lead, LeadScore, Campaign, EmailSent, OutreachOrder, PaymentOrder, EmailAccount.

### State Machines
- **Order**: `created → leads_generating → leads_ready → enriching → enrichment_complete → campaign_setup → email_connected → campaign_running → completed`
- **Campaign**: `draft → running → paused → completed`

### Data Pipeline
Resume upload → candidate profiling (Azure OpenAI) → lead discovery (Apollo) → scoring → enrichment → email campaign (Gmail OAuth)

## Key Patterns

- Auth dependency injection: `get_current_user` in `api/dependencies.py`
- Background jobs: enrichment and discovery run async, polled from frontend
- `STUDOJO_BASE` (`NEXT_PUBLIC_PLATFORM_URL`): prefix for links to main platform (empty string in production = same origin)
- Navbar Home/brand/sign-out links hardcode `/` (not basePath-prefixed) to navigate to root domain
- Frontend API calls use relative paths (`/api/v1/outreach/*`) — requires ingress routing on the domain

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

---

## DEPLOYMENT RULES

### Staging
- Branch: staging
- Namespace: staging
- Domain: studojo.pro

### Production
- Branch: main
- Namespace: studojo
- Domain: studojo.com

---

## FORBIDDEN ACTIONS

Claude MUST NEVER:

- Run docker build locally for deployment
- Run kubectl set image manually
- Modify running pods directly
- Bypass GitHub Actions

If such an action is required:
→ STOP and ask for confirmation

---

## API RULES

- Only use relative paths:
  /api/v1
  /api/auth

- Never use:
  https://api.studojo.com

---

## DEBUGGING RULES

Before fixing anything:

1. Identify root cause
2. Check environment (staging vs prod)
3. Inspect:
   - Network tab
   - Console logs

Never apply blind fixes.

---

## CHANGE POLICY

Every change must follow:

1. Minimal scope
2. Commit to correct branch
3. Push to GitHub
4. Let CI/CD deploy
5. Verify before next change