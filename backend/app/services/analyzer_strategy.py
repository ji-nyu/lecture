"""AnalyzerStrategy: the single interface every analyzer implements.

    HeuristicAnalyzer  (lecture_analyzer.py)  rule based, offline, deterministic
    LLMExtractor       (llm_extractor.py)     LLM-derived semantics, source-verified
    HybridAnalyzer     (hybrid_analyzer.py)   heuristic + LLM merged (with fallback)

Callers (the API) only depend on this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models.source import SourceAnalysis, SourceMaterial


class AnalyzerStrategy(ABC):
    @abstractmethod
    def analyze(self, material: SourceMaterial) -> SourceAnalysis:
        """SourceMaterial -> SourceAnalysis. Must not raise for LLM/network problems."""
