"""Choose the ContentLLMClient from settings (ENRICHMENT_PROVIDER).

The only provider-specific construction lives here; the enricher only sees `ContentLLMClient`.
"""

from __future__ import annotations

import logging

from ..services.llm_client import OpenAICompatibleClient
from .base import ContentLLMClient
from .json_client import JsonContentClient
from .mock import MockLLMClient

logger = logging.getLogger("ailecturegen")


def build_openai_content_client(
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    timeout: float = 90.0,
    temperature: float | None = 0.1,
    retries: int = 2,
) -> ContentLLMClient | None:
    """An OpenAI-compatible content writer (structured JSON output, bounded retries).
    None when the `openai` package is missing."""
    try:
        low = OpenAICompatibleClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            temperature=temperature,
            retries=retries,
            sdk_max_retries=0,  # retries are counted and logged by our own loop
        )
    except ImportError:
        logger.warning("openai package is not installed; slide enrichment is disabled.")
        return None
    return JsonContentClient(low, provider="openai")


def create_content_client(settings) -> ContentLLMClient | None:
    """None => no LLM available: the enricher keeps the rule-based content of every slide
    (reason `not_configured`) instead of failing the project."""
    provider = getattr(settings, "enrichment_provider", "none")
    if provider == "mock":
        return MockLLMClient()
    if provider == "openai":
        model = getattr(settings, "enrichment_model", None) or settings.llm_model
        if not settings.llm_api_key or not model:
            return None
        return build_openai_content_client(
            api_key=settings.llm_api_key,
            model=model,
            base_url=settings.llm_base_url,
            timeout=getattr(settings, "enrichment_timeout_seconds", 90.0),
            temperature=getattr(settings, "enrichment_temperature", 0.1),
            retries=getattr(settings, "enrichment_max_retries", 2),
        )
    return None
