# InternReach Job Outreach Tool

## Overview
The InternReach Job Outreach Tool is an AI-driven automation platform designed to help candidates find, score, and contact relevant leads for job opportunities. 

This repository contains a clean, production-grade backend implementation extracted from a rapid prototyping environment. It prioritizes modularity, structured logging, and strict separation of concerns.

## Key Features
- **Candidate Intelligence**: Fast resume parsing and AI-driven career profiling.
- **Lead Discovery**: Seamless integration with Apollo.io for high-precision lead searches.
- **Adaptive Calibration**: Intelligent feedback loop to optimize search filters for target lead counts.
- **AI Scoring**: High-context lead evaluation using Azure OpenAI.
- **Campaign Management**: Automated email outreach via Gmail OAuth integration.

## Folder Structure
- `services/`: Core logic modules (scoring, discovery, auth, etc.).
- `api/`: Thin FastAPI route modules.
- `core/`: Global configuration and logging.
- `database/`: Table schemas and SQLAlchemy models.
- `migrations/`: Versioned SQL migration scripts.
- `logs/`: Structured JSON log files.

## Environment Setup
1. **Initialize .env**: Copy the template to `.env` (already done if using the provided backend extraction).
   ```bash
   cp .env.example .env
   ```
2. **Populate Secrets**: Open `.env` and replace `YOUR_VALUE_HERE` with your actual API keys and credentials.
   - `DATABASE_URL`: PostgreSQL connection string.
   - `APOLLO_API_KEY`: Found in your Apollo.io settings.
   - `GMAIL_CLIENT_ID/SECRET`: From Google Cloud Console OAuth credentials.
   - `AZURE_OPENAI_KEY/ENDPOINT`: From your Azure portal.

The application uses `pydantic-settings` to validate these variables at startup. If any required variables are missing or incorrectly formatted, the application will log a critical error and exit.

---

## Documentation Links
- [Architecture Details](SYSTEM_ARCHITECTURE.md)
- [Setup & Installation](SETUP.md)
