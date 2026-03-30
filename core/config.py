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

    # GMAIL OAUTH
    GMAIL_CLIENT_ID: str
    GMAIL_CLIENT_SECRET: str
    GMAIL_REDIRECT_URI: str

    # AZURE OPENAI
    AZURE_OPENAI_ENDPOINT: str
    AZURE_OPENAI_API_VERSION: str
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str
    AZURE_OPENAI_LLM_DEPLOYMENT: str
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
    POSTHOG_HOST: str = "https://us.i.posthog.com"

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
