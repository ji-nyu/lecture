"""PlanningPolicy: how a LectureProfile changes the lecture (STAGE 3).

Every option -> plan rule lives here, in one place, so the effect of each option
is explicit, testable and easy to tune. The LecturePlanner only reads this policy.

Option        -> effect (spec rules in brackets)
-------------------------------------------------------------------------------
audience      beginner-type: define every term at first use, add a prerequisite
              section, examples first for hard concepts, slower pace [Audience_01]
              expert-type (professional/graduate/advanced): far fewer basic
              definitions, favour internals/architecture/process, industry cases [Audience_02]
duration      time budget -> how many concepts fit, number of sections
difficulty    introductory: more/easier examples, no formulas, definitions [Difficulty_01]
              advanced: principles + limits/trade-off section [Difficulty_02]
lecture_type  theory: definitions/concepts/principles up, practice down [Type_01]
              practice: practice blocks + step-by-step + code [Type_02]
              exam_preparation: comparison table, common mistakes, quizzes [Type_03]
explanation_depth  time and slides per concept, concepts per section, share of defined terms
source_policy source_only: nothing that is not in the source (no `suggested` slots)
              source_first / expanded: `suggested` slots for missing examples/code/terms
example_level share of concepts that get an example
practice_level share of time for practice, blocks, activities, steps
code_level    source code attached to sections / executed in practice
quiz_mode     checkpoint quiz in sections, final quiz section
visual_level  extra diagram slides (visual=high)
slide_density slides per concept
lecture_tone, speaker_notes  carried to the SlidePlanner via `presentation_hints` (STAGE 4)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.lecture_profile import (
    AudienceLevel,
    CodeLevel,
    Difficulty,
    ExampleLevel,
    ExplanationDepth,
    LectureProfile,
    LectureType,
    PracticeLevel,
    QuizMode,
    SlideDensity,
    SourcePolicy,
    VisualLevel,
)
from ..models.source import ConceptCategory as C

BEGINNER_AUDIENCES = (
    AudienceLevel.general,
    AudienceLevel.high_school,
    AudienceLevel.university_beginner,
)
EXPERT_AUDIENCES = (
    AudienceLevel.professional,
    AudienceLevel.graduate,
    AudienceLevel.university_advanced,
)

# Share of the taught concepts (most important first) whose term gets an explicit
# definition at first use = audience base + depth + type + difficulty, clamped to 0..1.
_AUDIENCE_DEFINE_BASE = {
    AudienceLevel.general: 1.0,
    AudienceLevel.high_school: 1.0,
    AudienceLevel.university_beginner: 1.0,
    AudienceLevel.university_intermediate: 0.75,
    AudienceLevel.university_advanced: 0.5,
    AudienceLevel.graduate: 0.5,
    AudienceLevel.professional: 0.25,
}
_DEPTH_DEFINE_ADJ = {ExplanationDepth.concise: -0.25, ExplanationDepth.standard: 0.0, ExplanationDepth.detailed: 0.25}
_TYPE_DEFINE_ADJ = {
    LectureType.theory: 0.25,
    LectureType.example_based: 0.0,
    LectureType.mixed: 0.0,
    LectureType.exam_preparation: 0.0,
    LectureType.practice: -0.25,
}
_DIFF_DEFINE_ADJ = {
    Difficulty.introductory: 0.25,
    Difficulty.beginner: 0.0,
    Difficulty.intermediate: 0.0,
    Difficulty.advanced: -0.25,
}
_CONCEPT_SECONDS = {ExplanationDepth.concise: 90, ExplanationDepth.standard: 140, ExplanationDepth.detailed: 200}
_TYPE_PACE = {
    LectureType.theory: 1.0,
    LectureType.example_based: 1.0,
    LectureType.mixed: 1.0,
    LectureType.exam_preparation: 0.85,
    LectureType.practice: 0.85,
}
_MAX_PER_SECTION = {ExplanationDepth.concise: 6, ExplanationDepth.standard: 4, ExplanationDepth.detailed: 3}
_SLIDES_PER_CONCEPT = {ExplanationDepth.concise: 0.6, ExplanationDepth.standard: 1.0, ExplanationDepth.detailed: 1.6}
_DENSITY_FACTOR = {SlideDensity.concise: 0.85, SlideDensity.normal: 1.0, SlideDensity.detailed: 1.25}
_EXAMPLE_RATIO = {ExampleLevel.none: 0.0, ExampleLevel.low: 0.25, ExampleLevel.medium: 0.5, ExampleLevel.high: 1.0}
_PRACTICE_SHARE = {PracticeLevel.none: 0.0, PracticeLevel.simple: 0.12, PracticeLevel.guided: 0.28, PracticeLevel.full: 0.45}
_PRACTICE_BLOCKS = {PracticeLevel.none: 0, PracticeLevel.simple: 1, PracticeLevel.guided: 2, PracticeLevel.full: 3}
_PRACTICE_ACTIVITIES = {PracticeLevel.none: 0, PracticeLevel.simple: 1, PracticeLevel.guided: 2, PracticeLevel.full: 2}
_PRACTICE_STEPS = {PracticeLevel.none: 0, PracticeLevel.simple: 2, PracticeLevel.guided: 4, PracticeLevel.full: 6}


@dataclass(frozen=True)
class PlanningPolicy:
    basics_first: bool
    expert: bool

    # ---- time
    intro_share: float
    summary_share: float
    final_quiz_share: float
    practice_share: float
    concept_seconds: float
    definition_seconds: float
    example_seconds: float
    code_seconds: float
    checkpoint_seconds: float

    # ---- content selection
    definition_ratio: float  # share of taught concepts that get a definition (0..1)
    example_ratio: float
    example_first: bool
    industry_examples: bool
    max_concepts_per_section: int
    min_concepts: int
    category_bias: dict = field(default_factory=dict)

    # ---- slides
    slides_per_concept: float = 1.0
    diagram_slides: bool = False

    # ---- practice / code / quiz
    practice_blocks: int = 0
    activities_per_block: int = 0
    steps_per_activity: int = 0
    code_level: CodeLevel = CodeLevel.none
    checkpoint_quiz: bool = False
    final_quiz: bool = False

    # ---- optional sections
    prerequisite_section: bool = False
    comparison_section: bool = False
    caution_section: bool = False
    caution_title: str = ""

    # ---- source policy / objectives
    allow_suggested: bool = False
    objective_style: str = "understand"


def _category_bias(p: LectureProfile, basics_first: bool, expert: bool) -> dict:
    """Score offsets that decide WHICH concepts win when time is short."""
    bias: dict = {c: 0.0 for c in C}

    def add(cat, v):
        bias[cat] += v

    if basics_first:
        add(C.definition, 0.5), add(C.principle, 0.5)
    if expert:
        add(C.process, 1.0), add(C.architecture, 1.0), add(C.caution, 0.5), add(C.code, 0.5)
        add(C.definition, -1.0), add(C.summary, -1.0), add(C.example, -0.5)
    if p.difficulty == Difficulty.introductory:
        add(C.definition, 0.5), add(C.principle, 0.5), add(C.formula, -1.5)  # minimise formula work
    if p.difficulty == Difficulty.advanced:
        add(C.process, 0.5), add(C.architecture, 0.5), add(C.principle, 0.5)
    t = p.lecture_type
    if t == LectureType.theory:
        add(C.definition, 0.5), add(C.principle, 0.5)
    elif t == LectureType.practice:
        add(C.process, 0.5), add(C.practice, 1.0), add(C.code, 1.0), add(C.definition, -0.5)
    elif t == LectureType.exam_preparation:
        add(C.caution, 1.0), add(C.definition, 0.5)
    elif t == LectureType.example_based:
        add(C.example, 0.5)
    return bias


def derive_policy(p: LectureProfile) -> PlanningPolicy:
    basics_first = p.audience_level in BEGINNER_AUDIENCES
    expert = p.audience_level in EXPERT_AUDIENCES

    define_ratio = (
        _AUDIENCE_DEFINE_BASE[p.audience_level]
        + _DEPTH_DEFINE_ADJ[p.explanation_depth]
        + _TYPE_DEFINE_ADJ[p.lecture_type]
        + _DIFF_DEFINE_ADJ[p.difficulty]
    )
    define_ratio = round(max(0.0, min(1.0, define_ratio)), 2)

    pace = 1.1 if basics_first else 0.85 if expert else 1.0
    example_ratio = _EXAMPLE_RATIO[p.example_level]
    if p.example_level != ExampleLevel.none and p.difficulty in (Difficulty.introductory, Difficulty.beginner):
        example_ratio = min(1.0, example_ratio + 0.15)  # easier examples: more of them

    caution = p.lecture_type == LectureType.exam_preparation or p.difficulty == Difficulty.advanced
    if p.lecture_type == LectureType.exam_preparation and p.difficulty == Difficulty.advanced:
        caution_title = "한계·trade-off와 흔한 오류"
    elif p.lecture_type == LectureType.exam_preparation:
        caution_title = "흔한 오류와 주의점"
    else:
        caution_title = "기술적 한계와 trade-off"

    style = (
        "exam" if p.lecture_type == LectureType.exam_preparation
        else "perform" if p.lecture_type == LectureType.practice
        else "analyze" if (expert or p.difficulty == Difficulty.advanced)
        else "understand" if (basics_first or p.difficulty == Difficulty.introductory)
        else "apply"
    )

    return PlanningPolicy(
        basics_first=basics_first,
        expert=expert,
        intro_share=0.05,
        summary_share=0.06,
        final_quiz_share=0.07 if p.quiz_mode in (QuizMode.final, QuizMode.both) else 0.0,
        practice_share=_PRACTICE_SHARE[p.practice_level],
        concept_seconds=_CONCEPT_SECONDS[p.explanation_depth] * _TYPE_PACE[p.lecture_type] * pace,
        definition_seconds=45,
        example_seconds=90 * (1.3 if basics_first else 1.0),
        code_seconds=120 if p.code_level == CodeLevel.executable else 90,
        checkpoint_seconds=60,
        definition_ratio=define_ratio,
        example_ratio=example_ratio,
        example_first=basics_first,
        industry_examples=expert,
        max_concepts_per_section=_MAX_PER_SECTION[p.explanation_depth],
        min_concepts=3,
        category_bias=_category_bias(p, basics_first, expert),
        slides_per_concept=_SLIDES_PER_CONCEPT[p.explanation_depth] * _DENSITY_FACTOR[p.slide_density],
        diagram_slides=p.visual_level == VisualLevel.high,
        practice_blocks=_PRACTICE_BLOCKS[p.practice_level],
        activities_per_block=_PRACTICE_ACTIVITIES[p.practice_level],
        steps_per_activity=_PRACTICE_STEPS[p.practice_level],
        code_level=p.code_level,
        checkpoint_quiz=p.quiz_mode in (QuizMode.checkpoint, QuizMode.both),
        final_quiz=p.quiz_mode in (QuizMode.final, QuizMode.both),
        prerequisite_section=basics_first,
        comparison_section=p.lecture_type == LectureType.exam_preparation,
        caution_section=caution,
        caution_title=caution_title,
        allow_suggested=p.source_policy != SourcePolicy.source_only,
        objective_style=style,
    )
