from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..errors import AnalysisNotReady, AppError, PlanNotReady, ProfileNotReady, SlidesNotReady
from ..models.lecture_plan import LecturePlan
from ..models.project import PresentationStatus, ProjectResponse
from ..services.lecture_planner import LecturePlanner
from ..services.lecture_script_writer import clear_script_cache

router = APIRouter(tags=["lecture-plan"])
logger = logging.getLogger("ailecturegen")


@router.post("/projects/{project_id}/plan", response_model=LecturePlan)
def create_plan(project_id: str, request: Request):
    """SourceAnalysis + LectureProfile -> LecturePlan (STAGE 3).

    Needs a completed analysis and a saved profile. Status goes
    analyzed -> planning -> planned. If planning fails the project returns to
    `analyzed` (the analysis is still valid) and `error_message` explains why.
    """
    store = request.app.state.store
    project = store.get(project_id)
    if project.source_analysis is None:
        raise AnalysisNotReady()
    if project.lecture_profile is None:
        raise ProfileNotReady()

    if project.input_mode == "deck":
        from ..services.deck_importer import apply_imported_deck

        project = apply_imported_deck(store, project)
        return project.lecture_plan
    project.presentation_status = PresentationStatus.planning
    project.error_message = None
    store.save(project)
    try:
        plan = LecturePlanner().plan(project.source_analysis, project.lecture_profile)
    except AppError as exc:
        project.presentation_status = PresentationStatus.analyzed
        project.error_message = exc.message
        store.save(project)
        raise
    project.lecture_plan = plan
    # Slides and the prompt were derived from an older plan.
    project.slide_specification = None
    project.enriched_specification = None
    clear_script_cache(store, project.id)
    project.discard_prompt()  # prompt, presentation job and file
    project.presentation_status = PresentationStatus.planned
    store.save(project)
    return plan


@router.post("/projects/{project_id}/plan/approve", response_model=ProjectResponse)
def approve_plan(project_id: str, request: Request):
    """The professor approves the lecture direction (STAGE 5 preview) -> `ready_to_generate`.

    Needs the plan and its slide structure. Approval only marks the project ready:
    prompt building and presentation generation are later stages. Changing the options,
    re-planning or re-creating the slides withdraws the approval.
    """
    store = request.app.state.store
    project = store.get(project_id)
    if project.lecture_plan is None:
        raise PlanNotReady()
    if project.slide_specification is None:
        raise SlidesNotReady()
    if not project.presentation_stage_started and project.presentation_status not in (
        # approving again must not undo later progress
        PresentationStatus.enriching,
        PresentationStatus.enriched,
        PresentationStatus.enrichment_partial,
        PresentationStatus.ready_for_presentation,
    ):
        project.presentation_status = PresentationStatus.ready_to_generate
    project.error_message = None
    store.save(project)
    return ProjectResponse.from_project(project, include_analysis=False)


@router.get("/projects/{project_id}/plan", response_model=LecturePlan)
def get_plan(project_id: str, request: Request):
    project = request.app.state.store.get(project_id)
    if project.lecture_plan is None:
        raise PlanNotReady()
    return project.lecture_plan
