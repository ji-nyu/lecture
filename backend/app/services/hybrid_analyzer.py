"""HybridAnalyzer: heuristic -> LLMExtractor -> EvidenceValidator -> merge.

    mode "hybrid"  heuristic result + validated LLM additions (nothing heuristic is dropped)
    mode "llm"     heuristic structure + LLM-confirmed concepts (heuristic-only concepts dropped)

The heuristic analyzer ALWAYS runs first. If the LLM path cannot deliver (no
key, timeout, provider error, unusable response) the heuristic result is
returned unchanged with a warning and `analyzer_info.fallback = True`; the
analysis request itself never fails because of the LLM.
"""

from __future__ import annotations

import logging
from collections import Counter

from ..models.source import AnalyzerInfo, SourceAnalysis, SourceMaterial
from .analysis_merge import apply_topic_labels, merge_analysis
from .analyzer_strategy import AnalyzerStrategy
from .evidence_validator import EvidenceValidator
from .lecture_analyzer import HeuristicAnalyzer
from .llm_client import LLMClient, LLMEmptyResult, LLMError
from .llm_extractor import LLMExtractor

logger = logging.getLogger("ailecturegen")

_FALLBACK_MESSAGES = {
    "not_configured": "LLM 설정(LLM_API_KEY, LLM_MODEL)이 없어 규칙 기반 분석 결과를 사용했습니다.",
    "timeout": "LLM 응답 시간이 초과되어 규칙 기반 분석 결과를 사용했습니다.",
    "error": "LLM 호출에 실패하여 규칙 기반 분석 결과를 사용했습니다.",
    "invalid_response": "LLM 응답을 사용할 수 없어 규칙 기반 분석 결과를 사용했습니다.",
    "empty_result": "LLM이 원문에서 검증 가능한 개념을 찾지 못해 규칙 기반 분석 결과를 사용했습니다.",
}


class HybridAnalyzer(AnalyzerStrategy):
    def __init__(
        self,
        client: LLMClient | None,
        *,
        mode: str = "hybrid",
        heuristic: AnalyzerStrategy | None = None,
    ):
        if mode not in ("hybrid", "llm"):
            raise ValueError(f"unsupported mode: {mode}")
        self.client = client
        self.mode = mode
        self.heuristic = heuristic or HeuristicAnalyzer()

    def analyze(self, material: SourceMaterial) -> SourceAnalysis:
        base = self.heuristic.analyze(material)
        if self.client is None:
            return self._fallback(base, "not_configured")
        try:
            return self._run(material, base)
        except LLMError as exc:
            return self._fallback(base, exc.reason)
        except Exception as exc:  # the LLM path must never fail the request
            logger.warning("LLM analysis failed unexpectedly: %s", type(exc).__name__)
            return self._fallback(base, "error")

    # ------------------------------------------------------------------
    def _run(self, material: SourceMaterial, base: SourceAnalysis) -> SourceAnalysis:
        validator = EvidenceValidator(material)
        extractor = LLMExtractor(self.client)
        ex = extractor.extract(material, validator)
        if self.mode == "llm" and not ex.concepts:
            raise LLMEmptyResult("no verifiable concepts")
        merged = merge_analysis(
            base, ex, validator, keep_heuristic_only=(self.mode == "hybrid")
        )
        calls = ex.calls
        discarded: Counter = ex.discarded

        # Topic titles: a failure here only costs the nicer titles.
        try:
            topics = [
                {
                    "index": i,
                    "sections": [s.title for s in merged.sections if s.id in t.section_ids],
                    "concepts": t.key_concepts,
                }
                for i, t in enumerate(merged.main_topics)
            ]
            labels = extractor.label_topics(topics, {c.name for c in merged.concepts}, discarded)
            calls += 1
            old_titles = [t.title for t in merged.main_topics]
            apply_topic_labels(merged, labels)
            for old, t in zip(old_titles, merged.main_topics):
                if old != t.title:
                    merged.summary = merged.summary.replace(old, t.title)
        except LLMError as exc:
            discarded["topic.call_failed"] += 1
            merged.warnings.append(f"주제 제목을 LLM으로 다듬지 못했습니다({exc.reason}). 규칙 기반 제목을 유지합니다.")

        merged.analyzer = "hybrid-v1" if self.mode == "hybrid" else "llm-v1"
        merged.analyzer_info = AnalyzerInfo(
            mode_requested=self.mode,
            mode_used=self.mode,
            fallback=False,
            llm_model=getattr(self.client, "model", None),
            llm_calls=calls,
            discarded=dict(discarded),
        )
        return merged

    def _fallback(self, base: SourceAnalysis, reason: str) -> SourceAnalysis:
        info = base.analyzer_info or AnalyzerInfo()
        info.mode_requested = self.mode
        info.mode_used = "heuristic"
        info.fallback = True
        info.fallback_reason = reason
        base.analyzer_info = info
        base.warnings.append(_FALLBACK_MESSAGES.get(reason, _FALLBACK_MESSAGES["error"]))
        return base
