import time
import uuid
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from core.logger import get_logger, set_request_context, clear_request_context
from core.metrics import REQUEST_COUNT, REQUEST_DURATION

logger = get_logger("http")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        user_id = getattr(request.state, "user_id", "") if hasattr(request.state, "user_id") else ""

        set_request_context(request_id=request_id, user_id=str(user_id))

        start = time.perf_counter()
        response: Response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        logger.info(
            f"{request.method} {request.url.path} {response.status_code} {duration_ms}ms",
            extra={
                "endpoint": request.url.path,
                "method": request.method,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )

        # Prometheus metrics
        endpoint = request.url.path
        REQUEST_COUNT.labels(method=request.method, endpoint=endpoint, status_code=response.status_code).inc()
        REQUEST_DURATION.labels(method=request.method, endpoint=endpoint).observe(duration_ms / 1000)

        response.headers["X-Request-ID"] = request_id
        clear_request_context()
        return response
