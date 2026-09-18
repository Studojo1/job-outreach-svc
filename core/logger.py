import logging
import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from contextvars import ContextVar

# Context variables for request-scoped fields
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")
job_id_var: ContextVar[str] = ContextVar("job_id", default="")

SERVICE_NAME = "job-outreach-svc"

# Ensure logs directory exists
LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def set_request_context(request_id: str = "", user_id: str = "", job_id: str = ""):
    if request_id:
        request_id_var.set(request_id)
    if user_id:
        user_id_var.set(user_id)
    if job_id:
        job_id_var.set(job_id)


def clear_request_context():
    request_id_var.set("")
    user_id_var.set("")
    job_id_var.set("")


class StructuredJsonFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            "user_id": user_id_var.get(),
            "job_id": job_id_var.get(),
        }

        # Include extra fields passed via logger.info("msg", extra={...})
        for key in ("endpoint", "duration_ms", "status", "status_code",
                     "method", "campaign_id", "lead_id", "email_id", "event"):
            if hasattr(record, key):
                log_obj[key] = getattr(record, key)

        if record.exc_info:
            log_obj["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(log_obj)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    # Console handler — structured JSON
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(StructuredJsonFormatter())

    # File handler — structured JSON
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(StructuredJsonFormatter())

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger
