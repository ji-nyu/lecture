"""LectureOptions (input) and LectureProfile (stored, structured) schemas."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class AudienceLevel(str, Enum):
    general = "general"
    high_school = "high_school"
    university_beginner = "university_beginner"
    university_intermediate = "university_intermediate"
    university_advanced = "university_advanced"
    graduate = "graduate"
    professional = "professional"


class Difficulty(str, Enum):
    introductory = "introductory"
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"


class LectureType(str, Enum):
    theory = "theory"
    example_based = "example_based"
    practice = "practice"
    mixed = "mixed"
    exam_preparation = "exam_preparation"


class ExplanationDepth(str, Enum):
    concise = "concise"
    standard = "standard"
    detailed = "detailed"


class SourcePolicy(str, Enum):
    source_only = "source_only"
    source_first = "source_first"
    expanded = "expanded"


# ---- Advanced options -------------------------------------------------------
class SlideDensity(str, Enum):
    concise = "concise"
    normal = "normal"
    detailed = "detailed"


class VisualLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class ExampleLevel(str, Enum):
    none = "none"
    low = "low"
    medium = "medium"
    high = "high"


class PracticeLevel(str, Enum):
    none = "none"
    simple = "simple"
    guided = "guided"
    full = "full"


class CodeLevel(str, Enum):
    none = "none"
    snippet = "snippet"
    executable = "executable"


class QuizMode(str, Enum):
    none = "none"
    checkpoint = "checkpoint"
    final = "final"
    both = "both"


class LectureTone(str, Enum):
    academic = "academic"
    professional = "professional"
    conversational = "conversational"


class SpeakerNotes(str, Enum):
    none = "none"
    concise = "concise"
    full = "full"


class VideoIntro(str, Enum):
    none = "none"
    include = "include"


class LecturePreset(str, Enum):
    """Spec presets: ConceptFocused / PracticeFocused / Balanced / ExamPreparation."""

    concept_focused = "concept_focused"
    practice_focused = "practice_focused"
    balanced = "balanced"
    exam_preparation = "exam_preparation"


BASIC_OPTION_FIELDS = (
    "audience_level",
    "duration_minutes",
    "difficulty",
    "lecture_type",
    "explanation_depth",
    "source_policy",
)

ADVANCED_OPTION_FIELDS = (
    "slide_density",
    "visual_level",
    "example_level",
    "practice_level",
    "code_level",
    "quiz_mode",
    "lecture_tone",
    "speaker_notes",
    "video_intro",
)

MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 240


class LectureOptionsInput(BaseModel):
    """What the instructor sends: 6 required basic options + optional advanced ones.

    Advanced options left as `null` are inferred by the server. An optional `preset`
    supplies the advanced values it defines (explicit values still win).
    """

    model_config = ConfigDict(extra="forbid")

    preset: LecturePreset | None = None

    # basic (required)
    audience_level: AudienceLevel
    duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)
    difficulty: Difficulty
    lecture_type: LectureType
    explanation_depth: ExplanationDepth
    source_policy: SourcePolicy

    # advanced (optional)
    slide_density: SlideDensity | None = None
    visual_level: VisualLevel | None = None
    example_level: ExampleLevel | None = None
    practice_level: PracticeLevel | None = None
    code_level: CodeLevel | None = None
    quiz_mode: QuizMode | None = None
    lecture_tone: LectureTone | None = None
    speaker_notes: SpeakerNotes | None = None
    video_intro: VideoIntro | None = None
    video_intro_file: str | None = None


class LectureProfile(BaseModel):
    """Structured, stored profile. Advanced fields are always populated
    (either user-chosen or inferred; see `inferred_fields`)."""

    id: str
    project_id: str

    audience_level: AudienceLevel
    duration_minutes: int
    difficulty: Difficulty
    lecture_type: LectureType
    explanation_depth: ExplanationDepth
    source_policy: SourcePolicy

    slide_density: SlideDensity
    visual_level: VisualLevel
    example_level: ExampleLevel
    practice_level: PracticeLevel
    code_level: CodeLevel
    quiz_mode: QuizMode
    lecture_tone: LectureTone
    speaker_notes: SpeakerNotes
    video_intro: VideoIntro = VideoIntro.none
    video_intro_file: str | None = None

    inferred_fields: list[str] = Field(default_factory=list)
    preset: LecturePreset | None = None
    preset_fields: list[str] = Field(default_factory=list)  # advanced values that came from the preset
    warnings: list[str] = Field(default_factory=list)  # non-blocking option-combination notes
    created_at: datetime
    updated_at: datetime
