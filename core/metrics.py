from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi import Response

# HTTP metrics
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# Business metrics
CAMPAIGNS_RUNNING = Gauge("campaigns_running", "Number of currently running campaigns")
EMAILS_SENT_TOTAL = Counter("emails_sent_total", "Total emails sent", ["status"])
LEAD_DISCOVERY_DURATION = Histogram(
    "lead_discovery_duration_seconds",
    "Lead discovery API call duration",
    buckets=[1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)


def metrics_endpoint():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
