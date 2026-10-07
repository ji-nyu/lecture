"""Source material (parser output) and source analysis (analyzer output) schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .lecture_profile import Difficulty

# Formats listed in the spec (PDF, PPT, PPTX, DOCX, TXT, Markdown).
ACCEPTED_EXTENSIONS = (".pdf", ".ppt", ".pptx", ".docx", ".txt", ".md", ".markdown")


class SourceFileInfo(BaseModel):
    filename: str
    extension: str
    size_bytes: int
    content_type: str | None = None
    uploaded_at: datetime


# --------------------------------------------------------------------------
# SourceMaterial (DocumentParser output)
# --------------------------------------------------------------------------
class SourceLocation(BaseModel):
    """Where something was found in the source document."""

    section_id: str | None = None
    section_title: str | None = None
    line: int | None = None  # txt / md
    page: int | None = None  # pdf page or pptx slide number


class ContentBlock(BaseModel):
    kind: str  # paragraph | list_item | note
    text: str
    line: int | None = None
    page: int | None = None


class Heading(BaseModel):
    level: int
    text: str
    section_id: str
    line: int | None = None
    page: int | None = None


class SourceSection(BaseModel):
    id: str
    order: int
    title: str
    level: int
    parent_id: str | None = None
    text: str = ""
    blocks: list[ContentBlock] = Field(default_factory=list)
    line: int | None = None
    page: int | None = None
    char_count: int = 0


class TableData(BaseModel):
    section_id: str | None = None
    rows: list[list[str]]
    location: SourceLocation | None = None


class CodeBlock(BaseModel):
    section_id: str | None = None
    language: str | None = None
    code: str
    location: SourceLocation | None = None


class Formula(BaseModel):
    section_id: str | None = None
    expression: str
    location: SourceLocation | None = None


class ImageMeta(BaseModel):
    section_id: str | None = None
    name: str
    page: int | None = None
    alt_text: str | None = None


class SourceMaterial(BaseModel):
    id: str
    filename: str
    title: str
    file_type: str
    raw_text: str
    headings: list[Heading] = Field(default_factory=list)
    sections: list[SourceSection] = Field(default_factory=list)
    tables: list[TableData] = Field(default_factory=list)
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    formulas: list[Formula] = Field(default_factory=list)
    images_metadata: list[ImageMeta] = Field(default_factory=list)
    page_count: int | None = None
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# SourceAnalysis (LectureAnalyzer output)
# --------------------------------------------------------------------------
class ConceptCategory(str, Enum):
    definition = "definition"
    principle = "principle"
    architecture = "architecture"
    process = "process"
    example = "example"
    practice = "practice"
    code = "code"
    formula = "formula"
    caution = "caution"
    summary = "summary"


class Provenance(str, Enum):
    """Where a piece of analysis came from."""

    heuristic = "heuristic"  # rule based
    llm = "llm"  # extracted by the LLM and verified against the source
    heuristic_llm = "heuristic+llm"  # both found it
    llm_inference = "llm_inference"  # semantic inference, not stated in the source


class Evidence(BaseModel):
    """A verbatim span of the source that backs an analysis entity.

    `quote` is always text taken FROM the source (never the LLM's own wording).
    `match` records how an LLM quote was located: exact | normalized | fuzzy.
    """

    quote: str
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    section_id: str | None = None
    match: str = "exact"
    match_score: float = 1.0


class Provenanced(BaseModel):
    """Traceability metadata shared by the analysis entities (all optional/defaulted,
    so older stored analyses and existing API consumers keep working)."""

    provenance: Provenance = Provenance.heuristic
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    inferred: bool = False  # True when not directly stated in the source
    evidence: Evidence | None = None


class PrerequisiteLink(BaseModel):
    """Provenance of one entry of `Concept.prerequisite`."""

    name: str
    provenance: Provenance
    inferred: bool = True
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: Evidence | None = None  # quote supporting the link, when one was found


class Concept(Provenanced):
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str
    importance: int = Field(ge=1, le=5)  # 5 = most important
    prerequisite: list[str] = Field(default_factory=list)
    prerequisite_links: list[PrerequisiteLink] = Field(default_factory=list)
    source_location: SourceLocation
    category: ConceptCategory
    mention_count: int = 0


class Definition(Provenanced):
    term: str
    definition: str
    source_location: SourceLocation


class ExampleItem(Provenanced):
    kind: str  # inline_example | case_study
    text: str
    related_concepts: list[str] = Field(default_factory=list)
    source_location: SourceLocation


class ImportantPoint(Provenanced):
    text: str
    reason: str
    source_location: SourceLocation


class ScopeNote(Provenanced):
    """Statements in the source that constrain HOW/HOW FAR to teach something."""

    text: str
    source_location: SourceLocation


class MainTopic(Provenanced):
    title: str
    summary: str
    section_ids: list[str]
    key_concepts: list[str]
    heuristic_title: str | None = None  # kept when an LLM title replaced it


class SectionInfo(BaseModel):
    id: str
    order: int
    title: str
    level: int
    summary: str
    concepts: list[str] = Field(default_factory=list)
    char_count: int = 0
    has_definition: bool = False
    has_example: bool = False


class HierarchyNode(BaseModel):
    id: str
    title: str
    level: int
    children: list["HierarchyNode"] = Field(default_factory=list)


class Complexity(BaseModel):
    level: Difficulty
    score: float
    factors: dict[str, float] = Field(default_factory=dict)


class AnalyzerInfo(BaseModel):
    """How this analysis was produced (incl. whether an LLM fallback happened)."""

    mode_requested: str = "heuristic"  # heuristic | hybrid | llm
    mode_used: str = "heuristic"
    fallback: bool = False
    fallback_reason: str | None = None  # not_configured | timeout | error | invalid_response | ...
    llm_model: str | None = None
    llm_calls: int = 0
    discarded: dict[str, int] = Field(default_factory=dict)  # LLM outputs rejected, by reason


class SourceAnalysis(BaseModel):
    source_id: str
    title: str
    summary: str
    main_topics: list[MainTopic]
    concepts: list[Concept]
    definitions: list[Definition]
    examples: list[ExampleItem]
    formulas: list[Formula]
    code_examples: list[CodeBlock]
    sections: list[SectionInfo]
    section_hierarchy: list[HierarchyNode]
    important_points: list[ImportantPoint]
    scope_notes: list[ScopeNote]
    estimated_complexity: Complexity
    analyzer: str = "heuristic-v1"  # heuristic-v1 | hybrid-v1 | llm-v1
    analyzer_info: AnalyzerInfo | None = None
    generated_at: datetime
    warnings: list[str] = Field(default_factory=list)
