# Setup Guide

## 1. Prerequisites
- Python 3.10+
- PostgreSQL
- Apollo.io API Key
- Azure OpenAI Account
- Google Cloud Project (for Gmail OAuth)

## 2. Installation
1. Clone the repository.
2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install fastapi uvicorn sqlalchemy psycopg2-binary httpx pydantic-settings jsonschema requests PyMuPDF
   ```

## 3. Environment Configuration
1. Copy the template:
   ```bash
   cp .env.example .env
   ```
2. Fill in the required API keys and database credentials in `.env`.

## 4. Database Setup
1. Create a PostgreSQL database named `internreach`.
2. Run the migration scripts in the `migrations/` folder in order:
   ```bash
   psql -d internreach -f database/schema.sql
   # Or run individual migrations
   ls migrations/*.sql | xargs -I {} psql -d internreach -f {}
   ```

## 5. Running the API Server
Start the development server:
```bash
uvicorn api.main:app --reload --port 8000
```
Visit `http://localhost:8000/docs` to view the interactive API documentation.
