from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Request

from ..errors import AnalysisNotReady, AppError, DocumentParsingError, SourceNotUploaded
from ..models.project import PresentationStatus, ProjectResponse
from ..models.source import SourceAnalysis
from ..services.document_parser import DocumentParser
from ..services.analyzer_factory import build_analyzer

router = APIRouter(tags=["analysis"])
logger = logging.getLogger("ailecturegen")


@router.post("/projects/{project_id}/analyze", response_model=ProjectResponse)
def analyze_project(project_id: str, request: Request):
    """Parse the uploaded source and produce a SourceAnalysis (STAGE 2).

    Synchronous (runs in FastAPI's thread pool). The rule-based analyzer takes
    well under a second; hybrid/llm modes add the LLM latency. Status goes uploaded -> analyzing -> analyzed
    (or `failed` with `error_message`).
    """
    store = request.app.state.store
    project = store.get(project_id)
    if not project.source_file_path or not project.source_file:
        raise SourceNotUploaded()

    project.presentation_status = PresentationStatus.analyzing
    project.error_message = None
    store.save(project)

    try:
        path = store.project_dir(project_id) / project.source_file_path
        material = DocumentParser().parse(
            path, project.source_file.filename, source_id=uuid.uuid4().hex
        )
        # ANALYZER_MODE picks heuristic (default) / hybrid / llm. LLM problems
        # never raise here: the analyzer falls back to the heuristic result.
        analyzer = build_analyzer(request.app.state.settings, request.app.state.llm_client)
        analysis = analyzer.analyze(material)
    except AppError as exc:
        project.presentation_status = PresentationStatus.failed
        project.error_message = exc.message
        store.save(project)
        raise
    except Exception as exc:  # never leak internals to the client
        logger.exception("Analysis failed", exc_info=exc)
        project.presentation_status = PresentationStatus.failed
        project.error_message = "강의자료를 분석하는 중 문제가 발생했습니다."
        store.save(project)
        raise DocumentParsingError(project.error_message) from exc

    store.write_artifact(project_id, "source_material.json", material)
    project.source_analysis = analysis
    # Anything derived from an older analysis is stale.
    project.lecture_plan = None
    project.slide_specification = None
    project.enriched_specification = None
    project.discard_prompt()  # prompt, presentation job and file
    project.presentation_status = PresentationStatus.analyzed
    store.save(project)
    return ProjectResponse.from_project(project)


@router.get("/projects/{project_id}/analysis", response_model=SourceAnalysis)
def get_analysis(project_id: str, request: Request):
    project = request.app.state.store.get(project_id)
    if project.source_analysis is None:
        raise AnalysisNotReady()
    return project.source_analysis
