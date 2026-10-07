"""LLMClient abstraction.

The analysis code depends only on `LLMClient.complete_json`. Provider specific
code lives in `OpenAICompatibleClient` (works with OpenAI and any server that
speaks the OpenAI chat-completions protocol via LLM_BASE_URL). Swapping the
provider means adding another `LLMClient` subclass; no analysis code changes.

The API key is read from the environment (LLM_API_KEY), never from code.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger("ailecturegen")


class LLMError(Exception):
    """Base class. `reason` is a short machine-readable code shown in analyzer_info."""

    reason = "error"


class LLMUnavailable(LLMError):
    reason = "not_configured"


class LLMConnectionError(LLMUnavailable):
    """The provider could not be reached / answered 5xx (transient; the provider IS configured)."""

    reason = "error"


class LLMTimeout(LLMError):
    reason = "timeout"


class LLMRateLimit(LLMError):
    reason = "rate_limit"


class LLMResponseError(LLMError):
    reason = "invalid_response"


class LLMEmptyResult(LLMError):
    """The call worked but nothing in it survived source validation."""

    reason = "empty_result"


class LLMClient(ABC):
    model: str = "unknown"
    # True => `complete_json` also accepts `schema=` (a JSON schema for structured output)
    supports_schema: bool = False

    @abstractmethod
    def complete_json(
        self, *, task: str, system: str, user: str, timeout: float | None = None
    ) -> dict[str, Any]:
        """Return the model's answer as a JSON object.

        Implementations must use temperature 0 / JSON output where the provider
        supports it, and raise LLMTimeout / LLMResponseError / LLMError (never
        leak provider exceptions or credentials).
        """


def parse_json_object(text: str) -> dict[str, Any]:
    """Tolerate ```json fences around the answer; require a JSON object."""
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.DOTALL)
    if m:
        t = m.group(1)
    try:
        data = json.loads(t)
    except json.JSONDecodeError as exc:
        raise LLMResponseError("LLM 응답이 JSON이 아닙니다.") from exc
    if not isinstance(data, dict):
        raise LLMResponseError("LLM 응답이 JSON 객체가 아닙니다.")
    return data


class _Rejected(Exception):
    """HTTP 400 from the provider: which optional parameter (if any) it did not accept."""

    def __init__(self, param: str | None):
        super().__init__("rejected")
        self.param = param


def _rejected_parameter(exc: Exception) -> str | None:
    """Classify a 400 by its `param` field / message (the text itself is never kept or logged)."""
    param = getattr(exc, "param", None)
    text = f"{param or ''} {getattr(exc, 'code', '') or ''} {exc}".lower()
    if "temperature" in text:
        return "temperature"
    if "response_format" in text or "json_schema" in text or "schema" in text:
        return "response_format"
    return None


class OpenAICompatibleClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        http_client: Any = None,  # injectable (tests use an httpx MockTransport)
        temperature: float | None = 0,  # None => the parameter is not sent at all
        retries: int = 0,  # extra attempts for transient errors (timeout / rate limit / connection / 5xx)
        sdk_max_retries: int = 1,
        backoff_seconds: float = 0.5,
        sleep: Any = time.sleep,  # injectable (tests do not wait)
    ):
        import openai  # optional dependency, imported lazily

        self._openai = openai
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.retries = max(0, retries)
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._lock = threading.Lock()
        # Parameters a provider rejected are dropped for the rest of the session: nothing is guessed.
        self._send_temperature = temperature is not None
        self._schema_ok = True
        self.usage: dict[str, int] = {"calls": 0, "retries": 0, "input_tokens": 0, "output_tokens": 0}
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
            max_retries=sdk_max_retries,
            http_client=http_client,
        )

    supports_schema = True

    # ------------------------------------------------------------------
    def usage_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.usage)

    def _count(self, **kw: int) -> None:
        with self._lock:
            for k, v in kw.items():
                self.usage[k] = self.usage.get(k, 0) + v

    def _response_format(self, schema: dict[str, Any] | None) -> dict[str, Any]:
        if schema is not None and self._schema_ok:
            return {
                "type": "json_schema",
                "json_schema": {"name": "slide_enrichment", "strict": True, "schema": schema},
            }
        return {"type": "json_object"}

    def _once(self, system: str, user: str, timeout: float, schema: dict[str, Any] | None) -> dict[str, Any]:
        """One HTTP call. Provider errors become LLMErrors (no message text leaves this class)."""
        o = self._openai
        params: dict[str, Any] = {
            "model": self.model,
            "response_format": self._response_format(schema),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "timeout": timeout,
        }
        if self._send_temperature and self.temperature is not None:
            params["temperature"] = self.temperature
        try:
            resp = self._client.chat.completions.create(**params)
        except o.APITimeoutError as exc:
            raise LLMTimeout("LLM 응답 시간이 초과되었습니다.") from exc
        except o.RateLimitError as exc:
            if getattr(exc, "code", None) == "insufficient_quota":  # not transient: retrying cannot help
                raise LLMError("LLM 사용 한도(크레딧)를 초과했습니다.") from exc
            raise LLMRateLimit("LLM 호출 한도에 도달했습니다.") from exc
        except o.APIConnectionError as exc:
            raise LLMConnectionError("LLM 서버에 연결할 수 없습니다.") from exc
        except o.InternalServerError as exc:
            raise LLMConnectionError("LLM 서버가 일시적으로 응답하지 않습니다.") from exc
        except o.BadRequestError as exc:
            raise _Rejected(_rejected_parameter(exc)) from exc
        except o.OpenAIError as exc:
            # Only the exception class is logged; the message may echo request details.
            logger.warning("LLM call failed: %s", type(exc).__name__)
            raise LLMError("LLM 호출에 실패했습니다.") from exc
        usage = getattr(resp, "usage", None)
        self._count(
            calls=1,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )
        try:
            message = resp.choices[0].message
            content = message.content or ""
        except (AttributeError, IndexError) as exc:
            raise LLMResponseError("LLM 응답 형식이 올바르지 않습니다.") from exc
        if not content and getattr(message, "refusal", None):
            raise LLMResponseError("LLM이 요청에 응답하지 않았습니다.")
        return parse_json_object(content)

    def complete_json(
        self, *, task: str, system: str, user: str, timeout: float | None = None,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """JSON object answer. With `schema` the provider's structured output is used; a provider
        that rejects it (or `temperature`) is asked again without - once per parameter."""
        attempt = 0
        rejections = 0
        while True:
            try:
                return self._once(system, user, timeout or self.timeout, schema)
            except _Rejected as rej:
                rejections += 1
                if rejections <= 2 and rej.param == "temperature":
                    self._send_temperature = False
                elif rejections <= 2 and rej.param == "response_format" and schema is not None:
                    self._schema_ok = False
                else:  # a real bad request: our request is wrong, asking again cannot help
                    logger.warning("LLM rejected the request (%s)", task)
                    raise LLMError("LLM이 요청을 받아들이지 않았습니다.") from None
                logger.info("LLM does not accept %r; continuing without it", rej.param)
            except (LLMTimeout, LLMRateLimit, LLMUnavailable, LLMResponseError) as exc:
                if attempt >= self.retries:
                    raise
                attempt += 1
                self._count(retries=1)
                logger.info("LLM call retry %d/%d after %s", attempt, self.retries, type(exc).__name__)
                self._sleep(min(self.backoff_seconds * (2 ** (attempt - 1)), 8.0))


def create_llm_client(settings) -> LLMClient | None:
    """Build the configured client, or None when no key/model is configured
    (the analyzers then fall back to the heuristic result)."""
    if not settings.llm_api_key or not settings.llm_model:
        return None
    try:
        return OpenAICompatibleClient(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
        )
    except ImportError:
        logger.warning("openai package is not installed; LLM analysis is disabled.")
        return None
