# System Architecture

## Architecture Overview
The tool follows a **Service-Oriented Architecture (SOA)** approach within a single repository. The goal is to isolate volatile external API logic (Apollo, Google, Azure OpenAI) from the core business workflows.

### 1. Data Flow Pipeline
1. **Intake**: A candidate uploads a resume → `services/candidate_intelligence` parses it.
2. **Strategy**: `services/lead_calibration` generates and optimizes Apollo search filters.
3. **Discovery**: `services/lead_discovery` fetches matching leads in batches.
4. **Validation**: `services/lead_scoring` uses LLMs to rank leads by relevance.
5. **Enrichment**: `services/enrichment` backfills missing contact data.
6. **Execution**: `services/email_campaign` generates personalized templates and sends them via `services/authentication`.

### 2. Service Modularity
Each folder under `services/` is designed to be self-contained:
- It contains its own `README.md` explaining its specific domain.
- It relies on `services/shared/` for universal utilities (AI clients, generic schemas).
- It communicates with the database layer via `database/models.py`.

### 3. API Design
- **Thin Controllers**: The files in `api/` contain minimal logic, focusing on request validation and response formatting. All complexity is delegated to the services.
- **Standardized Responses**: All endpoints return JSON objects with a consistent `status` field.

### 4. Cross-Cutting Concerns
- **Logging**: Every service call is logged with structured metadata (timestamp, service_name, result) using the `core/logging.py` module.
- **Configuration**: All secrets and environment-specific toggles are managed through the `Settings` object in `core/config.py`.
