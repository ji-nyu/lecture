"""Build the per-slide LLM request (STAGE 6A): only the context the slide needs.

Sending the whole document to the LLM costs tokens and invites hallucination, so each
request carries

  * the LectureProfile values that shape the wording,
  * the slide (title, type, purpose, key message, key points, source reference),
  * the section it belongs to,
  * the few source snippets (definition, description, example, code, ...) about the
    slide's own concepts - verbatim source text with locations,
  * the scope notes of the source ("not covered in this lecture", ...),
  * which fields to write (`allowed_fields`), how many points, and how long the
    presenter notes may be (from `speaker_notes` and the slide's time).

Deterministic: the same input always gives the same request (it is part of the cache key).
"""

from __future__ import annotations

from ..llm.base import (
    EnrichmentRequest,
    Limits,
    NotesBudget,
    ProfileInput,
    SectionInput,
    SlideInput,
    SourceSnippet,
)
from ..models.lecture_plan import LecturePlan, LectureSection
from ..models.lecture_profile import LectureProfile
from ..models.slide_spec import Slide, SlideSpecification
from ..models.source import SourceAnalysis
from .planning_policy import BEGINNER_AUDIENCES
from .text_utils import clip_chars, normalize, violates_scope

CHARS_PER_SECOND = 5.0  # ~300 characters of spoken Korean per minute
MAX_CONCEPTS = 6
MAX_SNIPPETS = 8
MAX_CONTEXT_CHARS = 1800
SNIPPET_CHARS = 360
CODE_CHARS = 600

BODY_POINTS = {"concise": (2, 3), "normal": (3, 5), "detailed": (4, 7)}
POINT_CHARS = {"concise": 90, "normal": 120, "detailed": 160}
EXPLANATION_CHARS = {"concise": 200, "standard": 380, "detailed": 600}

_CORE = {  # fields each slide type is about (spec: SLIDE TYPE RULES)
    "title": [],
    "agenda": ["body_points"],
    "definition": ["explanation"],
    "concept": ["body_points", "explanation"],
    "comparison": ["body_points", "explanation"],
    "architecture": ["body_points", "explanation"],
    "diagram": ["body_points", "explanation"],
    "workflow": ["body_points", "explanation"],
    "example": ["example", "explanation"],
    "practice": ["practice_instruction"],
    "code": ["code_explanation"],
    "formula": ["body_points", "explanation"],
    "quiz": ["quiz_content"],
    "summary": ["body_points", "summary_message"],
}
_EXAMPLE_TYPES = {"definition", "concept", "architecture", "diagram", "workflow", "comparison"}
_ANALOGY_TYPES = {"definition", "concept", "architecture", "diagram", "workflow"}


def core_fields(slide_type: str) -> list[str]:
    return list(_CORE.get(slide_type, ["body_points", "explanation"]))


def is_beginner(profile: LectureProfile) -> bool:
    return profile.audience_level in BEGINNER_AUDIENCES or profile.difficulty == "introductory"


def allowed_fields(slide_type: str, profile: LectureProfile) -> list[str]:
    """The content fields that make sense for this slide type AND this lecture profile."""
    fields = ["display_title", "key_message", *core_fields(slide_type)]
    wants_example = profile.example_level in ("medium", "high") or profile.lecture_type == "example_based"
    if slide_type in _EXAMPLE_TYPES and (wants_example or (slide_type == "definition" and is_beginner(profile))):
        fields.append("example")
    if slide_type in _ANALOGY_TYPES and is_beginner(profile):
        fields.append("analogy")
    fields.append("visual_instruction")
    if profile.speaker_notes != "none":
        fields.append("presenter_notes")
    return fields


