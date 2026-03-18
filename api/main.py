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


@app.on_event("startup")
def on_startup():
    """Start the background email sender loop on pod startup."""
    try:
        from services.email_campaign.campaign_worker import start_sender_loop
        start_sender_loop()
        logger.info("Email sender loop started successfully")
    except Exception as e:
        logger.error("Failed to start email sender loop: %s", e)


@app.on_event("shutdown")
def on_shutdown():
    """Signal the campaign worker to stop gracefully on SIGTERM."""
    import time
    try:
        from services.email_campaign.campaign_worker import stop_sender_loop
        logger.info("Shutdown signal received — stopping campaign sender loop...")
        stop_sender_loop()
        time.sleep(5)
        logger.info("Campaign sender loop stopped.")
    except Exception as e:
        logger.error("Failed to stop email sender loop: %s", e)


@app.get("/health")
def health_check():
    return {"status": "online"}


# ── Debug Console ─────────────────────────────────────────────────────────────
import collections
import logging
from fastapi.responses import HTMLResponse, JSONResponse

_log_buffer: collections.deque = collections.deque(maxlen=2000)


class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            _log_buffer.append(self.format(record))
        except Exception:
            pass


_buf_handler = BufferHandler()
_buf_handler.setLevel(logging.DEBUG)
_buf_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
# Attach to root logger so ALL loggers propagate here
logging.getLogger().addHandler(_buf_handler)


@app.get("/debug/logs")
def debug_logs(n: int = 200, filter: str = ""):
    """JSON log endpoint. /job-outreach/debug/logs?n=200&filter=DISCOVERY"""
    logs = list(_log_buffer)
    if filter:
        logs = [l for l in logs if filter.upper() in l.upper()]
    return JSONResponse({"logs": logs[-n:], "total_buffered": len(_log_buffer)})


@app.get("/debug/console", response_class=HTMLResponse)
def debug_console():
    """Live debug console. Open https://api.studojo.com/job-outreach/debug/console"""
    return """<!DOCTYPE html>
<html><head><title>OpportunityApply Debug Console</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0d1117; color: #c9d1d9; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; }
  #header { background: #161b22; border-bottom: 1px solid #30363d; padding: 12px 20px; display: flex; align-items: center; gap: 16px; position: sticky; top: 0; z-index: 10; }
  #header h1 { font-size: 15px; color: #8b5cf6; font-weight: 700; }
  #header .badge { background: #238636; color: #fff; padding: 2px 8px; border-radius: 12px; font-size: 11px; }
  #header .badge.paused { background: #da3633; }
  #controls { display: flex; gap: 8px; margin-left: auto; align-items: center; }
  #controls input { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 4px 10px; border-radius: 6px; font-size: 12px; width: 180px; }
  #controls button { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  #controls button:hover { background: #30363d; }
  #controls button.active { background: #8b5cf6; border-color: #8b5cf6; color: #fff; }
  #logs { padding: 8px 16px; overflow-y: auto; height: calc(100vh - 50px); }
  .log-line { padding: 2px 0; white-space: pre-wrap; word-break: break-all; border-bottom: 1px solid #21262d; }
  .log-line:hover { background: #161b22; }
  .lvl-ERROR, .lvl-CRITICAL { color: #f85149; }
  .lvl-WARNING { color: #d29922; }
  .lvl-INFO { color: #58a6ff; }
  .lvl-DEBUG { color: #6e7681; }
  .highlight { background: #3b2e00; }
  #status { font-size: 11px; color: #6e7681; }
</style></head><body>
<div id="header">
  <h1>OpportunityApply Debug Console</h1>
  <span id="liveBadge" class="badge">LIVE</span>
  <span id="status">0 logs</span>
  <div id="controls">
    <input id="filterInput" placeholder="Filter (e.g. DISCOVERY, APOLLO_INPUT, AI_OUTPUT, TEST_LAUNCH, ORDER)" />
    <button onclick="setFilter('APOLLO')">Apollo</button>
    <button onclick="setFilter('AI_')">AI</button>
    <button onclick="setFilter('EmailGen')">Email</button>
    <button onclick="setFilter('LOOSENING')">Loosening</button>
    <button onclick="setFilter('ORDER')">Orders</button>
    <button onclick="setFilter('PAYMENT')">Payment</button>
    <button onclick="setFilter('TEST_LAUNCH')">Test</button>
    <button onclick="setFilter('ERROR')">Errors</button>
    <button onclick="setFilter('')">All</button>
    <button onclick="clearLogs()">Clear</button>
    <button id="pauseBtn" onclick="togglePause()">Pause</button>
  </div>
</div>
<div id="logs"></div>
<script>
let paused = false, allLogs = [], lastCount = 0;
const $logs = document.getElementById('logs');
const $status = document.getElementById('status');
const $filter = document.getElementById('filterInput');
const $badge = document.getElementById('liveBadge');
const $pause = document.getElementById('pauseBtn');

function levelClass(line) {
  if (line.includes('[ERROR]') || line.includes('[CRITICAL]')) return 'lvl-ERROR';
  if (line.includes('[WARNING]')) return 'lvl-WARNING';
  if (line.includes('[DEBUG]')) return 'lvl-DEBUG';
  return 'lvl-INFO';
}

function renderLogs() {
  const filter = $filter.value.toUpperCase();
  const filtered = filter ? allLogs.filter(l => l.toUpperCase().includes(filter)) : allLogs;
  $logs.innerHTML = filtered.map(l =>
    `<div class="log-line ${levelClass(l)}${filter && l.toUpperCase().includes(filter) ? ' highlight' : ''}">${escHtml(l)}</div>`
  ).join('');
  $status.textContent = `${filtered.length}/${allLogs.length} logs (buffer: ${lastCount})`;
  $logs.scrollTop = $logs.scrollHeight;
}

function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function fetchLogs() {
  try {
    const r = await fetch('/job-outreach/debug/logs?n=2000');
    const d = await r.json();
    allLogs = d.logs || [];
    lastCount = d.total_buffered || 0;
    renderLogs();
  } catch(e) { $status.textContent = 'Fetch error: ' + e.message; }
}

function clearLogs() { allLogs = []; renderLogs(); }
function togglePause() {
  paused = !paused;
  $badge.textContent = paused ? 'PAUSED' : 'LIVE';
  $badge.className = paused ? 'badge paused' : 'badge';
  $pause.textContent = paused ? 'Resume' : 'Pause';
  $pause.className = paused ? 'active' : '';
}

function setFilter(val) { $filter.value = val; renderLogs(); }
$filter.addEventListener('input', renderLogs);
fetchLogs();
setInterval(() => { if (!paused) fetchLogs(); }, 3000);
</script></body></html>"""


@app.get("/metrics")
def metrics():
    return metrics_endpoint()


