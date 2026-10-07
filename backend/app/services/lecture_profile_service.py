"""LectureProfileService.

Turns instructor options into a structured LectureProfile:
  * structures the options and applies defaults
  * presets (ConceptFocused / PracticeFocused / Balanced / ExamPreparation)
  * validates option combinations (hard conflicts -> InvalidLectureProfile,
    soft ones -> `LectureProfile.warnings`)

Precedence for every advanced option (highest first):
    1. the value the instructor set explicitly
    2. the value defined by the selected preset
    3. the value inferred from the six basic options
`inferred_fields` lists what was inferred, `preset_fields` what came from the preset.

A preset also defines two BASIC options (lecture_type, explanation_depth). Basic
options are required and always explicit, so they are never overwritten; if they
differ from the preset a warning says so.
"""

from __future__ import annotations

import uuid

from ..errors import InvalidLectureProfile
from ..models.lecture_profile import (
    ADVANCED_OPTION_FIELDS,
    AudienceLevel,
    CodeLevel,
    Difficulty,
    ExampleLevel,
    ExplanationDepth,
    LectureOptionsInput,
    LecturePreset,
    LectureProfile,
    LectureTone,
    LectureType,
    PracticeLevel,
    QuizMode,
    SlideDensity,
    SpeakerNotes,
    VideoIntro,
    VisualLevel,
)
from ..storage.project_store import utcnow

# ---------------------------------------------------------------------------
# Presets (values from MASTER_SPEC "ONTOLOGY: PRESETS")
# ---------------------------------------------------------------------------
PRESETS: dict[LecturePreset, dict[str, object]] = {
    LecturePreset.concept_focused: {
        "lecture_type": LectureType.theory,
        "explanation_depth": ExplanationDepth.detailed,
        "example_level": ExampleLevel.medium,
        "practice_level": PracticeLevel.none,
        "visual_level": VisualLevel.medium,
    },
    LecturePreset.practice_focused: {
        "lecture_type": LectureType.practice,
        "explanation_depth": ExplanationDepth.standard,
        "example_level": ExampleLevel.medium,
        "practice_level": PracticeLevel.full,
        "code_level": CodeLevel.executable,
    },
    LecturePreset.balanced: {
        "lecture_type": LectureType.mixed,
        "explanation_depth": ExplanationDepth.standard,
        "example_level": ExampleLevel.medium,
        "practice_level": PracticeLevel.guided,
    },
    LecturePreset.exam_preparation: {
        "lecture_type": LectureType.exam_preparation,
        "explanation_depth": ExplanationDepth.concise,
        "example_level": ExampleLevel.high,
        "quiz_mode": QuizMode.both,
    },
}
_PRESET_BASIC = ("lecture_type", "explanation_depth")