def notes_budget(mode: str, seconds: int, slide_type: str) -> NotesBudget:
    """Presenter-notes size from the speaker_notes option and the time of the slide.

    On a practice slide most of the time is hands-on work, so only ~40% of it is spoken."""
    if mode == "none":
        return NotesBudget(mode="none")
    share = 0.4 if slide_type == "practice" else 1.0
    spoken = int(seconds * CHARS_PER_SECOND * share)
    if mode == "concise":
        max_chars = max(90, min(spoken, 400))
        return NotesBudget(mode=mode, min_sentences=2, max_sentences=4, target_chars=int(max_chars * 0.7), max_chars=max_chars)
    max_chars = max(120, spoken)
    target = int(max_chars * 0.8)
    return NotesBudget(
        mode=mode,
        min_sentences=2 if seconds < 30 else 3,
        max_sentences=max(3, target // 30),
        target_chars=target,
        max_chars=max_chars,
    )


def scope_notes_of(plan: LecturePlan, analysis: SourceAnalysis | None) -> list[str]:
    seen: dict[str, None] = {}
    for c in plan.source_constraints:
        seen[normalize(c.text)] = None
    if analysis:
        for n in analysis.scope_notes:
            seen[normalize(n.text)] = None
    return list(seen)[:10]


class _Index:
    def __init__(self, analysis: SourceAnalysis | None):
        self.analysis = analysis
        self.concepts = {}
        self.definitions = {}
        if analysis is None:
            return
        for c in analysis.concepts:
            self.concepts[c.name.lower()] = c
            for a in c.aliases:
                self.concepts.setdefault(a.lower(), c)
        for d in analysis.definitions:
            self.definitions.setdefault(d.term.lower(), d)

    def concept(self, name: str):
        return self.concepts.get(name.lower())

    def definition(self, name: str):
        d = self.definitions.get(name.lower())
        if d is None:
            c = self.concept(name)
            if c is not None:
                d = self.definitions.get(c.name.lower())
        return d


def _snippets_for(
    slide: Slide,
    section: LectureSection,
    idx: _Index,
    scope: list[str],
) -> list[SourceSnippet]:
    a = idx.analysis
    if a is None or slide.slide_type.value == "title":
        return []
    stype = slide.slide_type.value
    names = list(dict.fromkeys(slide.concepts or section.concepts))[:MAX_CONCEPTS]
    compact = stype in ("summary", "quiz", "agenda")
    out: list[SourceSnippet] = []
    seen: set[str] = set()

    def add(kind: str, text: str, loc, limit: int = SNIPPET_CHARS, concept: str | None = None) -> None:
        text = normalize(text)
        if not text or text in seen or violates_scope(text, scope):
            return
        seen.add(text)
        out.append(SourceSnippet(kind=kind, text=clip_chars(text, limit), location=loc, concept=concept))

    for name in names:
        c = idx.concept(name)
        d = idx.definition(name)
        if d is not None:
            add("definition", d.definition, d.source_location, concept=name)
        if c is not None and (not compact or d is None):
            add("description", c.description, c.source_location, concept=name)
        if compact:
            continue
        cname = c.name if c is not None else name
        limit = 3 if stype == "example" else 1
        n = 0
        for e in a.examples:
            if cname in e.related_concepts or name in e.related_concepts:
                add("example", e.text, e.source_location, concept=name)
                n += 1
                if n >= limit:
                    break
        for p in a.important_points:
            if name.lower() in p.text.lower():
                add("point", p.text, p.source_location, concept=name)
                break
    if stype in ("code", "practice") or section.code:
        sec_ids = {slide.source_reference.section_id if slide.source_reference else None, *section.source_section_ids}
        n = 0
        for cb in a.code_examples:
            if cb.section_id in sec_ids and n < 2:
                add("code", cb.code, cb.location, CODE_CHARS)
                n += 1
    if stype == "formula":
        for f in a.formulas[:2]:
            add("formula", f.expression, f.location)

    # keep the request small: cap the number and the total size of the snippets
    kept: list[SourceSnippet] = []
    total = 0
    for s in out[:MAX_SNIPPETS]:
        if total + len(s.text) > MAX_CONTEXT_CHARS:
            break
        kept.append(s)
        total += len(s.text)
    return kept


def build_requests(
    spec: SlideSpecification,
    plan: LecturePlan,
    profile: LectureProfile,
    analysis: SourceAnalysis | None,
    prompt_version: str,
) -> list[EnrichmentRequest]:
    idx = _Index(analysis)
    scope = scope_notes_of(plan, analysis)
    sections = {s.id: s for s in plan.sections}
    order = {s.id: i + 1 for i, s in enumerate(plan.sections)}
    dens = profile.slide_density.value if hasattr(profile.slide_density, "value") else str(profile.slide_density)
    depth = profile.explanation_depth.value
    lo, hi = BODY_POINTS.get(dens, (3, 5))
    limits = Limits(
        body_points_min=lo, body_points_max=hi,
        point_chars=POINT_CHARS.get(dens, 120),
        explanation_chars=EXPLANATION_CHARS.get(depth, 380),
    )
    prof = ProfileInput(
        audience_level=profile.audience_level.value,
        difficulty=profile.difficulty.value,
        lecture_type=profile.lecture_type.value,
        explanation_depth=depth,
        source_policy=profile.source_policy.value,
        slide_density=dens,
        visual_level=profile.visual_level.value,
        example_level=profile.example_level.value,
        practice_level=profile.practice_level.value,
        code_level=profile.code_level.value,
        lecture_tone=profile.lecture_tone.value,
        speaker_notes=profile.speaker_notes.value,
        quiz_mode=profile.quiz_mode.value,
    )
    requests: list[EnrichmentRequest] = []
    slides = spec.slides
    for i, s in enumerate(slides):
        sec = sections.get(s.section_id)
        if sec is None:  # cannot happen for a spec built from this plan
            raise ValueError(f"slide {s.slide_number} refers to an unknown section")
        stype = s.slide_type.value
        relations = {}
        for n in s.concepts[:MAX_CONCEPTS]:
            c = idx.concept(n)
            if c is not None and c.prerequisite:
                relations[n] = list(c.prerequisite)[:4]
        needs_objectives = stype in ("title", "agenda", "summary")
        requests.append(
            EnrichmentRequest(
                prompt_version=prompt_version,
                lecture_title=spec.title,
                profile=prof,
                slide=SlideInput(
                    slide_number=s.slide_number,
                    section_id=s.section_id,
                    slide_type=stype,
                    title=s.title,
                    learning_purpose=s.learning_purpose,
                    key_message=s.key_message,
                    key_points=list(s.key_points),
                    source_reference=s.source_reference,
                    concepts=list(s.concepts),
                    concept_relations=relations,
                    estimated_explanation_time=s.estimated_explanation_time,
                    content_origin=s.content_origin.value,
                    previous_slide_title=slides[i - 1].title if i > 0 else None,
                    next_slide_title=slides[i + 1].title if i + 1 < len(slides) else None,
                ),
                section=SectionInput(
                    id=sec.id, title=sec.title, purpose=sec.purpose, kind=sec.kind.value,
                    concepts=list(sec.concepts), position=f"{order[sec.id]}/{len(plan.sections)}",
                ),
                source_context=_snippets_for(s, sec, idx, scope),
                scope_notes=scope,
                allowed_fields=allowed_fields(stype, profile),
                limits=limits,
                notes=notes_budget(profile.speaker_notes.value, s.estimated_explanation_time, stype),
                objectives=list(plan.learning_objectives[:5]) if needs_objectives else [],
            )
        )
    return requests
