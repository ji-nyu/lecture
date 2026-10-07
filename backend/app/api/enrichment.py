"""STAGE 6A endpoints: enrich the approved slide structure with LLM-written content.

    POST /projects/{id}/enrich            run (uses the cache; ?force=true ignores it)
    GET  /projects/{id}/enrichment        the EnrichedSlideSpecification
    GET  /projects/{id}/enrichment/status progress / failures / provider
    POST /projects/{id}/enrichment/retry  ask the LLM again for the failed slides only

The structure (LecturePlan, SlideSpecification) is never changed here. A slide the LLM
cannot deliver keeps its rule-based content, so a provider outage never blocks the project
(status `enrichment_partial`). The call is synchronous, like the analysis: it returns when
the last batch is done.
"""

from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..errors import (
    AppError,
    EnrichmentError,
    EnrichmentNotAllowed,
    EnrichmentNotReady,
    PlanNotReady,
    ProfileNotReady,
    SlidesNotReady,
)
from ..models.enriched_slide_spec import EnrichedSlideSpecification, RunStatus, SlideEnrichmentStatus
from ..models.project import PresentationStatus, Project
from ..services.enrichment_cache import JsonFileCache
from ..services.slide_content_enricher import SlideContentEnricher, hash_profile, hash_spec

router = APIRouter(tags=["enrichment"])
logger = logging.getLogger("ailecturegen")

# The structure must be approved before content is written for it.
_ALLOWED_STATES = (
    PresentationStatus.ready_to_generate,
    PresentationStatus.enriching,  # a previous run may have been interrupted
    PresentationStatus.enriched,
    PresentationStatus.enrichment_partial,
    PresentationStatus.ready_for_presentation,
)


class StaleEnrichment(AppError):
    code = "EnrichmentStale"
    status_code = 409
    message = "콘텐츠를 보강하는 동안 강의 옵션이나 슬라이드 구성이 바뀌어 결과를 저장하지 않았습니다. 다시 실행해 주세요."


class FailedSlide(BaseModel):
    slide_number: int
    reason: str
    message: str


class EnrichmentStatusResponse(BaseModel):
    status: str  # not_started | enriching | enriched | enrichment_partial | enrichment_failed
    project_status: str | None
    configured: bool  # is a content LLM available at all
    provider: str | None = None
    model: str | None = None
    slide_count: int = 0
    enriched_count: int = 0
    failed_count: int = 0
    failed_slides: list[FailedSlide] = []
    llm_calls: int = 0
    cache_hits: int = 0
    validation_passed: bool | None = None
    generated_at: str | None = None


def _source_inputs(store, project: Project) -> tuple[str | None, str | None]:
    """(raw source text, sha256 of the uploaded file) when available."""
    text = None
    art = store.project_dir(project.id) / "source_material.json"
    try:
        if art.is_file():
            text = json.loads(art.read_text(encoding="utf-8")).get("raw_text")
    except (OSError, ValueError):
        text = None
    digest = None
    if project.source_file_path:
        f = store.project_dir(project.id) / project.source_file_path
        try:
            if f.is_file():
                digest = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            digest = None
    return text, digest


