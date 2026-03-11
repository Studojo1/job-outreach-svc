import sentry_sdk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes_candidate import router as candidate_router
from api.routes_discovery import router as discovery_router
from api.routes_scoring import router as scoring_router
from api.routes_enrichment import router as enrichment_router
from api.routes_campaign import router as campaign_router
from api.routes_auth import router as auth_router
from api.routes_gmail import router as gmail_router
from core.config import settings
from core.logger import get_logger
from core.middleware import RequestLoggingMiddleware
from core.metrics import metrics_endpoint

logger = get_logger("job_outreach_tool.api.main")

# Sentry
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        traces_sample_rate=0.2,
        environment="production",
        release=f"{settings.SERVICE_NAME}@1.0.0",
    )
    logger.info("Sentry initialized")

app = FastAPI(
    title="Job Outreach Service",
    description="Clean Backend Architecture Implementation",
    version="1.0.0",
    root_path="/job-outreach",
)

# Middleware (order matters — outermost first)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registry
app.include_router(candidate_router, prefix="/api/v1")
app.include_router(discovery_router, prefix="/api/v1")
app.include_router(scoring_router, prefix="/api/v1")
app.include_router(enrichment_router, prefix="/api/v1")
app.include_router(campaign_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(gmail_router, prefix="/api/v1")


@app.get("/health")
def health_check():
    return {"status": "online"}


@app.get("/metrics")
def metrics():
    return metrics_endpoint()
