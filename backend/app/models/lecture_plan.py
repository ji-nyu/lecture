"""LecturePlan: the lecture structure produced by the LecturePlanner (STAGE 3).

SourceAnalysis + LectureProfile -> LecturePlan

The model enforces the spec's hard rules itself, so an invalid plan can never
be stored:
  * sum(section.duration_minutes) == LecturePlan.duration_minutes
  * every section has at least one slide
  * estimated_slide_count == sum(section.estimated_slides)

Provenance of teaching material (`Origin`):
  source     taken from the uploaded material (with a `source_reference`)
  suggested  a slot the presenter/generator must fill with general knowledge.
             It carries NO text: the planner never states new facts. Only produced
             when source_policy allows it (source_first / expanded).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from .source import SourceLocation


class SectionKind(str, Enum):
    intro = "intro"
    prerequisite = "prerequisite"  # background needed before the main content
    concept = "concept"
    comparison = "comparison"
    caution = "caution"  # limits / trade-offs / common mistakes
    practice = "practice"
    quiz = "quiz"
    summary = "summary"


class Origin(str, Enum):
    source = "source"
    suggested = "suggested"


class PlanTerm(BaseModel):
    """A term the presenter defines when it first appears."""

    term: str
    text: str | None = None  # the SOURCE definition; None when `suggested`
    origin: Origin
    source_reference: SourceLocation | None = None


class PlanExample(BaseModel):
    title: str
    text: str | None = None  # SOURCE excerpt; None when `suggested`
    origin: Origin
    concepts: list[str] = Field(default_factory=list)
    example_first: bool = False  # show the example before the explanation (beginners)
    source_reference: SourceLocation | None = None


class PlanCode(BaseModel):
    language: str | None = None
    mode: str  # snippet | executable
    origin: Origin
    concepts: list[str] = Field(default_factory=list)
    source_reference: SourceLocation | None = None


class PracticeActivity(BaseModel):
    title: str
    objective: str
    steps: list[str]
    concepts: list[str]
    mode: str  # exercise | code
    # What the activity is anchored in (it never states new facts):
    #   source_example / source_code: reproduces a source example / code block
    #   source_concept: an activity structure built around a concept named in the source
    basis: str
    source_reference: SourceLocation | None = None


class QuizSpec(BaseModel):
    kind: str  # checkpoint | final
    question_count: int = Field(ge=1)
    targets: list[str]  # concepts to ask about (no question text yet)


class SourceConstraint(BaseModel):
    """A statement in the source that limits how/how far to teach (SourceAnalysis.scope_notes)."""

    text: str
    source_reference: SourceLocation


class LectureSection(BaseModel):
    id: str
    order: int
    title: str
    purpose: str
    kind: SectionKind
    duration_minutes: int = Field(ge=1)
    importance: int = Field(ge=1, le=5)
    concepts: list[str] = Field(default_factory=list)
    terms: list[PlanTerm] = Field(default_factory=list)  # definitions to give here
    examples: list[PlanExample] = Field(default_factory=list)
    practice: list[PracticeActivity] = Field(default_factory=list)
    code: list[PlanCode] = Field(default_factory=list)
    quiz: QuizSpec | None = None
    source_points: list[str] = Field(default_factory=list)  # SOURCE sentences to stress
    source_section_ids: list[str] = Field(default_factory=list)
    teaching_notes: list[str] = Field(default_factory=list)
    estimated_slides: int = Field(ge=1)


class PresentationHints(BaseModel):
    """Options that shape the SLIDES rather than the structure. Carried to the
    SlidePlanner (STAGE 4); `visual_level` and `slide_density` already affect
    `estimated_slides` here."""

    lecture_tone: str
    speaker_notes: str
    visual_level: str
    slide_density: str
    source_policy: str


class PlanMetrics(BaseModel):
    """Numbers used to compare plans (and shown in the preview later)."""

    section_count: int
    slide_count: int
    concept_count: int
    definition_count: int
    example_count: int
    practice_activity_count: int
    practice_step_count: int
    code_count: int
    quiz_question_count: int
    explanation_minutes: int  # concept + prerequisite + comparison + caution
    practice_minutes: int
    minutes_by_kind: dict[str, int]


class LecturePlan(BaseModel):
    id: str
    project_id: str
    title: str
    audience: str
    duration_minutes: int
    difficulty: str
    lecture_type: str
    explanation_depth: str
    source_policy: str
    learning_objectives: list[str]
    estimated_slide_count: int
    sections: list[LectureSection]
    metrics: PlanMetrics
    presentation_hints: PresentationHints
    source_constraints: list[SourceConstraint] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    profile_id: str
    source_id: str
    planner: str = "rule-v1"
    generated_at: datetime

    @model_validator(mode="after")
    def _check_invariants(self) -> "LecturePlan":
        total = sum(s.duration_minutes for s in self.sections)
        if total != self.duration_minutes:
            raise ValueError(
                f"section durations sum to {total} min, expected {self.duration_minutes}"
            )
        if self.estimated_slide_count != sum(s.estimated_slides for s in self.sections):
            raise ValueError("estimated_slide_count does not match the sections")
        return self
