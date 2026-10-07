from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile

from ..errors import EmptyDocument, FileTooLarge, UnsupportedFileType
from ..models.project import PresentationStatus, ProjectResponse
from ..models.source import ACCEPTED_EXTENSIONS, SourceFileInfo
from ..storage.project_store import utcnow

router = APIRouter(tags=["upload"])

_CHUNK = 1024 * 1024


@router.post("/projects/{project_id}/upload", response_model=ProjectResponse)
async def upload_source(
    project_id: str, request: Request, file: UploadFile = File(...)
):
    store = request.app.state.store
    settings = request.app.state.settings
    project = store.get(project_id)  # 404 early

    original_name = os.path.basename((file.filename or "").replace("\\", "/")).strip()
    ext = Path(original_name).suffix.lower()
    if not original_name or ext not in ACCEPTED_EXTENSIONS:
        raise UnsupportedFileType(
            "지원하지 않는 파일 형식입니다. 다음 형식만 업로드할 수 있습니다: "
            + ", ".join(e.lstrip(".").upper() for e in ACCEPTED_EXTENSIONS)
        )

    # Stream to a temp file inside the project dir, enforcing the size limit.
    project_dir = store.project_dir(project_id)
    tmp_path = project_dir / "upload.tmp"
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while chunk := await file.read(_CHUNK):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise FileTooLarge(
                        f"파일 크기가 {settings.max_upload_mb}MB 를 초과했습니다."
                    )
                out.write(chunk)
        if size == 0:
            raise EmptyDocument()

        # Replace any previously uploaded source (stored under a fixed safe name).
        source_dir = store.clear_source_dir(project_id)
        stored = source_dir / f"source{ext}"
        os.replace(tmp_path, stored)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    project.source_file_path = stored.relative_to(project_dir).as_posix()
    project.source_file = SourceFileInfo(
        filename=original_name,
        extension=ext,
        size_bytes=size,
        content_type=file.content_type,
        uploaded_at=utcnow(),
    )
    if not project.title:
        project.title = Path(original_name).stem
    project.input_mode = "source"
    project.presentation_status = PresentationStatus.uploaded
    project.error_message = None
    # A new source invalidates anything derived from the previous one.
    project.source_analysis = None
    project.lecture_plan = None
    project.slide_specification = None
    project.enriched_specification = None
    project.discard_prompt()  # prompt, presentation job and file
    store.save(project)
    return ProjectResponse.from_project(project)
