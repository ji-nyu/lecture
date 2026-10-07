"""PresentationPrompt (STAGE 6B): the text handed to a presentation provider.

    LectureProfile + LecturePlan + SlideSpecification (+ the enriched content, + the source)
        -> GensparkPromptBuilder -> final_prompt

The prompt is built by rules only (no LLM, no network) and contains no time stamp, so the same
inputs always give byte-identical text (`prompt_hash`). Everything that describes HOW it was built
(which content it used, which slides fell back, the checks it passed) lives next to the text in
this record and never inside it.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, computed_field


class PromptContentSource(str, Enum):
    enriched = "enriched"  # STAGE 6A content (slides that failed enrichment use the rule-based text)
    rule_based = "rule_based"  # the SlidePlanner's own text only (no enrichment was available)


class PromptSectionInfo(BaseModel):
    name: str  # one of the section names the spec asks for
    char_count: int


class PromptSlideInfo(BaseModel):
    slide_number: int
    title: str
    slide_type: str
    content: str  # enriched | rule_based | rule_based_fallback | placeholder
    char_count: int


class PromptValidation(BaseModel):
    """Checks run on the finished text against the inputs. A failed check blocks storing it."""

    all_sections_present: bool = True  # the 13 sections of the spec, in the spec's order
    slide_count_matches: bool = True  # the prompt states the real slide count
    all_slides_present_once_in_order: bool = True
    slide_titles_present: bool = True  # every slide title/number/time of the specification appears
    duration_matches: bool = True  # slide seconds add up to the lecture duration
    no_empty_slide_block: bool = True
    no_leftovers: bool = True  # no "None", "{...}" or other template residue
    errors: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def passed(self) -> bool:
        return not self.errors and all(
            getattr(self, n)
            for n in (
                "all_sections_present", "slide_count_matches", "all_slides_present_once_in_order",
                "slide_titles_present", "duration_matches", "no_empty_slide_block", "no_leftovers",
            )
        )


class PromptVersion(BaseModel):
    """Enough to tell later whether a stored prompt still matches its inputs."""

    builder_version: str
    generated_at: datetime
    slide_spec_hash: str
    lecture_profile_hash: str
    enrichment_prompt_version: str | None = None  # the STAGE 6A prompt version, when enriched content was used
    enrichment_model: str | None = None


class PresentationPrompt(BaseModel):
    project_id: str
    lecture_id: str
    title: str
    final_prompt: str
    prompt_hash: str  # sha256 of final_prompt
    char_count: int
    line_count: int

    content_source: PromptContentSource
    enrichment_status: str | None = None  # RunStatus of the enrichment used, if any
    slide_count: int
    section_names: list[str]
    sections: list[PromptSectionInfo]
    slides: list[PromptSlideInfo]
    fallback_slide_numbers: list[int] = Field(default_factory=list)  # enrichment failed -> rule-based text
    placeholder_slide_numbers: list[int] = Field(default_factory=list)  # no text at all (source has nothing)
    code_slide_numbers_without_code: list[int] = Field(default_factory=list)

    options: dict[str, str]  # the profile values that shaped the wording (for inspection)
    validation: PromptValidation
    warnings: list[str] = Field(default_factory=list)
    version: PromptVersion
