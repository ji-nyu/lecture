"""Pick the AnalyzerStrategy for the configured ANALYZER_MODE."""

from __future__ import annotations

from ..config import Settings
from .analyzer_strategy import AnalyzerStrategy
from .hybrid_analyzer import HybridAnalyzer
from .lecture_analyzer import HeuristicAnalyzer
from .llm_client import LLMClient, create_llm_client


def build_analyzer(settings: Settings, llm_client: LLMClient | None = None) -> AnalyzerStrategy:
    """heuristic (default) -> HeuristicAnalyzer. hybrid / llm -> HybridAnalyzer with
    the injected client (tests) or the one configured through LLM_* settings.
    With no client the HybridAnalyzer falls back to the heuristic result."""
    mode = settings.analyzer_mode
    if mode == "heuristic":
        return HeuristicAnalyzer()
    return HybridAnalyzer(llm_client or create_llm_client(settings), mode=mode)
