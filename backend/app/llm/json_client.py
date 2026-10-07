"""Adapter: any JSON-completing LLM (services.llm_client.LLMClient) -> ContentLLMClient.

This is the only place where prompts meet a provider. It is provider neutral: the
OpenAI-compatible client of STAGE 2.5 (LLM_API_KEY / LLM_MODEL / LLM_BASE_URL) plugs in
unchanged, and another provider only needs a `complete_json` implementation.
No credentials or provider exceptions pass through here: errors are already `LLMError`s.
"""

from __future__ import annotations

from typing import Any

from ..services.llm_client import LLMClient, LLMResponseError
from .base import ContentLLMClient, EnrichmentRequest, SlideResult
from .schema import batch_schema, slide_schema
from .prompts import (
    build_batch_system_prompt,
    build_batch_user_message,
    build_system_prompt,
    build_user_message,
)


class JsonContentClient(ContentLLMClient):
    def __init__(self, llm: LLMClient, provider: str = "openai-compatible", batch_prompt: bool = True):
        self._llm = llm
        self.provider = provider
        self.model = getattr(llm, "model", "unknown")
        self._batch_prompt = batch_prompt

    @property
    def usage(self) -> dict[str, int]:
        """Token / retry counters when the underlying client keeps them (else empty)."""
        snap = getattr(self._llm, "usage_snapshot", None)
        return snap() if callable(snap) else {}

    def _schema_kwargs(self, schema: dict[str, Any]) -> dict[str, Any]:
        # Only providers that declare structured-output support get a schema.
        return {"schema": schema} if getattr(self._llm, "supports_schema", False) else {}

    def enrich_slide(self, request: EnrichmentRequest) -> dict[str, Any]:
        return self._llm.complete_json(
            task="enrich_slide",
            system=build_system_prompt(request),
            user=build_user_message(request),
            **self._schema_kwargs(slide_schema()),
        )

    def enrich_batch(self, requests: list[EnrichmentRequest]) -> list[SlideResult]:
        if len(requests) == 1 or not self._batch_prompt:
            return super().enrich_batch(requests)
        data = self._llm.complete_json(
            task="enrich_batch",
            system=build_batch_system_prompt(requests),
            user=build_batch_user_message(requests),
            **self._schema_kwargs(batch_schema()),
        )
        items = data.get("slides") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise LLMResponseError("LLM 응답에 slides 목록이 없습니다.")
        results: list[SlideResult] = []
        for item in items:
            if not isinstance(item, dict):
                raise LLMResponseError("LLM 응답의 슬라이드 항목이 JSON 객체가 아닙니다.")
            number = item.get("slide_number")
            # An unusable number is kept as -1: the enricher treats it as a rejected output.
            results.append(SlideResult(number if isinstance(number, int) else -1, output=item))
        return results
