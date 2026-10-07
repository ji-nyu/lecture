"""SlideContentEnricher (STAGE 6A): the LLM writes content, the rule engine keeps the structure.

    SlideSpecification (+ plan, profile, analysis)
        -> one EnrichmentRequest per slide (only the relevant source context)
        -> ContentLLMClient (batches of `batch_size`, optional worker threads)
        -> schema check -> structure check -> GroundingValidator
        -> EnrichedSlideSpecification

Guarantees
  * The number, order, numbers, sections, types and times of the slides come from the
    input SlideSpecification only. The LLM's echo of them is compared, never adopted.
  * Anything wrong with ONE slide (timeout, invalid JSON, structure change, nothing
    left after validation, ...) marks that slide `enrichment_failed` and keeps its
    original content. The lecture is never failed by the LLM.
  * A batch answer that contains a slide that was not requested (e.g. a 23rd slide),
    or the same slide twice, is rejected as a whole: its slides use the original content.
  * Results are matched by slide_number and returned in slide order, however the
    batches were scheduled or completed.
  * Identical input => identical cache keys => no second LLM call.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..errors import EnrichmentError
from ..llm.base import (
    KNOWN_OUTPUT_KEYS,
    STRUCTURE_KEYS,
    ContentLLMClient,
    EnrichmentRequest,
    LLMSlideOutput,
)
from ..llm.prompts import PROMPT_VERSION
from ..models.enriched_slide_spec import (
    CONTENT_FIELDS,
    EnrichedSlide,
    EnrichedSlideSpecification,
    EnrichmentMetadata,
    EnrichmentStats,
    EnrichmentValidation,
    EnrichmentVersion,
    FieldProvenance,
    GroundingInfo,
    OriginalContent,
    Rejection,
    RunStatus,
    SlideEnrichmentStatus,
)
from ..models.lecture_plan import LecturePlan
from ..models.lecture_profile import LectureProfile
from ..models.slide_spec import Slide, SlideSpecification
from ..models.source import SourceAnalysis
from ..storage.project_store import utcnow
from .enrichment_cache import EnrichmentCache, InMemoryCache
from .grounding_validator import GroundingValidator
from .llm_client import (
    LLMError,
    LLMRateLimit,
    LLMResponseError,
    LLMTimeout,
    LLMUnavailable,
)
from .source_context import build_requests
from .text_utils import stable_hash

logger = logging.getLogger("ailecturegen")

ENRICHER_VERSION = "6a-1"

FAILURE_MESSAGES = {
    "not_configured": "콘텐츠 보강용 LLM이 설정되지 않아 기존 슬라이드 내용을 사용합니다.",
    "provider_unavailable": "LLM을 사용할 수 없어 기존 슬라이드 내용을 사용합니다.",
    "timeout": "LLM 응답 시간이 초과되어 기존 슬라이드 내용을 사용합니다.",
    "rate_limit": "LLM 호출 한도에 도달해 기존 슬라이드 내용을 사용합니다.",
    "api_error": "LLM 호출에 실패해 기존 슬라이드 내용을 사용합니다.",
    "invalid_json": "LLM 응답이 올바른 JSON이 아니어서 기존 슬라이드 내용을 사용합니다.",
    "invalid_schema": "LLM 응답 형식이 올바르지 않아 기존 슬라이드 내용을 사용합니다.",
    "missing_in_response": "LLM 응답에 이 슬라이드가 없어 기존 슬라이드 내용을 사용합니다.",
    "structure_mutation": "LLM이 슬라이드 구조를 바꾸려 해 결과를 거부하고 기존 슬라이드 내용을 사용합니다.",
    "slide_number_mismatch": "LLM 응답의 슬라이드 번호가 요청과 달라 결과를 거부하고 기존 슬라이드 내용을 사용합니다.",
    "required_field_missing": "핵심 메시지를 검증 기준에 맞게 만들지 못해 기존 슬라이드 내용을 사용합니다.",
    "validation_failed": "검증에 실패해 기존 슬라이드 내용을 사용합니다.",
    "unexpected": "콘텐츠 보강 중 예상하지 못한 문제가 있어 기존 슬라이드 내용을 사용합니다.",
}


def failure_reason_of(exc: BaseException) -> str:
    if isinstance(exc, LLMTimeout):
        return "timeout"
    if isinstance(exc, LLMRateLimit):
        return "rate_limit"
    if isinstance(exc, LLMUnavailable):
        return "provider_unavailable"
    if isinstance(exc, LLMResponseError):
        return "invalid_json"
    if isinstance(exc, LLMError):
        return "api_error"
    return "unexpected"


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------
def hash_profile(profile: LectureProfile) -> str:
    d = profile.model_dump(mode="json")
    for k in (
        "id", "project_id", "created_at", "updated_at", "warnings",
        "inferred_fields", "preset_fields", "video_intro", "video_intro_file",
    ):
        d.pop(k, None)
    return stable_hash(d)


def hash_spec(spec: SlideSpecification) -> str:
    """The slide specification without identity/time-stamp fields (equal structure => equal hash)."""
    d = spec.model_dump(mode="json")
    for k in ("lecture_id", "generated_at"):
        d.pop(k, None)
    return stable_hash(d)


def corpus_from_analysis(a: SourceAnalysis | None) -> str:
    """Stand-in for the raw source text when only the analysis is available."""
    if a is None:
        return ""
    parts: list[str] = [a.title, a.summary]
    for c in a.concepts:
        parts += [c.name, *c.aliases, c.description]
    parts += [f"{d.term} {d.definition}" for d in a.definitions]
    parts += [e.text for e in a.examples]
    parts += [p.text for p in a.important_points]
    parts += [n.text for n in a.scope_notes]
    parts += [cb.code for cb in a.code_examples]
    parts += [f.expression for f in a.formulas]
    parts += [f"{s.title} {s.summary}" for s in a.sections]
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
@dataclass
class _Outcome:
    slide: EnrichedSlide | None = None
    reason: str | None = None  # set => failed
    message: str | None = None
    raw: dict[str, Any] | None = None  # answer to cache (only when the slide succeeded)
    from_cache: bool = False
    reused: bool = False


@dataclass
class _Run:
    llm_calls: int = 0
    cache_hits: int = 0
    batches: int = 0
    rejections: list[Rejection] = field(default_factory=list)


class SlideContentEnricher:
    def __init__(
        self,
        client: ContentLLMClient | None,
        *,
        batch_size: int = 5,
        workers: int = 1,
        cache: EnrichmentCache | None = None,
        provider_name: str = "none",
        prompt_version: str = PROMPT_VERSION,
    ):
        self.client = client
        self.batch_size = max(1, batch_size)
        self.workers = max(1, workers)
        self.cache = cache if cache is not None else InMemoryCache()
        self.provider_name = provider_name
        self.prompt_version = prompt_version

    # ------------------------------------------------------------------
    @property
    def provider(self) -> str:
        return self.client.provider if self.client else self.provider_name

    @property
    def model(self) -> str:
        return self.client.model if self.client else "none"

    def cache_key(
        self, req: EnrichmentRequest, source_hash: str, profile_hash: str, spec_hash: str
    ) -> str:
        return stable_hash(
            {
                "source": source_hash,
                "profile": profile_hash,
                "spec": spec_hash,
                "slide": req.slide.slide_number,
                "request": stable_hash(req.model_dump(mode="json")),
                "prompt": self.prompt_version,
                "provider": self.provider,
                "model": self.model,
                "enricher": ENRICHER_VERSION,
            }
        )

    # ------------------------------------------------------------------
    def enrich(
        self,
        spec: SlideSpecification,
        plan: LecturePlan,
        profile: LectureProfile,
        analysis: SourceAnalysis | None,
        *,
        source_text: str | None = None,
        source_hash: str | None = None,
        force: bool = False,
        previous: EnrichedSlideSpecification | None = None,
    ) -> EnrichedSlideSpecification:
        """`force` ignores the cache; `previous` (retry) keeps its enriched slides and only
        asks the LLM again for the slides that failed."""
        if spec.lecture_id != plan.id:
            raise EnrichmentError("슬라이드 구성이 현재 강의 계획과 맞지 않습니다. 슬라이드를 다시 만들어 주세요.")
        try:
            requests = build_requests(spec, plan, profile, analysis, self.prompt_version)
        except ValueError as exc:
            raise EnrichmentError("슬라이드 콘텐츠 요청을 만들 수 없습니다.") from exc

        text = source_text or corpus_from_analysis(analysis)
        source_hash = source_hash or stable_hash(text)
        profile_hash = hash_profile(profile)
        spec_hash = hash_spec(spec)
        validator = GroundingValidator(text)
        run = _Run()
        keys = {r.slide.slide_number: self.cache_key(r, source_hash, profile_hash, spec_hash) for r in requests}
        by_number = {s.slide_number: s for s in spec.slides}

        outcomes: dict[int, _Outcome] = {}
        pending: list[EnrichmentRequest] = []
        reusable = self._reusable(previous, spec_hash, profile_hash, source_hash)
        for req in requests:
            n = req.slide.slide_number
            if reusable is not None and reusable.get(n) is not None:
                outcomes[n] = _Outcome(slide=reusable[n], reused=True)
                run.cache_hits += 1
                continue
            raw = None if force else self.cache.get(keys[n])
            if raw is not None:
                out = self._process(req, by_number[n], raw, validator, profile)
                if out.reason is None:
                    out.from_cache = True
                    out.slide.from_cache = True
                    out.raw = None  # already stored
                    outcomes[n] = out
                    run.cache_hits += 1
                    continue
            pending.append(req)

        self._call_llm(pending, by_number, validator, profile, outcomes, run)

        for n, out in outcomes.items():
            if out.raw is not None:
                self.cache.set(keys[n], out.raw)
        self.cache.retain({k for n, k in keys.items() if outcomes[n].reason is None})

        return self._assemble(
            spec, profile, outcomes, run, requests,
            source_hash=source_hash, profile_hash=profile_hash, spec_hash=spec_hash,
        )

    # ------------------------------------------------------------------
    def _reusable(self, previous, spec_hash, profile_hash, source_hash) -> dict[int, EnrichedSlide] | None:
        if previous is None:
            return None
        v = previous.version
        same = (
            v.slide_spec_hash == spec_hash and v.lecture_profile_hash == profile_hash
            and v.source_hash == source_hash and v.prompt_version == self.prompt_version
            and v.model_provider == self.provider and v.model_name == self.model
            and v.enricher_version == ENRICHER_VERSION
        )
        if not same:
            return None
        return {s.slide_number: s for s in previous.slides if s.status == SlideEnrichmentStatus.enriched}

    # ------------------------------------------------------------------
    def _call_llm(self, pending, by_number, validator, profile, outcomes, run: _Run) -> None:
        if not pending:
            return
        if self.client is None:
            for req in pending:
                outcomes[req.slide.slide_number] = self._failed(by_number[req.slide.slide_number], profile, "not_configured")
            return
        chunks = [pending[i : i + self.batch_size] for i in range(0, len(pending), self.batch_size)]
        run.batches += len(chunks)
        run.llm_calls += len(pending)

        def work(chunk: list[EnrichmentRequest]):
            return self._run_chunk(chunk, by_number, validator, profile)

        if self.workers > 1 and len(chunks) > 1:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                results = list(pool.map(work, chunks))
        else:
            results = [work(c) for c in chunks]
        for res, rejections in results:
            outcomes.update(res)
            run.rejections.extend(rejections)

    def _run_chunk(self, chunk, by_number, validator, profile):
        """One batch -> {slide_number: outcome}. Never raises."""
        numbers = [r.slide.slide_number for r in chunk]
        rejections: list[Rejection] = []

        def fail_all(reason: str, rejected: bool = False, extra: str = ""):
            if rejected:
                rejections.append(
                    Rejection(slide_numbers=numbers, reason=reason, message=FAILURE_MESSAGES[reason] + extra)
                )
            return {n: self._failed(by_number[n], profile, reason) for n in numbers}, rejections

        try:
            results = self.client.enrich_batch(chunk)
        except LLMError as exc:
            return fail_all(failure_reason_of(exc))
        except Exception as exc:  # a provider bug must not fail the lecture
            logger.warning("Content LLM raised %s", type(exc).__name__)
            return fail_all("unexpected")

        if not isinstance(results, list):
            return fail_all("invalid_schema")
        requested = set(numbers)
        seen: dict[int, Any] = {}
        extras: list[int] = []
        duplicates: list[int] = []
        for res in results:
            n = getattr(res, "slide_number", None)
            if n not in requested:
                extras.append(n)
            elif n in seen:
                duplicates.append(n)
            else:
                seen[n] = res
        if extras or duplicates:  # slides were added / repeated: the whole answer is untrusted
            detail = f" (요청하지 않은 슬라이드: {sorted(map(str, extras))}, 중복: {sorted(duplicates)})"
            return fail_all("structure_mutation", rejected=True, extra=detail)

        outcomes: dict[int, _Outcome] = {}
        for req in chunk:
            n = req.slide.slide_number
            res = seen.get(n)
            if res is None:
                outcomes[n] = self._failed(by_number[n], profile, "missing_in_response")
            elif res.error is not None:
                outcomes[n] = self._failed(by_number[n], profile, failure_reason_of(res.error))
            else:
                outcomes[n] = self._process(req, by_number[n], res.output, validator, profile)
                if outcomes[n].reason in ("structure_mutation", "slide_number_mismatch"):
                    rejections.append(
                        Rejection(slide_numbers=[n], reason=outcomes[n].reason, message=outcomes[n].message or "")
                    )
        return outcomes, rejections

    # ------------------------------------------------------------------
    def _process(self, req, slide: Slide, raw: Any, validator: GroundingValidator, profile) -> _Outcome:
        """Raw LLM answer -> checked EnrichedSlide (or a failed outcome)."""
        if not isinstance(raw, dict):
            return self._failed(slide, profile, "invalid_json")
        if any(k in raw for k in STRUCTURE_KEYS):
            return self._failed(slide, profile, "structure_mutation")
        try:
            out = LLMSlideOutput.model_validate(raw)
        except ValidationError:
            return self._failed(slide, profile, "invalid_schema")
        if out.slide_number != slide.slide_number:
            return self._failed(slide, profile, "slide_number_mismatch")
        if (
            (out.section_id is not None and out.section_id != slide.section_id)
            or (out.slide_type is not None and out.slide_type != slide.slide_type.value)
            or (out.estimated_explanation_time is not None
                and out.estimated_explanation_time != slide.estimated_explanation_time)
        ):
            return self._failed(slide, profile, "structure_mutation")
        try:
            v = validator.validate(req, out)
        except Exception as exc:  # a validator bug must not fail the lecture either
            logger.warning("GroundingValidator raised %s", type(exc).__name__)
            return self._failed(slide, profile, "unexpected")
        if v.failure_reason:
            return self._failed(slide, profile, v.failure_reason, warnings=v.warnings)
        ignored = sorted(set(raw) - KNOWN_OUTPUT_KEYS)
        warnings = list(v.warnings)
        if ignored:
            warnings.append(f"알 수 없는 응답 항목을 무시했습니다: {', '.join(ignored[:5])}")
        es = EnrichedSlide(
            **_structure(slide),
            original=_original(slide),
            enriched=v.content,
            provenance=v.provenance,
            grounding=GroundingInfo(source_references=v.references, grounded=v.grounded, warnings=warnings),
            metadata=_metadata(profile),
            status=SlideEnrichmentStatus.enriched,
        )
        return _Outcome(slide=es, raw=raw)

    def _failed(self, slide: Slide, profile, reason: str, warnings: list[str] | None = None) -> _Outcome:
        message = FAILURE_MESSAGES.get(reason, FAILURE_MESSAGES["unexpected"])
        es = EnrichedSlide(
            **_structure(slide),
            original=_original(slide),
            enriched=None,
            provenance={},
            grounding=GroundingInfo(
                source_references=[slide.source_reference] if slide.source_reference else [],
                grounded=False,
                warnings=list(warnings or []),
            ),
            metadata=_metadata(profile),
            status=SlideEnrichmentStatus.enrichment_failed,
            failure_reason=reason,
            failure_message=message,
        )
        return _Outcome(slide=es, reason=reason, message=message)

    # ------------------------------------------------------------------
    def _assemble(self, spec, profile, outcomes, run: _Run, requests, *, source_hash, profile_hash, spec_hash):
        slides: list[EnrichedSlide] = []
        for orig in spec.slides:  # spec order == slide_number order
            out = outcomes[orig.slide_number]
            es = out.slide
            problems = check_slide(orig, es, profile)
            if problems:  # defence in depth: never let a broken slide into the result
                logger.warning("Enriched slide %d rejected: %s", orig.slide_number, "; ".join(problems))
                es = self._failed(orig, profile, "validation_failed", warnings=problems).slide
                run.rejections.append(
                    Rejection(slide_numbers=[orig.slide_number], reason="validation_failed", message="; ".join(problems))
                )
            slides.append(es)

        failed = [s for s in slides if s.status == SlideEnrichmentStatus.enrichment_failed]
        ok = len(slides) - len(failed)
        status = RunStatus.enriched if not failed else (RunStatus.enrichment_failed if ok == 0 else RunStatus.enrichment_partial)
        by_reason: dict[str, int] = {}
        for s in failed:
            by_reason[s.failure_reason or "unexpected"] = by_reason.get(s.failure_reason or "unexpected", 0) + 1
        by_prov: dict[str, int] = {}
        for s in slides:
            for p in s.provenance.values():
                by_prov[p.value] = by_prov.get(p.value, 0) + 1
        warnings = []
        if failed:
            reasons = ", ".join(f"{r} {c}장" for r, c in sorted(by_reason.items()))
            warnings.append(f"{len(failed)}장은 콘텐츠 보강에 실패해 기존 슬라이드 내용을 사용합니다 ({reasons}).")
        meta = _metadata(profile)
        result = EnrichedSlideSpecification(
            lecture_id=spec.lecture_id,
            project_id=spec.project_id,
            title=spec.title,
            duration_minutes=spec.duration_minutes,
            slide_count=len(slides),
            status=status,
            slides=slides,
            metadata=meta,
            validation=EnrichmentValidation(),
            stats=EnrichmentStats(
                slide_count=len(slides),
                enriched_count=ok,
                failed_count=len(failed),
                adjusted_count=sum(1 for s in slides if s.status == SlideEnrichmentStatus.enriched and s.grounding.warnings),
                llm_calls=run.llm_calls,
                cache_hits=run.cache_hits,
                batch_size=self.batch_size,
                batch_count=run.batches,
                failures_by_reason=by_reason,
                fields_by_provenance=by_prov,
            ),
            version=EnrichmentVersion(
                enricher_version=ENRICHER_VERSION,
                prompt_version=self.prompt_version,
                model_provider=self.provider,
                model_name=self.model,
                generation_timestamp=utcnow(),
                source_hash=source_hash,
                lecture_profile_hash=profile_hash,
                slide_spec_hash=spec_hash,
            ),
            warnings=warnings,
        )
        result.validation = validate_enrichment(spec, result, profile)
        result.validation.rejections = run.rejections
        if not result.validation.passed:
            raise EnrichmentError(
                "콘텐츠 보강 결과가 검증을 통과하지 못했습니다. 기존 슬라이드 구성은 그대로 유지됩니다.",
                details=result.validation.errors,
            )
        return result


# ---------------------------------------------------------------------------
# structure helpers + validation
# ---------------------------------------------------------------------------
def _structure(slide: Slide) -> dict[str, Any]:
    return dict(
        slide_number=slide.slide_number,
        section_id=slide.section_id,
        title=slide.title,
        slide_type=slide.slide_type,
        learning_purpose=slide.learning_purpose,
        estimated_explanation_time=slide.estimated_explanation_time,
    )


def _original(slide: Slide) -> OriginalContent:
    return OriginalContent(
        key_message=slide.key_message,
        key_points=list(slide.key_points),
        source_reference=slide.source_reference,
        visual_instruction=slide.visual_instruction,
        presenter_instruction=slide.presenter_instruction,
    )


def _metadata(profile: LectureProfile) -> EnrichmentMetadata:
    return EnrichmentMetadata(
        audience_level=profile.audience_level.value,
        difficulty=profile.difficulty.value,
        lecture_type=profile.lecture_type.value,
        explanation_depth=profile.explanation_depth.value,
    )


def check_slide(orig: Slide, es: EnrichedSlide, profile: LectureProfile) -> list[str]:
    """Independent post-check of one assembled slide against its source slide."""
    problems: list[str] = []
    if (
        es.slide_number != orig.slide_number or es.section_id != orig.section_id
        or es.slide_type != orig.slide_type or es.estimated_explanation_time != orig.estimated_explanation_time
        or es.title != orig.title or es.learning_purpose != orig.learning_purpose
    ):
        problems.append("슬라이드 구조가 원본과 다릅니다.")
    if es.original != _original(orig):
        problems.append("원본 콘텐츠가 변경되었습니다.")
    if es.status == SlideEnrichmentStatus.enrichment_failed:
        if es.enriched is not None or not es.failure_reason:
            problems.append("실패한 슬라이드의 표시가 올바르지 않습니다.")
        return problems
    c = es.enriched
    if c is None or not c.key_message or not c.display_title:
        problems.append("필수 필드(display_title, key_message)가 비어 있습니다.")
        return problems
    policy = profile.source_policy.value
    for f, p in es.provenance.items():
        if f not in CONTENT_FIELDS:
            problems.append(f"알 수 없는 출처 필드: {f}")
        value = getattr(c, f, None)
        empty = value in (None, "", [])
        if p == FieldProvenance.suggested and not empty:
            problems.append(f"{f}: suggested인데 내용이 있습니다.")
        if not empty and policy == "source_only":
            if p == FieldProvenance.llm_example:
                problems.append(f"{f}: source_only에서 llm_example은 허용되지 않습니다.")
            if f == "analogy":
                problems.append("analogy: source_only에서는 허용되지 않습니다.")
            if f == "example" and p not in (FieldProvenance.source_grounded, FieldProvenance.paraphrased_source):
                problems.append("example: source_only에서는 원문 예시만 허용됩니다.")
    for f in CONTENT_FIELDS:
        value = getattr(c, f)
        if value not in (None, "", []) and f not in es.provenance and f != "display_title":
            problems.append(f"{f}: 출처 표시가 없습니다.")
    if profile.speaker_notes.value == "none" and c.presenter_notes:
        problems.append("presenter_notes: speaker_notes=none인데 내용이 있습니다.")
    return problems


def validate_enrichment(
    spec: SlideSpecification, result: EnrichedSlideSpecification, profile: LectureProfile
) -> EnrichmentValidation:
    """Compare the enriched result with the input specification and profile."""
    v = EnrichmentValidation()
    errors = v.errors
    slides = result.slides
    v.slide_count_same = len(slides) == len(spec.slides) == result.slide_count == spec.slide_count
    if not v.slide_count_same:
        errors.append(f"슬라이드 수가 다릅니다: 입력 {len(spec.slides)}장, 결과 {len(slides)}장")
    pairs = list(zip(spec.slides, slides))
    v.slide_number_same = v.slide_count_same and all(a.slide_number == b.slide_number for a, b in pairs)
    v.section_id_same = v.slide_count_same and all(a.section_id == b.section_id for a, b in pairs)
    v.slide_type_same = v.slide_count_same and all(a.slide_type == b.slide_type for a, b in pairs)
    v.duration_same = (
        v.slide_count_same
        and all(a.estimated_explanation_time == b.estimated_explanation_time for a, b in pairs)
        and result.duration_minutes == spec.duration_minutes
        and sum(b.estimated_explanation_time for b in slides) == spec.duration_minutes * 60
    )
    meta = _metadata(profile)
    v.lecture_profile_same = (
        result.metadata == meta
        and all(s.metadata == meta for s in slides)
        and result.version.lecture_profile_hash == hash_profile(profile)
    )
    for name, ok in (
        ("slide_number_same", v.slide_number_same), ("section_id_same", v.section_id_same),
        ("slide_type_same", v.slide_type_same), ("duration_same", v.duration_same),
        ("lecture_profile_same", v.lecture_profile_same),
    ):
        if not ok:
            errors.append(f"{name} 검증 실패")
    problems: list[str] = []
    policy_problems: list[str] = []
    required_problems: list[str] = []
    if v.slide_count_same:
        for orig, es in pairs:
            for p in check_slide(orig, es, profile):
                problems.append(f"slide {orig.slide_number}: {p}")
                if "source_only" in p or "suggested" in p or "speaker_notes" in p:
                    policy_problems.append(p)
                if "필수" in p:
                    required_problems.append(p)
    v.no_structure_mutation = (
        v.slide_number_same and v.section_id_same and v.slide_type_same and v.duration_same
        and not any("구조" in p or "원본" in p for p in problems)
    )
    v.source_policy_valid = not policy_problems
    v.required_field_valid = not required_problems
    v.schema_valid = True  # the result was built through the pydantic models
    errors.extend(problems)
    return v
