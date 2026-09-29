import time
import uuid
from fastapi import Request, Response
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import Message, Receive, Scope, Send
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

        # Prometheus metrics. Label by the route template (/candidate/{id}/leads),
        # not the raw path: raw paths made one series per id and exposed live
        # open-tracking tokens under /metrics (audit PS-N11).
        route = request.scope.get("route")
        endpoint = getattr(route, "path", None) or "unmatched"
        REQUEST_COUNT.labels(method=request.method, endpoint=endpoint, status_code=response.status_code).inc()
        REQUEST_DURATION.labels(method=request.method, endpoint=endpoint).observe(duration_ms / 1000)

        response.headers["X-Request-ID"] = request_id
        clear_request_context()
        return response


# UC-Q21 / NEW-06: gzip API responses, but never event streams. The Starlette
# pinned by fastapi 0.115 (0.38.x) gzips text/event-stream too, and GzipFile
# holds small SSE frames in its buffer, so the quiz chat would stop streaming.
class _SSEPassthroughGZipResponder(GZipResponder):
    async def send_with_gzip(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            content_type = Headers(raw=message["headers"]).get("content-type", "")
            if content_type.startswith("text/event-stream"):
                # Same pass-through path Starlette takes for responses that
                # already carry a Content-Encoding.
                self.initial_message = message
                self.content_encoding_set = True
                return
        await super().send_with_gzip(message)


class SSESafeGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and "gzip" in Headers(scope=scope).get("Accept-Encoding", ""):
            responder = _SSEPassthroughGZipResponder(self.app, self.minimum_size, compresslevel=self.compresslevel)
            await responder(scope, receive, send)
            return
        await self.app(scope, receive, send)
