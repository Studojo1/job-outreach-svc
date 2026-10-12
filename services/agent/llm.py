"""The only place the student agent talks to Azure OpenAI.

`stream_chat(messages, tools, on_text, should_stop) -> Turn` is a module-level
function on purpose: it is the test seam (`services/agent/testing.FakeLLM`
replaces it), so no test ever reaches the network.

Chat Completions with `tools`, `stream=True` and usage in the last chunk,
through the sync `AzureOpenAI` client built per call with an explicit timeout
(design 1.2: Sensei's conductor forgot it). Text deltas go to `on_text` as they
arrive; tool call fragments are accumulated per index. Errors map to two
exceptions the runtime turns into the contract's error frames; raw provider
text never leaves this module.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.config import settings
from core.logger import get_logger

logger = get_logger(__name__)

TIMEOUT_SECONDS = 60
MAX_RETRIES = 1
TEMPERATURE = 0.4
STOP_CHECK_SECONDS = 1.0


class ContentFiltered(Exception):
    """Azure's content filter refused the prompt or the answer."""


class ModelUnavailable(Exception):
    """The model could not be reached, timed out, or rate limited us."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"          # raw JSON text from the model

    def parsed_args(self) -> dict:
        """Invalid or non-object JSON becomes {} (design 4.4)."""
        try:
            value = json.loads(self.arguments or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class Turn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    finish_reason: Optional[str] = None
    stopped: bool = False          # should_stop() ended the stream early

    def as_message(self) -> dict:
        """The assistant message to append to the history before tool results."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments or "{}"}}
                for c in self.tool_calls
            ]
        return msg


def deployment() -> str:
    return (settings.AGENT_DEPLOYMENT or "").strip() or settings.AZURE_OPENAI_EMAIL_DEPLOYMENT


def _client():
    from openai import AzureOpenAI

    return AzureOpenAI(
        api_key=settings.AZURE_OPENAI_KEY,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        timeout=TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
    )


def _estimate_tokens(messages: list[dict], text: str, calls: list[ToolCall]) -> dict:
    """Characters divided by 4 when the stream carried no usage."""
    chars_in = sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)
    chars_out = len(text) + sum(len(c.arguments or "") + len(c.name) for c in calls)
    return {"prompt_tokens": chars_in // 4, "completion_tokens": chars_out // 4}


def _is_content_filter(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if code == "content_filter":
        return True
    if isinstance(body, dict) and (body.get("code") == "content_filter"
                                   or (body.get("innererror") or {}).get("code") == "ResponsibleAIPolicyViolation"):
        return True
    return "content_filter" in str(exc) or "ResponsibleAIPolicyViolation" in str(exc)


def _rejected_param(exc: Exception) -> Optional[str]:
    """Name of a request parameter the deployment refused, if that is the error."""
    text = str(exc)
    for name in ("temperature", "stream_options", "max_completion_tokens"):
        if name in text and ("unsupported" in text.lower() or "not supported" in text.lower()
                             or "unrecognized" in text.lower() or "invalid" in text.lower()):
            return name
    return None


def _open_stream(messages: list[dict], tools: Optional[list[dict]]):
    """Create the streamed completion, retrying once per refused parameter
    (gpt-5-mini rejects temperature; an older API version may reject
    stream_options or max_completion_tokens)."""
    import openai

    kwargs: dict[str, Any] = {
        "model": deployment(),
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": TEMPERATURE,
        "max_completion_tokens": settings.AGENT_MAX_OUTPUT_TOKENS,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
        kwargs["parallel_tool_calls"] = True
    client = _client()
    for _attempt in range(4):
        try:
            return client.chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            if _is_content_filter(exc):
                raise ContentFiltered() from exc
            param = _rejected_param(exc)
            if param == "max_completion_tokens" and param in kwargs:
                kwargs["max_tokens"] = kwargs.pop(param)
                continue
            if param and param in kwargs:
                kwargs.pop(param)
                continue
            if "parallel_tool_calls" in kwargs and "parallel_tool_calls" in str(exc):
                kwargs.pop("parallel_tool_calls")
                continue
            logger.warning("[AGENT] model rejected the request: %s", type(exc).__name__)
            raise ModelUnavailable() from exc
        except (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError,
                openai.InternalServerError, openai.AuthenticationError, openai.PermissionDeniedError,
                openai.NotFoundError) as exc:
            logger.warning("[AGENT] model unavailable: %s", type(exc).__name__)
            raise ModelUnavailable() from exc
        except openai.APIError as exc:
            if _is_content_filter(exc):
                raise ContentFiltered() from exc
            logger.warning("[AGENT] model error: %s", type(exc).__name__)
            raise ModelUnavailable() from exc
    raise ModelUnavailable()


def stream_chat(messages: list[dict], tools: Optional[list[dict]],
                on_text: Callable[[str], None],
                should_stop: Callable[[], bool] = lambda: False) -> Turn:
    """One streamed model call. Returns the text, the tool calls and usage."""
    import openai

    stream = _open_stream(messages, tools)
    text_parts: list[str] = []
    calls: dict[int, dict] = {}
    usage: Optional[dict] = None
    finish_reason: Optional[str] = None
    stopped = False
    last_check = time.monotonic()
    try:
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = {"prompt_tokens": int(chunk.usage.prompt_tokens or 0),
                         "completion_tokens": int(chunk.usage.completion_tokens or 0)}
            for choice in chunk.choices or []:
                delta = choice.delta
                if delta is not None:
                    if delta.content:
                        text_parts.append(delta.content)
                        on_text(delta.content)
                    for tc in delta.tool_calls or []:
                        slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function is not None:
                            if tc.function.name:
                                slot["name"] += tc.function.name
                            if tc.function.arguments:
                                slot["arguments"] += tc.function.arguments
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
            now = time.monotonic()
            if now - last_check >= STOP_CHECK_SECONDS:
                last_check = now
                if should_stop():
                    stopped = True
                    break
    except (openai.APITimeoutError, openai.APIConnectionError) as exc:
        logger.warning("[AGENT] model stream broke: %s", type(exc).__name__)
        raise ModelUnavailable() from exc
    except openai.APIError as exc:
        if _is_content_filter(exc):
            raise ContentFiltered() from exc
        raise ModelUnavailable() from exc
    finally:
        try:
            stream.close()
        except Exception:
            pass

    if finish_reason == "content_filter" and not text_parts:
        raise ContentFiltered()
    tool_calls = [] if stopped else [
        ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=c["arguments"] or "{}")
        for i, c in sorted(calls.items()) if c["name"]
    ]
    text = "".join(text_parts)
    return Turn(text=text, tool_calls=tool_calls,
                usage=usage or _estimate_tokens(messages, text, tool_calls),
                finish_reason=finish_reason, stopped=stopped)
