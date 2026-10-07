"""STAGE 7 endpoints: turn the prompt into a presentation through the configured provider.

    POST /projects/{id}/presentation            start (from `ready_for_presentation`, or again to regenerate)
    GET  /projects/{id}/presentation/status     progress; polling it also collects the finished file
    GET  /projects/{id}/presentation/result     the finished result (metadata)
    GET  /projects/{id}/presentation/download   the file itself

The provider comes from GENSPARK_MODE (`mock` by default). The lecture (plan, slides, enrichment,
prompt) is never changed here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..models.presentation import PresentationResult, PresentationStatusResponse
from ..services.presentation_service import PresentationService
from .prompt import build_prompt

router = APIRouter(tags=["presentation"])


def _service(request: Request) -> PresentationService:
    return PresentationService(request.app.state.presentation_provider, request.app.state.store)


@router.post("/projects/{project_id}/presentation", response_model=PresentationStatusResponse)
def create_presentation(project_id: str, request: Request):
    """From an approved lecture: build the prompt if it is missing, then start the provider."""
    project = request.app.state.store.get(project_id)
    if project.input_mode == "deck":
        if project.presentation_result is None:
            from ..services.deck_importer import apply_imported_deck

            apply_imported_deck(request.app.state.store, project)
        return _service(request).status(project_id)
    if project.final_prompt is None:
        build_prompt(project_id, request)
    return _service(request).start(project_id)


@router.get("/projects/{project_id}/presentation/status", response_model=PresentationStatusResponse)
def presentation_status(project_id: str, request: Request):
    return _service(request).status(project_id)


@router.get("/projects/{project_id}/presentation/result", response_model=PresentationResult)
def presentation_result(project_id: str, request: Request):
    return _service(request).result(project_id)


@router.get("/projects/{project_id}/presentation/download")
def download_presentation(project_id: str, request: Request):
    path, result = _service(request).download_path(project_id)
    return FileResponse(path, media_type=result.content_type, filename=result.file_name)
