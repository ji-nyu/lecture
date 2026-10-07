"""EnrichedSlideSpecification (STAGE 6A): the SlideSpecification plus LLM-written content.

The rule engine decides the structure (STAGE 3/4). The LLM only writes content INSIDE
that structure. Every enriched slide therefore repeats the structural fields of the
original slide unchanged (slide_number, section_id, slide_type, time) and carries the
original text next to the new text, so nothing the planner produced is ever lost:

    original   what the SlidePlanner wrote (kept verbatim)
    enriched   what the LLM added (`None` when the slide fell back)
    provenance for every enriched field: where it comes from (see FieldProvenance)
    grounding  source locations behind the slide + warnings from the GroundingValidator

A slide whose enrichment failed keeps `enriched = None` and `status = enrichment_failed`
with a readable `failure_message`; consumers then use `original`. One failed slide never
fails the lecture.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, computed_field, model_validator

from .slide_spec import SlideType
from .source import SourceLocation


class FieldProvenance(str, Enum):
    source_grounded = "source_grounded"  # stated in the source
    paraphrased_source = "paraphrased_source"  # the source, reworded with the same meaning
    llm_explanation = "llm_explanation"  # explains a source concept so it is easier to understand
    llm_example = "llm_example"  # an educational example made up by the LLM
    llm_inference = "llm_inference"  # inferred from the source, not stated in it
    suggested = "suggested"  # placeholder: a later step must write it (carries no text)


class SlideEnrichmentStatus(str, Enum):
    enriched = "enriched"
    enrichment_failed = "enrichment_failed"


class RunStatus(str, Enum):
    enriched = "enriched"  # every slide enriched
    enrichment_partial = "enrichment_partial"  # some slides fell back to the rule-based content
    enrichment_failed = "enrichment_failed"  # every slide fell back (LLM unavailable, ...)


# ---- enriched content ------------------------------------------------------
class PracticeContent(BaseModel):
    goal: str | None = None
    prerequisites: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    expected_result: str | None = None
    cautions: list[str] = Field(default_factory=list)


class CodeExplanation(BaseModel):
    purpose: str | None = None
    key_lines: list[str] = Field(default_factory=list)
    execution_flow: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


class QuizContent(BaseModel):
    question: str | None = None
    choices: list[str] = Field(default_factory=list)
    answer: str | None = None
    explanation: str | None = None


CONTENT_FIELDS = (
    "display_title",
    "key_message",
    "body_points",
    "explanation",
    "example",
    "analogy",
    "practice_instruction",
    "code_explanation",
    "visual_instruction",
    "presenter_notes",
    "quiz_content",
    "summary_message",
)


class EnrichedContent(BaseModel):
    """Only the fields the slide type needs are filled; the others stay empty."""

    display_title: str | None = None
    key_message: str | None = None
    body_points: list[str] = Field(default_factory=list)
    explanation: str | None = None
    example: str | None = None
    analogy: str | None = None
    practice_instruction: PracticeContent | None = None
    code_explanation: CodeExplanation | None = None
    visual_instruction: str | None = None  # an instruction for the renderer (Genspark), no image
    presenter_notes: str | None = None
    quiz_content: QuizContent | None = None
    summary_message: str | None = None


class OriginalContent(BaseModel):
    """The SlidePlanner's text for this slide (unchanged)."""

    key_message: str
    key_points: list[str] = Field(default_factory=list)
    source_reference: SourceLocation | None = None
    visual_instruction: str
    presenter_instruction: str


class GroundingInfo(BaseModel):
    source_references: list[SourceLocation] = Field(default_factory=list)
    grounded: bool = False  # the key message rests on the source (grounded / paraphrased)
    warnings: list[str] = Field(default_factory=list)  # what the GroundingValidator changed


class EnrichmentMetadata(BaseModel):
    """The profile values that shaped the wording of this slide."""

    audience_level: str
    difficulty: str
    lecture_type: str
    explanation_depth: str


class EnrichedSlide(BaseModel):
    # ---- structure: copied from the SlideSpecification, never written by the LLM
    slide_number: int = Field(ge=1)
    section_id: str
    title: str
    slide_type: SlideType
    learning_purpose: str
    estimated_explanation_time: int = Field(ge=1)  # seconds, unchanged

    original: OriginalContent
    enriched: EnrichedContent | None = None
    provenance: dict[str, FieldProvenance] = Field(default_factory=dict)
    grounding: GroundingInfo = Field(default_factory=GroundingInfo)
    metadata: EnrichmentMetadata

    status: SlideEnrichmentStatus
    failure_reason: str | None = None  # machine readable: timeout, invalid_json, ...
    failure_message: str | None = None  # readable (Korean), no stack trace
    from_cache: bool = False


# ---- specification-level records -------------------------------------------
class EnrichmentVersion(BaseModel):
    """Everything needed to reproduce / compare a result later."""

    enricher_version: str
    prompt_version: str
    model_provider: str
    model_name: str
    generation_timestamp: datetime
    source_hash: str
    lecture_profile_hash: str
    slide_spec_hash: str


class Rejection(BaseModel):
    """An LLM output that was refused (the affected slides use the original content)."""

    slide_numbers: list[int] = Field(default_factory=list)
    reason: str
    message: str


class EnrichmentValidation(BaseModel):
    """The checks run on the assembled result against the input SlideSpecification."""

    slide_count_same: bool = True
    slide_number_same: bool = True
    section_id_same: bool = True
    slide_type_same: bool = True
    duration_same: bool = True
    lecture_profile_same: bool = True
    source_policy_valid: bool = True
    no_structure_mutation: bool = True
    required_field_valid: bool = True
    schema_valid: bool = True
    errors: list[str] = Field(default_factory=list)
    rejections: list[Rejection] = Field(default_factory=list)

    @computed_field  # also part of the API / stored JSON, so clients need not re-derive it
    @property
    def passed(self) -> bool:
        return not self.errors and all(
            getattr(self, name)
            for name in (
                "slide_count_same", "slide_number_same", "section_id_same", "slide_type_same",
                "duration_same", "lecture_profile_same", "source_policy_valid",
                "no_structure_mutation", "required_field_valid", "schema_valid",
            )
        )


class EnrichmentStats(BaseModel):
    slide_count: int
    enriched_count: int
    failed_count: int
    adjusted_count: int = 0  # enriched slides where the validator removed/changed something
    llm_calls: int = 0
    cache_hits: int = 0
    batch_size: int = 1
    batch_count: int = 0
    failures_by_reason: dict[str, int] = Field(default_factory=dict)
    fields_by_provenance: dict[str, int] = Field(default_factory=dict)  # enriched fields per provenance


class EnrichedSlideSpecification(BaseModel):
    lecture_id: str  # == SlideSpecification.lecture_id
    project_id: str
    title: str
    duration_minutes: int
    slide_count: int
    status: RunStatus
    slides: list[EnrichedSlide]
    metadata: EnrichmentMetadata
    validation: EnrichmentValidation
    stats: EnrichmentStats
    version: EnrichmentVersion
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_invariants(self) -> "EnrichedSlideSpecification":
        if not self.slides:
            raise ValueError("an enriched specification needs at least one slide")
        if [s.slide_number for s in self.slides] != list(range(1, len(self.slides) + 1)):
            raise ValueError("slide numbers must run 1..n without gaps")
        if self.slide_count != len(self.slides):
            raise ValueError("slide_count does not match the slides")
        if sum(s.estimated_explanation_time for s in self.slides) != self.duration_minutes * 60:
            raise ValueError("slide times do not add up to the lecture duration")
        return self