def preset_schema() -> dict[str, dict[str, dict[str, str]]]:
    """JSON-friendly preset definitions for the UI: {name: {basic: {...}, advanced: {...}}}."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    for name, values in PRESETS.items():
        out[name.value] = {
            "basic": {k: v.value for k, v in values.items() if k in _PRESET_BASIC},
            "advanced": {k: v.value for k, v in values.items() if k not in _PRESET_BASIC},
        }
    return out


# ---------------------------------------------------------------------------
# Inference of advanced options from the basic ones
# ---------------------------------------------------------------------------
_PRACTICE_BY_TYPE = {
    LectureType.theory: PracticeLevel.none,
    LectureType.example_based: PracticeLevel.simple,
    LectureType.practice: PracticeLevel.full,
    LectureType.mixed: PracticeLevel.guided,
    LectureType.exam_preparation: PracticeLevel.none,
}
_EXAMPLE_BY_TYPE = {
    LectureType.theory: ExampleLevel.medium,
    LectureType.example_based: ExampleLevel.high,
    LectureType.practice: ExampleLevel.medium,
    LectureType.mixed: ExampleLevel.medium,
    LectureType.exam_preparation: ExampleLevel.high,
}
_CODE_BY_TYPE = {
    LectureType.theory: CodeLevel.none,
    LectureType.example_based: CodeLevel.snippet,
    LectureType.practice: CodeLevel.executable,
    LectureType.mixed: CodeLevel.snippet,
    LectureType.exam_preparation: CodeLevel.none,
}
_QUIZ_BY_TYPE = {
    LectureType.theory: QuizMode.none,
    LectureType.example_based: QuizMode.none,
    LectureType.practice: QuizMode.none,
    LectureType.mixed: QuizMode.checkpoint,
    LectureType.exam_preparation: QuizMode.both,
}
_DENSITY_BY_DEPTH = {
    ExplanationDepth.concise: SlideDensity.concise,
    ExplanationDepth.standard: SlideDensity.normal,
    ExplanationDepth.detailed: SlideDensity.detailed,
}
_NOTES_BY_DEPTH = {
    ExplanationDepth.concise: SpeakerNotes.concise,
    ExplanationDepth.standard: SpeakerNotes.concise,
    ExplanationDepth.detailed: SpeakerNotes.full,
}
_TONE_BY_AUDIENCE = {
    AudienceLevel.general: LectureTone.conversational,
    AudienceLevel.high_school: LectureTone.conversational,
    AudienceLevel.university_beginner: LectureTone.academic,
    AudienceLevel.university_intermediate: LectureTone.academic,
    AudienceLevel.university_advanced: LectureTone.academic,
    AudienceLevel.graduate: LectureTone.academic,
    AudienceLevel.professional: LectureTone.professional,
}


def infer_defaults(o: LectureOptionsInput) -> dict:
    """Reasonable values for every advanced option, derived from basic options."""
    return {
        "slide_density": _DENSITY_BY_DEPTH[o.explanation_depth],
        "visual_level": (
            VisualLevel.high
            if o.difficulty == Difficulty.introductory
            else VisualLevel.medium
        ),
        "example_level": _EXAMPLE_BY_TYPE[o.lecture_type],
        "practice_level": _PRACTICE_BY_TYPE[o.lecture_type],
        "code_level": _CODE_BY_TYPE[o.lecture_type],
        "quiz_mode": _QUIZ_BY_TYPE[o.lecture_type],
        "lecture_tone": _TONE_BY_AUDIENCE[o.audience_level],
        "speaker_notes": _NOTES_BY_DEPTH[o.explanation_depth],
        "video_intro": VideoIntro.none,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_conflicts(o: LectureOptionsInput, resolved: dict) -> None:
    """Reject contradictory combinations with a readable message."""
    problems: list[str] = []
    if (
        o.lecture_type == LectureType.practice
        and resolved["practice_level"] == PracticeLevel.none
    ):
        problems.append("실습 중심 강의에서는 실습 수준을 '없음'으로 설정할 수 없습니다.")
    if (
        o.lecture_type == LectureType.theory
        and resolved["practice_level"] == PracticeLevel.full
    ):
        problems.append("이론 중심 강의에서는 실습 수준을 '전체 실습'으로 설정할 수 없습니다.")
    if (
        o.lecture_type == LectureType.exam_preparation
        and resolved["quiz_mode"] == QuizMode.none
    ):
        problems.append("시험 대비 강의에서는 퀴즈 모드를 '없음'으로 설정할 수 없습니다.")
    if (
        resolved["code_level"] == CodeLevel.executable
        and resolved["practice_level"] == PracticeLevel.none
    ):
        problems.append("실행 가능한 코드를 사용하려면 실습 수준이 '없음'이 아니어야 합니다.")
    if resolved["video_intro"] == VideoIntro.include:
        from .intro_library import safe_intro_name

        if not safe_intro_name(o.video_intro_file):
            problems.append("인트로를 넣으려면 폴더에서 인트로 영상을 선택해 주세요.")
    if problems:
        raise InvalidLectureProfile(problems[0], details=problems)


_BEGINNER = (AudienceLevel.general, AudienceLevel.high_school, AudienceLevel.university_beginner)
_EXPERT = (AudienceLevel.professional, AudienceLevel.graduate, AudienceLevel.university_advanced)


def collect_warnings(
    o: LectureOptionsInput, resolved: dict, preset: LecturePreset | None
) -> list[str]:
    """Soft problems: the profile is usable, but the instructor should know."""
    w: list[str] = []
    if preset is not None:
        want = PRESETS[preset]
        for name in _PRESET_BASIC:
            if getattr(o, name) != want[name]:
                w.append(
                    f"프리셋 '{preset.value}'의 {name}({want[name].value})과 다른 값"
                    f"({getattr(o, name).value})을 선택하여 선택한 값을 사용합니다."
                )
    if o.duration_minutes <= 20:
        if resolved["practice_level"] == PracticeLevel.full:
            w.append("20분 이하 강의에서 '전체 실습'은 시간이 부족할 수 있습니다.")
        if o.explanation_depth == ExplanationDepth.detailed:
            w.append("20분 이하 강의에서 '상세 설명'은 다룰 수 있는 개념 수가 매우 적어집니다.")
    if o.audience_level in _EXPERT and o.difficulty == Difficulty.introductory:
        w.append("전문가/상급 수강생 대상인데 난이도가 '입문'입니다. 의도한 조합인지 확인하세요.")
    if o.audience_level in _BEGINNER and o.difficulty == Difficulty.advanced:
        w.append("초보 수강생 대상인데 난이도가 '고급'입니다. 의도한 조합인지 확인하세요.")
    if o.lecture_type == LectureType.example_based and resolved["example_level"] == ExampleLevel.none:
        w.append("예제 중심 강의인데 예제 수준이 '없음'입니다.")
    if o.lecture_type == LectureType.theory and resolved["code_level"] == CodeLevel.executable:
        w.append("이론 중심 강의에서 '실행 가능한 코드'는 실습 없이는 활용되기 어렵습니다.")
    return w


def build_profile(
    project_id: str,
    options: LectureOptionsInput,
    existing: LectureProfile | None = None,
) -> LectureProfile:
    defaults = infer_defaults(options)
    preset = options.preset
    preset_values = PRESETS[preset] if preset else {}
    resolved: dict = {}
    inferred: list[str] = []
    from_preset: list[str] = []
    for name in ADVANCED_OPTION_FIELDS:
        value = getattr(options, name)
        if value is not None:  # 1. explicit
            resolved[name] = value
        elif name in preset_values:  # 2. preset
            resolved[name] = preset_values[name]
            from_preset.append(name)
        else:  # 3. inferred
            resolved[name] = defaults[name]
            inferred.append(name)

    validate_conflicts(options, resolved)
    warnings = collect_warnings(options, resolved, preset)
    from .intro_library import safe_intro_name

    intro_file = safe_intro_name(options.video_intro_file)

    now = utcnow()
    return LectureProfile(
        id=existing.id if existing else uuid.uuid4().hex,
        project_id=project_id,
        audience_level=options.audience_level,
        duration_minutes=options.duration_minutes,
        difficulty=options.difficulty,
        lecture_type=options.lecture_type,
        explanation_depth=options.explanation_depth,
        source_policy=options.source_policy,
        **resolved,
        video_intro_file=intro_file,
        inferred_fields=inferred,
        preset=preset,
        preset_fields=from_preset,
        warnings=warnings,
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )


_VIDEO_ONLY_FIELDS = frozenset({"video_intro", "video_intro_file"})
_PROFILE_META_FIELDS = frozenset(
    {"id", "project_id", "created_at", "updated_at", "warnings", "inferred_fields", "preset_fields"}
)


def profile_content_changed(old: LectureProfile, new: LectureProfile) -> bool:
    """True when options that affect plan/slides/scripts changed.

    Intro settings only change the lecture video, so they are ignored here.
    """
    skip = _VIDEO_ONLY_FIELDS | _PROFILE_META_FIELDS
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    return any(a.get(k) != b.get(k) for k in (set(a) | set(b)) - skip)
