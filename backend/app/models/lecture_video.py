"""Lecture video: one MP4 that shows each PPT slide while its script is spoken."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class VideoState(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class VideoJob(BaseModel):
    job_id: str
    state: VideoState
    progress: int = Field(default=0, ge=0, le=100)
    stage: str | None = None
    message: str | None = None
    fingerprint: str
    created_at: datetime
    updated_at: datetime


class VideoResult(BaseModel):
    job_id: str
    file_name: str
    content_type: str = "video/mp4"
    size_bytes: int = Field(ge=0)
    duration_seconds: int = Field(ge=0)
    slide_count: int = Field(ge=1)
    fingerprint: str
    completed_at: datetime
    note: str | None = None


class VideoStatusResponse(BaseModel):
    state: str  # not_started | queued | running | completed | failed
    progress: int = 0
    stage: str | None = None
    message: str | None = None
    has_result: bool = False
    duration_seconds: int | None = None
    slide_count: int | None = None
    job_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
