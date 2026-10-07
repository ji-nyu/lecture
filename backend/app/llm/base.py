"""Provider-independent contract between the SlideContentEnricher and any LLM.

    EnrichmentRequest  -> what one slide needs (only the relevant source context)
    LLMSlideOutput     <- what the LLM must answer (structured JSON, schema validated)
    ContentLLMClient   the interface: enrich_slide(...) / enrich_batch(...)

Nothing in the service code knows about OpenAI, HTTP or prompts. A provider is a
`ContentLLMClient` subclass (`JsonContentClient` adapts any JSON-completing LLM,
`MockLLMClient` is the offline test double). The output of an LLM is never trusted:
the enricher validates the schema and the GroundingValidator checks the content.

Named `ContentLLMClient` (not `LLMClient`) because `services.llm_client.LLMClient` is the
low-level "give me a JSON object" interface already used by the analysis stage.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models.enriched_slide_spec import (
    CONTENT_FIELDS,
    CodeExplanation,
    FieldProvenance,
    PracticeContent,
    QuizContent,
)
from ..models.source import SourceLocation
from ..services.llm_client import LLMError


# ---------------------------------------------------------------------------
# request (what goes to the LLM)
# ---------------------------------------------------------------------------
class SourceSnippet(BaseModel):
    """A piece of the source that is relevant to the slide (verbatim, never the whole document)."""

    kind: str  # definition | description | example | point | code | formula | objective
    text: str
    location: SourceLocation | None = None
    concept: str | None = None  # the slide concept this snippet is about


class SlideInput(BaseModel):
    slide_number: int
    section_id: str
    slide_type: str
    title: str
    learning_purpose: str
    key_message: str
    key_points: list[str]
    source_reference: SourceLocation | None = None
    concepts: list[str] = Field(default_factory=list)
    concept_relations: dict[str, list[str]] = Field(default_factory=dict)  # concept -> prerequisites
    estimated_explanation_time: int  # seconds
    content_origin: str
    previous_slide_title: str | None = None
    next_slide_title: str | None = None


class SectionInput(BaseModel):
    id: str
    title: str
    purpose: str
    kind: str
    concepts: list[str] = Field(default_factory=list)
    position: str  # "2/6"


class ProfileInput(BaseModel):
    audience_level: str
    difficulty: str
    lecture_type: str
    explanation_depth: str
    source_policy: str
    slide_density: str
    visual_level: str
    example_level: str
    practice_level: str
    code_level: str
    lecture_tone: str
    speaker_notes: str
    quiz_mode: str = "both"


class NotesBudget(BaseModel):
    """How much presenter text the slide may carry (from speaker_notes + its time)."""

    mode: str  # none | concise | full
    min_sentences: int = 0
    max_sentences: int = 0
    target_chars: int = 0
    max_chars: int = 0


class Limits(BaseModel):
    body_points_min: int
    body_points_max: int
    point_chars: int
    explanation_chars: int


class EnrichmentRequest(BaseModel):
    prompt_version: str
    lecture_title: str
    profile: ProfileInput
    slide: SlideInput
    section: SectionInput
    source_context: list[SourceSnippet] = Field(default_factory=list)
    scope_notes: list[str] = Field(default_factory=list)
    allowed_fields: list[str]
    limits: Limits
    notes: NotesBudget
    objectives: list[str] = Field(default_factory=list)  # only for title / agenda / summary slides


# ---------------------------------------------------------------------------
# response (what the LLM must return)
# ---------------------------------------------------------------------------
# Keys that would mean the LLM tries to change the structure of the lecture.
STRUCTURE_KEYS = (
    "slides", "new_slides", "added_slides", "deleted_slides", "removed_slides",
    "sections", "order", "reorder", "insert_after", "merge_with", "split_into",
)


class LLMSlideOutput(BaseModel):
    """Structured answer for one slide. Unknown keys are ignored (and reported)."""

    model_config = ConfigDict(extra="ignore")

    slide_number: int
    # Structural echoes are optional; when present they must equal the input (checked by the enricher).
    section_id: str | None = None
    slide_type: str | None = None
    estimated_explanation_time: int | None = None

    display_title: str | None = None
    key_message: str | None = None
    body_points: list[str] = Field(default_factory=list)
    explanation: str | None = None
    example: str | None = None
    analogy: str | None = None
    practice_instruction: PracticeContent | None = None
    code_explanation: CodeExplanation | None = None
    visual_instruction: str | None = None
    presenter_notes: str | None = None
    quiz_content: QuizContent | None = None
    summary_message: str | None = None

    provenance: dict[str, FieldProvenance | None] = Field(default_factory=dict)

    @field_validator("body_points", mode="before")
    @classmethod
    def _null_list(cls, v: Any) -> Any:
        return [] if v is None else v

    @field_validator("provenance", mode="before")
    @classmethod
    def _known_keys(cls, v: Any) -> Any:
        if v is None:
            return {}
        if isinstance(v, dict):  # unknown field names are dropped, not fatal
            return {k: val for k, val in v.items() if k in CONTENT_FIELDS}
        return v


KNOWN_OUTPUT_KEYS = set(LLMSlideOutput.model_fields)


# ---------------------------------------------------------------------------
# client interface
# ---------------------------------------------------------------------------
@dataclass
class SlideResult:
    """The answer for one requested slide: a raw JSON object, or the error that prevented it."""

    slide_number: int
    output: dict[str, Any] | None = None
    error: LLMError | None = None


class ContentLLMClient(ABC):
    provider: str = "unknown"
    model: str = "unknown"

    @abstractmethod
    def enrich_slide(self, request: EnrichmentRequest) -> dict[str, Any]:
        """Return the structured answer (a JSON object) for one slide.

        Raise `LLMError` subclasses (timeout, rate limit, invalid response, ...) on failure;
        never leak provider exceptions or credentials.
        """

    def enrich_batch(self, requests: list[EnrichmentRequest]) -> list[SlideResult]:
        """Answer several slides. The default asks one by one (a failing slide does not
        affect the others). Providers that can answer several slides per call override this.

        The order of the returned list carries no meaning: results are matched by slide number
        and the caller restores slide order.
        """
        results: list[SlideResult] = []
        for req in requests:
            try:
                results.append(SlideResult(req.slide.slide_number, output=self.enrich_slide(req)))
            except LLMError as exc:
                results.append(SlideResult(req.slide.slide_number, error=exc))
        return results
