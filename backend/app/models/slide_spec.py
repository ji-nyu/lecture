"""SlideSpecification: the slide-by-slide structure produced by the SlidePlanner (STAGE 4).

LecturePlan (+ SourceAnalysis for verbatim excerpts) -> SlideSpecification

The SlidePlanner - not the presentation provider - decides the whole slide
structure. Every slide carries the reason it exists (`learning_purpose`); the
model refuses slides without one (spec SuccessMetric_04) and refuses a
specification whose slide times do not add up to the lecture duration.

Content origin of a slide (`content_origin`):
  source      the key message / key points contain text taken from the uploaded material
  structural  the slide is pure structure (title, agenda, quiz, summary...): no facts at all
  suggested   the source has nothing for it; the presenter/generator must fill it with
              general knowledge (only when source_policy allows it). Carries no text.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from .source import SourceLocation


class SlideType(str, Enum):
    title = "title"
    agenda = "agenda"
    concept = "concept"
    definition = "definition"
    comparison = "comparison"
    diagram = "diagram"
    architecture = "architecture"
    workflow = "workflow"
    example = "example"
    practice = "practice"
    code = "code"
    formula = "formula"
    quiz = "quiz"
    summary = "summary"


class ContentOrigin(str, Enum):
    source = "source"
    structural = "structural"
    suggested = "suggested"


class Slide(BaseModel):
    slide_number: int = Field(ge=1)
    section_id: str
    section_title: str
    title: str = Field(min_length=1)
    slide_type: SlideType
    learning_purpose: str = Field(min_length=1)  # why this slide exists
    key_message: str = Field(min_length=1)
    # True when key_message is a verbatim (whitespace-normalised, possibly clipped with "…")
    # sentence of the source; False when it is neutral structure ("‘X’의 역할을 설명한다").
    message_from_source: bool = False
    key_points: list[str] = Field(default_factory=list)
    source_reference: SourceLocation | None = None
    visual_instruction: str = Field(min_length=1)
    presenter_instruction: str = Field(min_length=1)
    # seconds. For practice slides this includes the hands-on time of the activity.
    estimated_explanation_time: int = Field(ge=1)
    # -- extras used by later stages / the preview
    concepts: list[str] = Field(default_factory=list)
    content_origin: ContentOrigin = ContentOrigin.structural


class SlideMetrics(BaseModel):
    slide_count: int
    total_seconds: int
    slides_by_type: dict[str, int]
    slides_by_section: dict[str, int]
    source_slide_count: int  # content_origin == source
    suggested_slide_count: int
    avg_key_points: float
    max_key_points: int
    visual_slide_count: int  # diagram / architecture / workflow / comparison


class SlideStyle(BaseModel):
    """The options that shaped the slides (recorded so the effect is inspectable)."""

    slide_density: str
    visual_level: str
    lecture_tone: str
    speaker_notes: str
    source_policy: str
    audience: str
    difficulty: str
    max_key_points: int


class SlideSpecification(BaseModel):
    lecture_id: str  # == LecturePlan.id
    project_id: str
    title: str
    duration_minutes: int
    slide_count: int
    plan_estimated_slide_count: int
    slides: list[Slide]
    metrics: SlideMetrics
    style: SlideStyle
    warnings: list[str] = Field(default_factory=list)
    planner: str = "rule-v1"
    generated_at: datetime

    @model_validator(mode="after")
    def _check_invariants(self) -> "SlideSpecification":
        if not self.slides:
            raise ValueError("a slide specification needs at least one slide")
        if [s.slide_number for s in self.slides] != list(range(1, len(self.slides) + 1)):
            raise ValueError("slide numbers must run 1..n without gaps")
        if self.slide_count != len(self.slides):
            raise ValueError("slide_count does not match the slides")
        total = sum(s.estimated_explanation_time for s in self.slides)
        expected = self.duration_minutes * 60
        if self.planner == "imported-deck-v1":
            if total < 1 or total > expected:
                raise ValueError(
                    f"imported slide times sum to {total}s, expected 1..{expected}s"
                )
        elif total != expected:
            raise ValueError(
                f"slide times sum to {total}s, expected {expected}s"
            )
        return self
