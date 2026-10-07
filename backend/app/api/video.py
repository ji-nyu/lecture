"""Lecture video from the finished PPT and spoken scripts."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..models.lecture_video import VideoStatusResponse
from ..services.presentation_service import PresentationService
from ..services.video_service import VideoService

router = APIRouter(tags=["video"])


def _service(request: Request) -> VideoService:
    settings = request.app.state.settings
    return VideoService(
        request.app.state.store,
        jobs_root=settings.data_dir / "video_jobs",
        presentation=PresentationService(request.app.state.presentation_provider, request.app.state.store),
        pipeline=getattr(request.app.state, "video_pipeline", None),
        launcher=getattr(request.app.state, "video_launcher", None),
        llm=request.app.state.llm_client,
        settings=settings,
    )


@router.post("/projects/{project_id}/video", response_model=VideoStatusResponse)
def create_video(project_id: str, request: Request, force: bool = False):
    return _service(request).start(project_id, force=force)


@router.get("/projects/{project_id}/video/status", response_model=VideoStatusResponse)
def video_status(project_id: str, request: Request):
    return _service(request).status(project_id)


@router.get("/projects/{project_id}/video/download")
def download_video(project_id: str, request: Request):
    path, result = _service(request).download_path(project_id)
    return FileResponse(path, media_type=result.content_type, filename=result.file_name)
