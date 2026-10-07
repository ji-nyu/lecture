"""Presentation models (STAGE 7): what the Lecture Engine hands to a presentation provider and
what it gets back.

    PresentationPrompt (6B) -> PresentationRequest -> PresentationProvider -> job -> result file

Nothing here knows which provider is behind the interface. The request carries the prompt and a
few neutral facts about it; the job/result records say which provider produced them and whether it
was a mock (`is_mock`), so a mock file can never be mistaken for a real presentation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    # The provider exists but cannot be used (no integration / no credentials). Never a failure of
    # the lecture itself.
    not_configured = "not_configured"


class SlideBrief(BaseModel):
    """One slide of the approved structure (what the provider must render, in this order)."""

    slide_number: int = Field(ge=1)
    title: str
    slide_type: str


class PresentationRequest(BaseModel):
    project_id: str
    title: str
    final_prompt: str = Field(min_length=1)
    prompt_hash: str
    slide_count: int = Field(ge=1)
    slides: list[SlideBrief]
    duration_minutes: int | None = None


class ProviderStatus(BaseModel):
    """What a provider reports about a job (create_presentation / get_status)."""

    job_id: str
    state: JobState
    progress: int = Field(default=0, ge=0, le=100)
    stage: str | None = None  # short human-readable current step
    message: str | None = None  # a user-facing reason for `failed` / `not_configured`


class ProviderResult(BaseModel):
    """What a provider reports about a finished job (get_result)."""

    job_id: str
    file_name: str
    content_type: str
    size_bytes: int = Field(ge=0)
    slide_count: int | None = None  # when the provider can tell


class DownloadedFile(BaseModel):
    file_name: str
    content_type: str
    data: bytes = Field(repr=False)


class PresentationJob(BaseModel):
    """The job as stored with the project (provider-neutral; `job_id` is the provider's own id)."""

    provider: str
    is_mock: bool
    job_id: str
    state: JobState
    progress: int = 0
    stage: str | None = None
    message: str | None = None
    prompt_hash: str  # the prompt this job was created from
    created_at: datetime
    updated_at: datetime


class PresentationResult(BaseModel):
    """The finished presentation file. The file lives with the project; no server path is exposed."""

    provider: str
    is_mock: bool
    job_id: str
    prompt_hash: str
    file_name: str  # the name offered for download
    content_type: str
    size_bytes: int
    sha256: str
    slide_count: int | None = None
    completed_at: datetime
    note: str | None = None  # e.g. "Mock provider: placeholder deck, not a designed presentation"


class PresentationStatusResponse(BaseModel):
    """GET/POST /projects/{id}/presentation(/status)."""

    state: str  # not_started | queued | running | completed | failed | not_configured
    project_status: str | None
    provider: str
    is_mock: bool
    provider_configured: bool
    provider_message: str | None = None
    job_id: str | None = None
    progress: int = 0
    stage: str | None = None
    message: str | None = None
    has_result: bool = False
    prompt_hash: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
