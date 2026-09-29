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
from api.routes_orders import router as orders_router
from api.routes_payment import router as payment_router
from api.routes_admin import router as admin_router
from api.routes_linkedin import router as linkedin_router
from api.routes_leadstest import router as leadstest_router
from api.routes_linkedin_automation import router as linkedin_automation_router
from api.routes_partners import router as partners_router
from api.routes_marketing import router as marketing_router
from api.routes_mesa import router as mesa_router
from api.routes_extension import router as extension_router
from api.routes_account import router as account_router
from api.routes_connections import router as connections_router
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
    allow_origins=[
        "https://studojo.com",
        "https://www.studojo.com",
        "https://api.studojo.com",
        "https://studojo.pro",
        "https://api.studojo.pro",
        "https://admin.studojo.com",
        "https://admin.studojo.pro",
        "http://localhost:3000",
        "http://localhost:3001",
    ],
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
app.include_router(orders_router, prefix="/api/v1")
app.include_router(payment_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(linkedin_router, prefix="/api/v1")
app.include_router(leadstest_router, prefix="/api/v1")
app.include_router(linkedin_automation_router, prefix="/api/v1")
app.include_router(partners_router, prefix="/api/v1")
app.include_router(marketing_router, prefix="/api/v1")
app.include_router(mesa_router, prefix="/api/v1")
app.include_router(extension_router, prefix="/api/v1")
app.include_router(account_router, prefix="/api/v1")
app.include_router(connections_router, prefix="/api/v1")


@app.on_event("startup")
def on_startup():
    """App startup hook."""
    logger.info("Job outreach service started")
    from services.linkedin_outreach.automation_service import start_automation_daemon
    start_automation_daemon()
    from services.payment_reconciler import start_reconciler
    start_reconciler()
    # Encrypt any Gmail tokens still stored in plaintext (idempotent; a no-op
    # once done). Off the startup path so a slow DB cannot delay readiness.
    import threading
    from services.gmail_tokens import run_startup_backfill
    threading.Thread(target=run_startup_backfill, name="gmail-token-backfill", daemon=True).start()


@app.on_event("shutdown")
def on_shutdown():
    """App shutdown hook."""
    logger.info("Job outreach service shutting down")
    from core.analytics import shutdown as ph_shutdown
    ph_shutdown()
    from services.linkedin_outreach.automation_service import stop_automation_daemon
    stop_automation_daemon()
    from services.payment_reconciler import stop_reconciler
    stop_reconciler()


@app.get("/health")
def health_check():
    return {"status": "online"}


# ── Open-tracking pixel ───────────────────────────────────────────────────────
# Public (unauthenticated) — loaded by the recipient's mail client. Records an
# open against the emails_sent row that carries this token, then returns a 1x1
# transparent GIF. Always returns the pixel (never errors to the client).
#
# NOTE: pixel opens are approximate. Apple Mail Privacy Protection and Gmail's
# image proxy pre-fetch images, so we filter opens that arrive within a couple
# of seconds of send (proxy prefetch) and count repeats separately.
import base64 as _b64
from datetime import datetime as _dt, timedelta as _td
from fastapi.responses import Response as _FResponse

# 1x1 transparent GIF
_TRACKING_PIXEL = _b64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)
_PIXEL_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    "Expires": "0",
}
# Ignore a pixel load arriving within this window of send_at — almost certainly
# a mail-provider image prefetch, not a human open.
_OPEN_PREFETCH_GUARD_SECONDS = 3


# Path note: the public ingress rewrites studojo.pro/api/v1/outreach/(.*) to
# /api/v1/$1 on the pod, so this route is registered at /api/v1/track/... and is
# reached publicly at https://<host>/api/v1/outreach/track/{token}.png (see
# _ensure_tracking_token in campaign_worker for the URL that is embedded).
@app.get("/api/v1/track/{token}.png")
@app.get("/api/v1/track/{token}.gif")
def tracking_pixel(token: str):
    """Record an email open and return a 1x1 transparent pixel."""
    from database.session import SessionLocal
    from database.models import EmailSent

    try:
        db = SessionLocal()
        try:
            email = (
                db.query(EmailSent)
                .filter(EmailSent.tracking_token == token)
                .first()
            )
            if email is not None:
                now = _dt.utcnow()
                is_prefetch = (
                    email.sent_at is not None
                    and now - email.sent_at < _td(seconds=_OPEN_PREFETCH_GUARD_SECONDS)
                )
                if not is_prefetch:
                    email.open_count = (email.open_count or 0) + 1
                    email.last_opened_at = now
                    if email.first_opened_at is None:
                        email.first_opened_at = now
                    db.commit()
        finally:
            db.close()
    except Exception as e:  # never let tracking break the pixel response
        logger.warning("tracking_pixel failed for token=%s: %s", token, e)

    return _FResponse(content=_TRACKING_PIXEL, media_type="image/gif", headers=_PIXEL_HEADERS)


@app.get("/metrics")
def metrics():
    return metrics_endpoint()


