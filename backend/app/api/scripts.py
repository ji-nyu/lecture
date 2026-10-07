"""Spoken lecture scripts for the preview (one readable text per slide)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..errors import PlanNotReady, SlidesNotReady
from ..models.lecture_script import LectureScript
from ..services.lecture_script_writer import load_or_build_scripts
from ..services.llm_client import create_llm_client

router = APIRouter(tags=["scripts"])
logger = logging.getLogger("ailecturegen")


@router.get("/projects/{project_id}/scripts", response_model=LectureScript)
def get_scripts(project_id: str, request: Request):
    store = request.app.state.store
    project = store.get(project_id)
    if project.lecture_plan is None:
        raise PlanNotReady()
    if project.slide_specification is None:
        raise SlidesNotReady()
    settings = request.app.state.settings
    llm = request.app.state.llm_client or create_llm_client(settings)
    return load_or_build_scripts(store, project, llm)