def _run(request: Request, project_id: str, *, force: bool, retry: bool) -> EnrichedSlideSpecification:
    store = request.app.state.store
    settings = request.app.state.settings
    project = store.get(project_id)
    if project.lecture_plan is None:
        raise PlanNotReady()
    if project.slide_specification is None:
        raise SlidesNotReady()
    if project.lecture_profile is None:
        raise ProfileNotReady()
    if project.presentation_status not in _ALLOWED_STATES and not project.presentation_finished:
        raise EnrichmentNotAllowed()
    if retry and project.enriched_specification is None:
        raise EnrichmentNotReady()

    resume_status = project.presentation_status
    project.presentation_status = PresentationStatus.enriching
    project.error_message = None
    store.save(project)

    text, digest = _source_inputs(store, project)
    spec, plan, profile = project.slide_specification, project.lecture_plan, project.lecture_profile
    enricher = SlideContentEnricher(
        request.app.state.content_client,
        batch_size=settings.enrichment_batch_size,
        workers=settings.enrichment_workers,
        cache=(
            JsonFileCache(store.project_dir(project_id) / "enrichment_cache.json")
            if settings.enrichment_cache_enabled
            else None  # a private in-memory cache: nothing is reused between runs
        ),
        provider_name=settings.enrichment_provider,
    )
    try:
        result = enricher.enrich(
            spec, plan, profile, project.source_analysis,
            source_text=text, source_hash=digest, force=force,
            previous=project.enriched_specification if retry else None,
        )
    except Exception as exc:
        fresh = store.get(project_id)
        if fresh.presentation_status == PresentationStatus.enriching:  # give the state back
            fresh.presentation_status = (
                resume_status if resume_status != PresentationStatus.enriching
                else PresentationStatus.ready_to_generate
            )
        message = exc.message if isinstance(exc, AppError) else "슬라이드 콘텐츠를 보강하는 중 문제가 발생했습니다."
        fresh.error_message = message
        store.save(fresh)
        if isinstance(exc, AppError):
            raise
        logger.exception("Enrichment failed", exc_info=exc)  # server log only, never sent to the client
        raise EnrichmentError(message) from exc

    # The run can take a while: make sure the inputs did not change meanwhile.
    fresh = store.get(project_id)
    if (
        fresh.slide_specification is None or fresh.lecture_profile is None
        or hash_spec(fresh.slide_specification) != hash_spec(spec)
        or hash_profile(fresh.lecture_profile) != hash_profile(profile)
    ):
        raise StaleEnrichment()
    fresh.enriched_specification = result
    fresh.discard_prompt()  # a prompt (and presentation) built from the earlier content is stale
    fresh.presentation_status = (
        PresentationStatus.enriched if result.status == RunStatus.enriched else PresentationStatus.enrichment_partial
    )
    fresh.error_message = None
    store.save(fresh)
    return result


@router.post("/projects/{project_id}/enrich", response_model=EnrichedSlideSpecification)
def enrich(project_id: str, request: Request, force: bool = False):
    return _run(request, project_id, force=force, retry=False)


@router.post("/projects/{project_id}/enrichment/retry", response_model=EnrichedSlideSpecification)
def retry_enrichment(project_id: str, request: Request):
    """Only the slides that failed are sent to the LLM again; the others are kept as they are."""
    return _run(request, project_id, force=False, retry=True)


@router.get("/projects/{project_id}/enrichment", response_model=EnrichedSlideSpecification)
def get_enrichment(project_id: str, request: Request):
    project = request.app.state.store.get(project_id)
    if project.enriched_specification is None:
        raise EnrichmentNotReady()
    return project.enriched_specification


@router.get("/projects/{project_id}/enrichment/status", response_model=EnrichmentStatusResponse)
def enrichment_status(project_id: str, request: Request):
    project = request.app.state.store.get(project_id)
    client = request.app.state.content_client
    ps = project.presentation_status.value if project.presentation_status else None
    e = project.enriched_specification
    base = dict(project_status=ps, configured=client is not None,
                provider=client.provider if client else request.app.state.settings.enrichment_provider,
                model=client.model if client else None)
    if e is None:
        running = project.presentation_status == PresentationStatus.enriching
        return EnrichmentStatusResponse(status="enriching" if running else "not_started", **base)
    failed = [
        FailedSlide(slide_number=s.slide_number, reason=s.failure_reason or "unexpected", message=s.failure_message or "")
        for s in e.slides if s.status == SlideEnrichmentStatus.enrichment_failed
    ]
    return EnrichmentStatusResponse(
        status=e.status.value,
        slide_count=e.slide_count,
        enriched_count=e.stats.enriched_count,
        failed_count=e.stats.failed_count,
        failed_slides=failed,
        llm_calls=e.stats.llm_calls,
        cache_hits=e.stats.cache_hits,
        validation_passed=e.validation.passed,
        generated_at=e.version.generation_timestamp.isoformat(),
        **{**base, "provider": e.version.model_provider, "model": e.version.model_name},
    )
