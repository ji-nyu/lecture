"""Spoken lecture script: one readable narrator text per slide."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SlideScript(BaseModel):
    slide_number: int = Field(ge=1)
    title: str
    slide_type: str
    estimated_seconds: int = Field(ge=1)
    spoken_seconds: int = Field(ge=1)
    text: str
    source: str  # llm | enriched | composed


class LectureScript(BaseModel):
    project_id: str
    title: str
    slide_count: int
    total_seconds: int
    slides: list[SlideScript]
    writer: str = "composed"  # llm | composed | mixed
    fingerprint: str | None = None
    model_name: str | None = None
