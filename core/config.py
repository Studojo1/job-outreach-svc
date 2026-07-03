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
    DODO_PRODUCT_OUTREACH: str = ""  # Single product with pay_what_you_want enabled

    # REDIS
    REDIS_URL: str = "redis://localhost:6379/0"

    # FRONTEND
    FRONTEND_URL: str = "http://localhost:3000"

    # OBSERVABILITY
    SENTRY_DSN: str = ""
    SERVICE_NAME: str = "job-outreach-svc"

    # POSTHOG
    POSTHOG_KEY: str = ""
    POSTHOG_HOST: str = "https://eu.i.posthog.com"

    # LINKEDIN OUTREACH
    # Generate: python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
    LINKEDIN_ENCRYPTION_KEY: str = ""  # base64-encoded 32-byte AES key
    LINKEDIN_PROXY_URL: str = ""  # e.g. http://user:pass@host:port or socks5://...
    # Mesa post-scraper burner session (separate from linkedin_outreach tokens).
    # A single shared burner li_at reused for ALL Mesa LinkedIn post searches;
    # refreshed via the login flow when it expires.
    MESA_LI_AT: str = ""
    MESA_JSESSIONID: str = ""
    # Reddit hiring-signal OAuth app (reddit.com/prefs/apps -> "script").
    # client_credentials works app-only; add user/pass for password grant.
    MESA_REDDIT_CLIENT_ID: str = ""
    MESA_REDDIT_SECRET: str = ""
    MESA_REDDIT_USER: str = ""
    MESA_REDDIT_PASS: str = ""
    MESA_REDDIT_UA: str = "mesa-hiring-signal/0.1 (by Studojo)"
    # X/Twitter hiring-signal burner (separate throwaway account). Cookie auth:
    # auth_token + ct0 (csrf) captured from a logged-in x.com session.
    MESA_X_AUTH_TOKEN: str = ""
    MESA_X_CT0: str = ""

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
