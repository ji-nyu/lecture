from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..errors import AppError, PlanNotReady, SlidesNotReady
from ..models.project import PresentationStatus
from ..models.slide_spec import SlideSpecification
from ..services.lecture_script_writer import clear_script_cache
from ..services.slide_planner import SlidePlanner

router = APIRouter(tags=["slides"])

# States that rest on the current slides (approval, and everything after it).
_APPROVED_STATES = (
    PresentationStatus.ready_to_generate,
    PresentationStatus.enriching,
    PresentationStatus.enriched,
    PresentationStatus.enrichment_partial,
    PresentationStatus.ready_for_presentation,
)
logger = logging.getLogger("ailecturegen")


@router.post("/projects/{project_id}/slides", response_model=SlideSpecification)
def create_slides(project_id: str, request: Request):
    """LecturePlan -> SlideSpecification (STAGE 4). Needs a saved plan.

    The slide structure is fixed here, before any presentation provider is involved.
    Creating slides again replaces the old ones and withdraws an earlier approval.
    """
    store = request.app.state.store
    project = store.get(project_id)
    if project.lecture_plan is None:
        raise PlanNotReady()
    if project.input_mode == "deck":
        from ..services.deck_importer import apply_imported_deck

        project = apply_imported_deck(store, project)
        return project.slide_specification
    try:
        spec = SlidePlanner().plan(project.lecture_plan, project.source_analysis)
    except AppError as exc:
        project.error_message = exc.message
        store.save(project)
        raise
    project.slide_specification = spec
    project.enriched_specification = None  # written for the old slides
    clear_script_cache(store, project.id)
    project.discard_prompt()  # prompt, presentation job and file (a finished project falls back to ready_for_presentation)
    project.error_message = None
    if project.presentation_status in _APPROVED_STATES:
        project.presentation_status = PresentationStatus.planned  # approval covered the old slides
    store.save(project)
    return spec


@router.get("/projects/{project_id}/slides", response_model=SlideSpecification)
def get_slides(project_id: str, request: Request):
    store = request.app.state.store
    project = store.get(project_id)
    if project.slide_specification is None:
        raise SlidesNotReady()
    from ..services.lecture_script_builder import _is_imported, rebalance_imported_times
    from ..services.lecture_script_writer import clear_script_cache

    if (
        project.lecture_plan is not None
        and _is_imported(project.lecture_plan)
        and project.slide_specification is not None
    ):
        total = project.lecture_plan.duration_minutes * 60
        if rebalance_imported_times(project.slide_specification, total):
            project.discard_video()
            clear_script_cache(store, project.id)
            store.save(project)
    return project.slide_specification
