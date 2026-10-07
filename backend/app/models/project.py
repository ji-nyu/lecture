"""LectureProject model and its public (API) representation."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel

from .enriched_slide_spec import EnrichedSlideSpecification
from .lecture_plan import LecturePlan
from .lecture_profile import LectureProfile
from .lecture_video import VideoJob, VideoResult
from .presentation import PresentationJob, PresentationResult
from .presentation_prompt import PresentationPrompt
from .slide_spec import SlideSpecification
from .source import SourceAnalysis, SourceFileInfo


class PresentationStatus(str, Enum):
    uploaded = "uploaded"
    analyzing = "analyzing"
    analyzed = "analyzed"
    planning = "planning"
    planned = "planned"
    ready_to_generate = "ready_to_generate"
    # STAGE 6A: LLM content enrichment (the structure is already approved)
    enriching = "enriching"
    enriched = "enriched"  # every slide enriched
    enrichment_partial = "enrichment_partial"  # some/all slides use the rule-based content
    ready_for_presentation = "ready_for_presentation"  # STAGE 6B: the presentation prompt has been built
    # STAGE 7: a presentation provider is working / has delivered / could not deliver
    generating = "generating"
    completed = "completed"
    failed = "failed"


class Project(BaseModel):
    """Persisted LectureProject.

    `final_prompt` is set by STAGE 6B; `presentation_provider`, `presentation_job` and
    `presentation_result` by STAGE 7 (they stay `None` until a presentation is requested).
    """

    id: str
    title: str | None = None
    # `None` until a source file has been uploaded; then `uploaded`, ...
    presentation_status: PresentationStatus | None = None
    # Human-readable reason when the last analysis failed (`presentation_status=failed`).
    error_message: str | None = None

    source_file_path: str | None = None  # relative to the project directory
    source_file: SourceFileInfo | None = None

    source_analysis: SourceAnalysis | None = None  # STAGE 2
    lecture_profile: LectureProfile | None = None

    lecture_plan: LecturePlan | None = None  # STAGE 3

    slide_specification: SlideSpecification | None = None  # STAGE 4
    enriched_specification: EnrichedSlideSpecification | None = None  # STAGE 6A

    # STAGE 6B: `final_prompt` is the text; `presentation_prompt` is the same text plus how it was built.
    final_prompt: str | None = None
    presentation_prompt: PresentationPrompt | None = None

    # STAGE 7: which provider was asked, the job it returned and the finished file (if any).
    presentation_provider: str | None = None
    presentation_job: PresentationJob | None = None
    presentation_result: PresentationResult | None = None

    video_job: VideoJob | None = None
    video_result: VideoResult | None = None

    # source: upload notes and generate a PPT. deck: the uploaded PPTX is the presentation.
    input_mode: str = "source"

    created_at: datetime
    updated_at: datetime

    @property
    def presentation_finished(self) -> bool:
        """A presentation was delivered, or a generation attempt failed (a retry is possible).
        `failed` without a job is a failed analysis, not a failed presentation."""
        return self.presentation_status == PresentationStatus.completed or (
            self.presentation_status == PresentationStatus.failed and self.presentation_job is not None
        )

    @property
    def presentation_stage_started(self) -> bool:
        return self.presentation_finished or self.presentation_status == PresentationStatus.generating

    def discard_prompt(self) -> None:
        """Drop the prompt and everything built from it (job, result). Call this whenever an
        upstream artifact changed. A project that had reached the presentation stage falls back to
        `ready_for_presentation` first, so the callers' existing "was it approved?" checks keep working."""
        self.final_prompt = None
        self.presentation_prompt = None
        if self.input_mode == "deck":
            self.discard_video()
            return
        if self.presentation_stage_started:
            self.presentation_status = PresentationStatus.ready_for_presentation
        self.presentation_provider = None
        self.presentation_job = None
        self.presentation_result = None
        self.discard_video()

    def discard_video(self) -> None:
        self.video_job = None
        self.video_result = None


class ProjectResponse(BaseModel):
    """What the API returns. Server-side file paths are never exposed."""

    id: str
    title: str | None
    presentation_status: PresentationStatus | None
    error_message: str | None = None
    source_file: SourceFileInfo | None
    has_analysis: bool = False
    source_analysis: SourceAnalysis | None = None
    lecture_profile: LectureProfile | None
    has_plan: bool = False  # the plan itself is read with GET /projects/{id}/plan
    has_slides: bool = False  # ...and the slides with GET /projects/{id}/slides
    has_enrichment: bool = False  # ...and the enriched content with GET /projects/{id}/enrichment
    has_prompt: bool = False  # ...and the presentation prompt with GET /projects/{id}/prompt
    presentation_provider: str | None = None  # STAGE 7: the provider asked for the presentation
    has_presentation: bool = False  # ...its file is downloadable (GET /projects/{id}/presentation/result)
    has_video: bool = False
    input_mode: str = "source"
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_project(cls, p: Project, include_analysis: bool = True) -> "ProjectResponse":
        return cls(
            id=p.id,
            title=p.title,
            presentation_status=p.presentation_status,
            error_message=p.error_message,
            source_file=p.source_file,
            has_analysis=p.source_analysis is not None,
            source_analysis=p.source_analysis if include_analysis else None,
            lecture_profile=p.lecture_profile,
            has_plan=p.lecture_plan is not None,
            has_slides=p.slide_specification is not None,
            has_enrichment=p.enriched_specification is not None,
            has_prompt=p.presentation_prompt is not None and p.final_prompt is not None,
            presentation_provider=p.presentation_provider,
            has_presentation=p.presentation_result is not None,
            has_video=p.video_result is not None,
            input_mode=p.input_mode,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


class CreateProjectRequest(BaseModel):
    title: str | None = None
