import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from job_outreach_tool.api.routes_candidate import router as candidate_router
from job_outreach_tool.api.routes_discovery import router as discovery_router
from job_outreach_tool.api.routes_scoring import router as scoring_router
from job_outreach_tool.api.routes_enrichment import router as enrichment_router
from job_outreach_tool.api.routes_campaign import router as campaign_router
from job_outreach_tool.api.routes_auth import router as auth_router
from job_outreach_tool.api.routes_gmail import router as gmail_router
from job_outreach_tool.core.logger import get_logger

logger = get_logger("job_outreach_tool.api.main")

app = FastAPI(
    title="InternReach Job Outreach Tool",
    description="Clean Backend Architecture Implementation",
    version="1.0.0"
)

# CORS
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
