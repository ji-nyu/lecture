"""STAGE 6B endpoints: build and read the presentation prompt.

    POST /projects/{id}/prompt        build it from the approved lecture (uses the enriched content if there is any)
    GET  /projects/{id}/prompt        the PresentationPrompt (text + how it was built)
    GET  /projects/{id}/prompt/text   only the text (`text/plain`), ready to copy

Building is deterministic, needs no LLM and no network, and never changes the lecture itself. It only
stores the prompt and moves the project to `ready_for_presentation`. Sending it to a presentation
provider is `api/presentation.py` (STAGE 7); nothing here talks to a provider.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ..errors import (
    AppError,
    PlanNotReady,
    ProfileNotReady,
    PromptBuildError,
    PromptNotAllowed,
    PromptNotReady,
    SlidesNotReady,
)
from ..models.presentation_prompt import PresentationPrompt
from ..models.project import PresentationStatus
from ..models.source import SourceMaterial
from ..services.lecture_script_writer import load_or_build_scripts
from ..services.llm_client import create_llm_client
from ..services.prompt_builder import GensparkPromptBuilder

router = APIRouter(tags=["prompt"])
logger = logging.getLogger("ailecturegen")

# The structure must be approved and no enrichment may be running.
_ALLOWED_STATES = (
    PresentationStatus.ready_to_generate,
    PresentationStatus.enriched,
    PresentationStatus.enrichment_partial,
    PresentationStatus.ready_for_presentation,
)


def _load_material(store, project_id: str) -> SourceMaterial | None:
    path = store.project_dir(project_id) / "source_material.json"
    try:
        if path.is_file():
            return SourceMaterial.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        logger.warning("source_material.json could not be read; building the prompt without it")
    return None


@router.post("/projects/{project_id}/prompt", response_model=PresentationPrompt)
def build_prompt(project_id: str, request: Request):
    store = request.app.state.store
    project = store.get(project_id)
    if project.lecture_plan is None:
        raise PlanNotReady()
    if project.slide_specification is None:
        raise SlidesNotReady()
    if project.lecture_profile is None:
        raise ProfileNotReady()
    # A finished (or failed) presentation may be rebuilt: the structure is still approved.
    if project.presentation_status not in _ALLOWED_STATES and not project.presentation_finished:
        raise PromptNotAllowed()

    try:
        settings = request.app.state.settings
        llm = request.app.state.llm_client or create_llm_client(settings)
        script = load_or_build_scripts(store, project, llm)
        result = GensparkPromptBuilder().build(
            project_id=project.id,
            profile=project.lecture_profile,
            plan=project.lecture_plan,
            spec=project.slide_specification,
            analysis=project.source_analysis,
            material=_load_material(store, project.id),
            enriched=project.enriched_specification,
            script=script,
        )
    except AppError:
        raise
    except Exception as exc:
        logger.exception("Prompt building failed", exc_info=exc)  # server log only
        raise PromptBuildError() from exc

    if not result.validation.passed:
        # A prompt that does not match the specification is never stored or handed on.
        logger.error("Prompt validation failed: %s", result.validation.errors)
        raise PromptBuildError(details=result.validation.errors)

    previous = project.presentation_prompt
    if previous is not None and previous.prompt_hash == result.prompt_hash:
        # Same text as before: a presentation already made from it (or its failure) stays valid.
        project.final_prompt = result.final_prompt
        project.presentation_prompt = result
        if not project.presentation_finished:
            project.presentation_status = PresentationStatus.ready_for_presentation
    else:
        project.discard_prompt()  # a presentation made from another prompt no longer matches
        project.final_prompt = result.final_prompt
        project.presentation_prompt = result
        project.presentation_status = PresentationStatus.ready_for_presentation
        project.error_message = None
    store.save(project)
    return result


@router.get("/projects/{project_id}/prompt", response_model=PresentationPrompt)
def get_prompt(project_id: str, request: Request):
    project = request.app.state.store.get(project_id)
    if project.presentation_prompt is None or project.final_prompt is None:
        raise PromptNotReady()
    return project.presentation_prompt


@router.get("/projects/{project_id}/prompt/text", response_class=PlainTextResponse)
def get_prompt_text(project_id: str, request: Request):
    project = request.app.state.store.get(project_id)
    if project.final_prompt is None:
        raise PromptNotReady()
    return PlainTextResponse(project.final_prompt, media_type="text/plain; charset=utf-8")
