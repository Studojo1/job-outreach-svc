import logging
import sys
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import ValidationError

# Initialize basic logging for config loading
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("config_loader")

class Settings(BaseSettings):
    # DATABASE
    DATABASE_URL: str

    # APOLLO
    APOLLO_API_KEY: str
    APOLLO_API_KEY_2: str = ""  # fallback key when primary is exhausted
    APOLLO_API_KEY_3: str = ""  # third key slot (optional)

    # MESA job scraping (optional)
    MESA_ATS_BOARDS: str = ""   # override ATS boards: "greenhouse:stripe,ashby:ramp,..."
    ADZUNA_APP_ID: str = ""     # Adzuna aggregated jobs API (source "adzuna")
    ADZUNA_APP_KEY: str = ""
    ADZUNA_COUNTRY: str = "in"

    # GMAIL OAUTH
    GMAIL_CLIENT_ID: str
    GMAIL_CLIENT_SECRET: str
    GMAIL_REDIRECT_URI: str

    # AZURE OPENAI
    AZURE_OPENAI_ENDPOINT: str
    AZURE_OPENAI_API_VERSION: str = "2025-04-01-preview"
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str = "text-embedding-ada-002"
    # Reasoning-capable model used for quality-critical tasks: Bing web research,
    # profiler agent, career strategist, quality probe, email generation, resume parsing.
    AZURE_OPENAI_LLM_DEPLOYMENT: str = "gpt-5-mini"
    # Cheap/fast model used for batch & pattern tasks: justifier, company fit scoring,
    # fact extractor, reply classifier, role/location normalization. ~17x cheaper.
    AZURE_OPENAI_FAST_DEPLOYMENT: str = "gpt-4o-mini"
    # Cold-outreach email writer. Kept on gpt-4o because reasoning models tend to
    # produce over-structured prose; gpt-4o writes warmer, more human cold emails.
    AZURE_OPENAI_EMAIL_DEPLOYMENT: str = "gpt-4o"
    AZURE_OPENAI_KEY: str

    # RAZORPAY
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    RAZORPAY_TEST_MODE: bool = True

    # DODO PAYMENTS
    DODO_PAYMENTS_API_KEY: str = ""
    DODO_TEST_MODE: bool = True
    DODO_WEBHOOK_SECRET: str = ""

    # Meta Conversions API — authoritative server-side Purchase. Both blank by
    # default, which disables it rather than failing a payment.
    META_PIXEL_ID: str = "1402801611979819"
    META_CAPI_TOKEN: str = ""
    DODO_PRODUCT_OUTREACH: str = ""  # Single product with pay_what_you_want enabled

    # REDIS
    REDIS_URL: str = "redis://localhost:6379/0"

    # FRONTEND
    FRONTEND_URL: str = "http://localhost:3000"

    # Public base URL of this service, used to build the open-tracking pixel URL
    # embedded in outreach emails. Must be the externally reachable host that
    # serves /job-outreach/t/{token}.png (ingress), e.g. https://api.studojo.com
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # SERVICE-TO-SERVICE AUTH
    # Shared secret the frontend's server-side loaders send as `x-studojo-internal`
    # next to X-User-Id (Studojo1/frontend app/lib/outreach/server-api.ts, same
    # INTERNAL_API_SECRET name there). X-User-Id is only trusted when this matches.
    # Optional: blank disables the X-User-Id path (requests fall back to the
    # session cookie) instead of failing startup.
    INTERNAL_API_SECRET: str = ""

    # EMAILER (Studojo1/emailer-service). send-template is gated by its own
    # X-Internal-Secret. Blank secret disables the paid-not-launched nudge
    # emails (the routing fix still applies); nothing else sends through it.
    EMAILER_URL: str = "http://emailer-service:8087"
    EMAILER_INTERNAL_SECRET: str = ""
    # Founders who get the paid-not-launched alert. Comma-separated.
    OPS_ALERT_RECIPIENTS: str = "jeremy.zac@gmail.com,businessconnect.pranav@gmail.com"

    # OBSERVABILITY
    SENTRY_DSN: str = ""
    SERVICE_NAME: str = "job-outreach-svc"

    # POSTHOG
    POSTHOG_KEY: str = ""
    POSTHOG_HOST: str = "https://eu.i.posthog.com"
    # Account deletion asks PostHog to delete the person (Privacy Policy §15).
    # Both blank: skipped with a warning. Private API host is derived from
    # POSTHOG_HOST (eu.i.posthog.com -> eu.posthog.com) unless set.
    POSTHOG_PERSONAL_API_KEY: str = ""
    POSTHOG_PROJECT_ID: str = ""
    POSTHOG_API_HOST: str = ""

    # MIXPANEL GDPR deletion API (Privacy Policy §15). Blank: skipped with a
    # warning. MIXPANEL_GDPR_TOKEN is an OAuth token of a project owner.
    MIXPANEL_PROJECT_TOKEN: str = ""
    MIXPANEL_GDPR_TOKEN: str = ""
    MIXPANEL_API_HOST: str = "https://mixpanel.com"

    # AZURE BLOB STORAGE (same account the frontend and control-plane upload
    # resumes to). Used only to delete a deleted account's files. Blank:
    # skipped with a warning.
    AZURE_STORAGE_ACCOUNT_NAME: str = ""
    AZURE_STORAGE_ACCOUNT_KEY: str = ""
    AZURE_STORAGE_CONTAINER_NAME: str = "resumes"
    AZURE_STORAGE_TICKETS_CONTAINER: str = "ticket-screenshots"

    # LINKEDIN OUTREACH
    # Generate: python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
    LINKEDIN_ENCRYPTION_KEY: str = ""  # base64-encoded 32-byte AES key
    LINKEDIN_PROXY_URL: str = ""  # e.g. http://user:pass@host:port or socks5://...
    # Mesa post-scraper burner session (separate from linkedin_outreach tokens).
    # A single shared burner li_at reused for ALL Mesa LinkedIn post searches;
    # refreshed via the login flow when it expires.
    MESA_LI_AT: str = ""
    MESA_JSESSIONID: str = ""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

try:
    logger.info("Initializing environment configuration...")
    settings = Settings()
    logger.info("Configuration loaded successfully.")
except ValidationError as e:
    logger.error("CRITICAL: Environment validation failed!")
    for error in e.errors():
        logger.error(f"  - Missing or invalid variable: {error['loc'][0]}")
    logger.error("The application cannot start without these required variables.")
    sys.exit(1)
except Exception as e:
    logger.error(f"CRITICAL: Unexpected error loading configuration: {e}")
    sys.exit(1)
