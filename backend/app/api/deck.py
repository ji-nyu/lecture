"""Import an instructor-made PPTX and use it for scripts + video."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile

from ..errors import AnalysisNotReady, DeckNeedsPptx, EmptyDocument, FileTooLarge
from ..models.lecture_profile import (
    AudienceLevel,
    Difficulty,
    ExplanationDepth,
    LectureOptionsInput,
    LectureType,
    SourcePolicy,
)
from ..models.project import PresentationStatus, ProjectResponse
from ..models.source import SourceFileInfo
from ..services.analyzer_factory import build_analyzer
from ..services.deck_importer import apply_imported_deck
from ..services.document_parser import DocumentParser
from ..services.lecture_profile_service import build_profile
from ..storage.project_store import utcnow

router = APIRouter(tags=["deck"])

_CHUNK = 1024 * 1024


@router.post("/projects/{project_id}/deck", response_model=ProjectResponse)
async def import_deck(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
    duration_minutes: int = Form(20),
):
    store = request.app.state.store
    settings = request.app.state.settings
    project = store.get(project_id)

    original_name = os.path.basename((file.filename or "").replace("\\", "/")).strip()
    ext = Path(original_name).suffix.lower()
    if not original_name or ext != ".pptx":
        raise DeckNeedsPptx()

    project_dir = store.project_dir(project_id)
    tmp_path = project_dir / "upload.tmp"
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise FileTooLarge(f"파일 크기가 {settings.max_upload_mb}MB 를 초과했습니다.")
                out.write(chunk)
        if size == 0:
            raise EmptyDocument()
        source_dir = store.clear_source_dir(project_id)
        stored = source_dir / "source.pptx"
        os.replace(tmp_path, stored)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    project.input_mode = "deck"
    project.source_file_path = stored.relative_to(project_dir).as_posix()
    project.source_file = SourceFileInfo(
        filename=original_name,
        extension=".pptx",
        size_bytes=size,
        content_type=file.content_type,
        uploaded_at=utcnow(),
    )
    if not project.title:
        project.title = Path(original_name).stem
    project.error_message = None
    project.source_analysis = None
    project.lecture_plan = None
    project.slide_specification = None
    project.enriched_specification = None
    project.discard_video()
    store.save(project)

    project.presentation_status = PresentationStatus.analyzing
    store.save(project)
    path = store.project_dir(project_id) / project.source_file_path
    material = DocumentParser().parse(path, project.source_file.filename, source_id=uuid.uuid4().hex)
    analyzer = build_analyzer(request.app.state.settings, request.app.state.llm_client)
    analysis = analyzer.analyze(material)
    store.write_artifact(project_id, "source_material.json", material)
    project.source_analysis = analysis
    project.presentation_status = PresentationStatus.analyzed
    store.save(project)

    minutes = max(5, min(240, int(duration_minutes or 20)))
    project.lecture_profile = build_profile(
        project.id,
        LectureOptionsInput(
            audience_level=AudienceLevel.general,
            duration_minutes=minutes,
            difficulty=Difficulty.introductory,
            lecture_type=LectureType.theory,
            explanation_depth=ExplanationDepth.concise,
            source_policy=SourcePolicy.source_first,
        ),
        existing=project.lecture_profile,
    )
    store.save(project)
    project = apply_imported_deck(store, project)
    return ProjectResponse.from_project(project)


@router.post("/projects/{project_id}/deck/prepare", response_model=ProjectResponse)
def prepare_deck(project_id: str, request: Request):
    store = request.app.state.store
    project = store.get(project_id)
    if project.input_mode != "deck":
        raise DeckNeedsPptx("업로드한 PPT 강의가 아닙니다.")
    if project.source_analysis is None:
        raise AnalysisNotReady()
    project = apply_imported_deck(store, project)
    return ProjectResponse.from_project(project)
