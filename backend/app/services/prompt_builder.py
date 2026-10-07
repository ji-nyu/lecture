"""GensparkPromptBuilder (STAGE 6B): turn the approved lecture into ONE self-contained prompt.

    LectureProfile + LecturePlan + SlideSpecification
        (+ EnrichedSlideSpecification, SourceAnalysis, SourceMaterial)  ->  final_prompt

Roles stay as in the whole system: the rule engine planned the lecture, the LLM (STAGE 6A) wrote
the slide content, and the presentation provider only RENDERS. So the prompt does not ask the
provider to plan, to shorten or to add anything: it states the fixed structure, the fixed content of
every slide and the rules for drawing it.

Properties
  * Deterministic: rules only, no LLM, no network, no time stamp inside the text.
  * Provider independent: nothing here knows Genspark's API. (The class name follows the spec;
    the text is plain Korean/markdown that any generator can read.) Delivering it is STAGE 7/8.
  * Every option of the LectureProfile changes the wording somewhere (audience, difficulty, depth,
    type, tone, density, visual level, example / practice / code / quiz levels, speaker notes,
    source policy) - see `_options_text`.
  * A slide whose enrichment failed uses the SlidePlanner's text; a slide the source has nothing for
    is marked as such instead of being invented.

Prompt structure (the 13 sections of the spec, in this order):
    ROLE, PRESENTATION OBJECTIVE, AUDIENCE, DURATION, DIFFICULTY, LECTURE STYLE, SOURCE POLICY,
    LEARNING OBJECTIVES, LECTURE STRUCTURE, SLIDE SPECIFICATION, VISUAL REQUIREMENTS,
    SPEAKER NOTES REQUIREMENTS, SOURCE MATERIAL CONSTRAINTS
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models.enriched_slide_spec import (
    EnrichedSlide,
    EnrichedSlideSpecification,
    FieldProvenance,
    SlideEnrichmentStatus,
)
from ..models.lecture_plan import LecturePlan
from ..models.lecture_profile import LectureProfile
from ..models.lecture_script import LectureScript
from ..models.presentation_prompt import (
    PresentationPrompt,
    PromptContentSource,
    PromptSectionInfo,
    PromptSlideInfo,
    PromptValidation,
    PromptVersion,
)
from ..models.slide_spec import ContentOrigin, Slide, SlideSpecification, SlideType
from ..models.source import CodeBlock, SourceAnalysis, SourceLocation, SourceMaterial
from .slide_content_enricher import hash_profile, hash_spec
from .source_context import CHARS_PER_SECOND

BUILDER_VERSION = "genspark-prompt-v5"

SECTION_NAMES = (
    "ROLE",
    "PRESENTATION OBJECTIVE",
    "AUDIENCE",
    "DURATION",
    "DIFFICULTY",
    "LECTURE STYLE",
    "SOURCE POLICY",
    "LEARNING OBJECTIVES",
    "LECTURE STRUCTURE",
    "SLIDE SPECIFICATION",
    "VISUAL REQUIREMENTS",
    "SPEAKER NOTES REQUIREMENTS",
    "SOURCE MATERIAL CONSTRAINTS",
)

MAX_CODE_LINES = 120  # a longer source code block is cut (and the cut is announced in the prompt)
MAX_OUTLINE_LINES = 40
# A heuristic, NOT a provider limit (none is known): only a hint that the text is long.
LONG_PROMPT_CHARS = 60_000

_HEADING = re.compile(r"^## (.+)$", re.M)
_SLIDE_HEADING = re.compile(r"^### 슬라이드 (\d+)/(\d+) · (.+)$", re.M)


# ---------------------------------------------------------------------------
# wording tables (one entry per option value: an option that is not in here would not change the text)
# ---------------------------------------------------------------------------
_AUDIENCE = {
    "general": (
        "일반 청중",
        "전문 배경지식이 없는 청중이다. 어려운 용어에는 풀이가 붙어 있으므로 그대로 크게 보여 주고, 본문 문장을 읽히게 둔다. 그림만으로 설명하지 않는다.",
    ),
    "high_school": (
        "고등학생",
        "고등학생 눈높이다. 밝고 친근한 시각 요소와 구체적인 그림을 많이 쓰고 글은 짧게 유지한다.",
    ),
    "university_beginner": (
        "대학 초급(학부 저학년)",
        "기초 개념을 처음 배우는 학생이다. 용어와 정의를 눈에 띄게 강조하고, 도식으로 개념 사이의 관계를 보여 준다.",
    ),
    "university_intermediate": (
        "대학 중급",
        "기본 용어는 알고 있는 학생이다. 동작 원리와 구성 요소의 상호작용을 보여 주는 구조도·흐름도를 우선한다.",
    ),
    "university_advanced": (
        "대학 고급",
        "기술 세부사항을 이해하는 학생이다. 세부 구조와 trade-off를 밀도 있게 배치해도 좋다.",
    ),
    "graduate": (
        "대학원",
        "연구·설계 관점을 가진 청중이다. 세부 구조, 한계, 비교를 정확하고 간결하게 배치한다.",
    ),
    "professional": (
        "실무 전문가",
        "실무자이다. 기본 정의에 화면을 쓰지 않고 아키텍처, 설계 판단, 운영 관점을 중심으로 깔끔하게 배치한다.",
    ),
}
_DIFFICULTY = {
    "introductory": ("입문", "개념을 한 번에 하나씩 보여 주고 수식·세부 사양은 화면에 넣지 않는다."),
    "beginner": ("초급", "기본 개념 중심으로 단계적으로 보여 주고 세부 사양은 최소화한다."),
    "intermediate": ("중급", "원리와 동작 방식을 보여 주며 기술 용어를 그대로 사용해도 된다."),
    "advanced": ("고급", "내부 메커니즘과 세부 구현, trade-off를 화면에 담아도 좋다."),
}
_DEPTH = {
    "concise": ("간결", "핵심 문장은 화면에 읽히게 두고, 같은 말의 반복만 발표자 노트에 맡긴다."),
    "standard": ("표준", "핵심 항목과 짧은 설명을 균형 있게 보여 준다."),
    "detailed": ("상세", "핵심 항목에 더해 설명·예시를 화면에 충분히 보여 준다(슬라이드를 나누지 않고 배치로 해결한다)."),
}
_TYPE = {
    "theory": ("이론 중심", "개념·정의·원리를 명확한 구조도와 정의 상자로 보여 준다."),
    "example_based": ("예제 중심", "개념마다 예시가 눈에 잘 띄도록 예시 영역을 따로 구분해 배치한다."),
    "practice": ("실습 중심", "실습 단계와 확인 지점을 번호·체크리스트 형식으로 뚜렷하게 배치한다."),
    "mixed": ("이론+실습 혼합", "이론 슬라이드와 실습 슬라이드가 시각적으로 구분되도록 서로 다른 레이아웃 색을 쓴다."),
    "exam_preparation": ("시험 대비", "핵심 정의·비교·요약을 표와 강조 상자로 정리해 암기와 복습이 쉽게 만든다."),
}
_TONE = {
    "academic": ("학술적", "차분하고 단정한 디자인. 절제된 색상과 표준 서체를 쓴다."),
    "professional": ("전문적", "간결하고 깔끔한 비즈니스 디자인. 정보 위계를 분명히 한다."),
    "conversational": ("대화체", "친근하고 가벼운 디자인. 질문·강조 표현이 있는 슬라이드는 눈에 띄게 배치한다."),
}
_DENSITY = {
    "concise": "슬라이드 밀도=간결: 한 슬라이드에 항목 최대 {n}개. 여백을 충분히 두고 글자를 크게 쓴다.",
    "normal": "슬라이드 밀도=보통: 한 슬라이드에 항목 최대 {n}개. 제목·핵심 메시지·항목의 위계를 분명히 한다.",
    "detailed": "슬라이드 밀도=상세: 한 슬라이드에 항목 최대 {n}개. 글자 크기를 줄여서라도 주어진 내용을 모두 담는다.",
}
_EXAMPLE = {
    "none": "예시 수준=없음: 예시 영역을 만들지 않는다. 명세에 예시 항목이 없는 슬라이드에 예시를 새로 만들지 않는다.",
    "low": "예시 수준=낮음: 명세에 예시가 있는 슬라이드에서만 예시를 작게 보여 준다.",
    "medium": "예시 수준=보통: 명세에 예시가 있으면 본문과 구분되는 상자로 보여 준다.",
    "high": "예시 수준=높음: 명세에 예시가 있으면 슬라이드의 주요 영역에 크게 보여 준다(예시를 새로 만들지는 않는다).",
}
_PRACTICE = {
    "none": "실습 수준=없음: 실습 슬라이드가 없어야 한다. 명세에 실습이 없으면 실습 화면을 만들지 않는다.",
    "simple": "실습 수준=간단: 실습 슬라이드는 단계 목록과 기대 결과만 간결하게 보여 준다.",
    "guided": "실습 수준=따라 하기: 실습 슬라이드는 준비물, 번호가 붙은 단계, 기대 결과, 주의사항을 구분해 보여 준다.",
    "full": "실습 수준=전체: 실습 슬라이드는 준비물, 단계, 기대 결과, 주의사항을 빠짐없이 보여 주고 확인 체크 칸을 둔다.",
}
_CODE = {
    "none": "코드 수준=없음: 코드 화면을 새로 만들지 않는다. 명세에 코드가 없으면 코드를 넣지 않는다.",
    "snippet": "코드 수준=발췌: 코드 슬라이드는 주어진 코드를 읽기 쉬운 고정폭 글꼴로 보여 주고 핵심 줄을 강조한다(실행 화면은 만들지 않는다).",
    "executable": "코드 수준=실행: 코드 슬라이드는 주어진 코드를 그대로 보여 주고, 실행 흐름과 예상 출력을 나란히 배치할 영역을 둔다.",
}
_QUIZ = {
    "none": "퀴즈 수준=없음: 명세에 퀴즈 슬라이드가 없으면 퀴즈를 만들지 않는다.",
    "checkpoint": "퀴즈 수준=중간 확인: 중간 확인 퀴즈 슬라이드는 문제와 보기를 간결하게 보여 준다.",
    "final": "퀴즈 수준=마무리: 마무리 퀴즈 슬라이드는 문제와 보기를 한눈에 보이게 보여 준다.",
    "both": "퀴즈 수준=중간+마무리: 중간 확인과 마무리 퀴즈 슬라이드를 같은 디자인으로 통일해 보여 준다.",
}
_VISUAL = {
    "low": "시각 수준=낮음: 텍스트 중심의 단정한 슬라이드로 만든다. 도식·표는 슬라이드 명세가 요구한 곳에만 쓰고 장식 이미지는 넣지 않는다.",
    "medium": "시각 수준=보통: 구조도·흐름도·비교표를 슬라이드 명세가 요구한 곳에 쓰고, 그 밖의 슬라이드에는 아이콘 정도의 절제된 시각 요소를 쓴다.",
    "high": "시각 수준=높음: 본문 문장이 화면의 주된 내용이다. 도식은 본문에 나온 대상과 관계를 상자·화살표·라벨로만 그린다. 그림만 있고 설명이 없는 슬라이드, 화면을 채우는 거대 장식 글자, 빈 일러스트는 쓰지 않는다. 시각 요소가 내용을 바꾸거나 새 사실을 추가해서는 안 된다.",
}
_POLICY = {
    "source_only": [
        "원문 전용(source_only): 이 강의는 업로드된 강의자료의 내용만 다룬다.",
        "슬라이드 명세에 적힌 내용 밖의 사실, 수치, 사례, 인용, 명령어를 화면·도식·발표자 노트 어디에도 추가하지 않는다.",
        "도식과 아이콘은 명세에 나온 용어와 관계만 그린다. 명세에 없는 구성 요소를 그려 넣지 않는다.",
        "비유와 외부 사례를 만들지 않는다.",
    ],
    "source_first": [
        "원문 우선(source_first): 강의자료가 중심이고, 명세에 이미 적힌 보충 설명과 예시만 그대로 사용한다.",
        "명세에 적힌 것 밖의 새로운 사실, 수치, 버전, 경로, 명령어를 추가하지 않는다.",
        "표시된 교육용 예시·보충 설명은 원문 내용이 아님을 아는 상태에서 배치한다(본문과 시각적으로 구분해도 좋다).",
        "'내용 지시'가 붙은 슬라이드만 일반적인 배경 지식으로 채운다(특정 제품·버전·수치를 단정하지 않는다).",
    ],
    "expanded": [
        "확장(expanded): 강의자료를 바탕으로 하되 명세에 적힌 외부 지식·사례·비유를 그대로 사용한다.",
        "원문과 충돌하는 내용을 넣지 않는다. 명세에 없는 수치·버전·경로·명령어는 추측해서 추가하지 않는다.",
        "'내용 지시'가 붙은 슬라이드는 강의자료의 주제와 어긋나지 않는 범위의 일반 지식으로 채운다.",
    ],
}
_NOTES = {
    "none": [
        "발표자 노트 수준=없음: PPT의 발표자 노트를 작성하지 않는다. 노트란은 비워 둔다.",
        "슬라이드 명세의 '발표자 노트' 항목도 없다. 발표 방향을 화면에 옮겨 적지 않는다.",
    ],
    "concise": [
        "발표자 노트 수준=간단: 슬라이드마다 핵심 설명 2~4문장의 발표자 노트를 PPT 노트란에 넣는다.",
        "명세에 '발표자 노트'가 있으면 그 문장을 그대로 노트란에 입력한다.",
    ],
    "full": [
        "발표자 노트 수준=대본: 슬라이드마다 그 슬라이드를 설명하는 대본을 PPT 노트란에 넣는다.",
        f"명세의 '발표자 노트'는 이미 그 슬라이드의 예상 설명 시간에 맞춘 분량이다(말하는 속도 약 {CHARS_PER_SECOND:g}자/초). 그대로 입력하고 줄이거나 늘리지 않는다.",
    ],
}
_LAYOUT = {
    "title": "표지. 강의 제목을 가장 크게, 부제(대상·시간)를 작게 배치한다.",
    "agenda": "번호가 붙은 목록. 항목 사이 간격을 일정하게 두고 시간이 있는 항목은 시간을 오른쪽에 정렬한다.",
    "definition": "용어와 정의 전문을 같은 화면에 읽히게 둔다. 본문 항목은 완전한 문장으로 정의 상자 안에 넣는다. 용어만 적힌 빈 상자나 화면을 채우는 거대 장식 글자를 만들지 않는다.",
    "concept": "핵심 메시지(진행 지시가 아닐 때)를 제목 아래 한 줄로 두고, 본문 항목을 완전한 문장의 목록이나 카드로 배치한다. 개념 이름만 적힌 빈 상자·빈 카드를 만들지 않는다.",
    "comparison": "비교 표. 비교 대상을 열로, 항목별 차이를 행으로 배치한다.",
    "diagram": "도식. 구성 요소를 노드로, 관계를 선·화살표로 그린다. 요소 이름은 명세의 용어를 그대로 쓴다.",
    "architecture": "구조도. 구성 요소를 상자로 배치하고 데이터·제어 흐름을 화살표로 그린다. 시각 지시의 배치와 방향을 따른다.",
    "workflow": "흐름도. 단계를 순서대로 배치하고 단계 사이를 화살표로 연결한다.",
    "example": "예시 상자를 중심으로 배치한다. 상황 → 동작 → 결과를 단어가 아니라 문장으로 적는다.",
    "practice": "번호가 붙은 실습 단계와 체크 칸. 준비물, 기대 결과, 주의사항은 구분된 영역에 둔다.",
    "code": "코드 상자(고정폭 글꼴, 줄 번호 가능) + 옆 또는 아래에 코드 설명. 코드는 한 글자도 바꾸지 않는다.",
    "formula": "수식을 크게 가운데 정렬하고 기호 설명을 아래에 둔다. 수식은 그대로 사용한다.",
    "quiz": "문제를 크게, 보기를 번호가 붙은 선택지로 배치한다. 정답과 해설은 화면에 미리 보이지 않게 한다.",
    "summary": "핵심 항목을 번호나 체크 표시가 붙은 짧은 목록으로 정리한다.",
}
_TYPE_LABEL = {
    "title": "표지", "agenda": "목차/학습 목표", "concept": "개념", "definition": "정의", "comparison": "비교",
    "diagram": "도식", "architecture": "구조도", "workflow": "흐름도", "example": "예시", "practice": "실습",
    "code": "코드", "formula": "수식", "quiz": "퀴즈", "summary": "요약",
}
_KIND_LABEL = {
    "intro": "도입", "prerequisite": "선수 지식", "concept": "핵심 개념", "comparison": "비교", "caution": "주의점",
    "practice": "실습", "quiz": "퀴즈", "summary": "정리",
}
_VISUAL_TYPES = {"diagram", "architecture", "workflow", "comparison"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _dur(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    if m and s:
        return f"{m}분 {s}초"
    return f"{m}분" if m else f"{s}초"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _loc(loc: SourceLocation | None) -> str | None:
    if loc is None:
        return None
    parts = []
    if loc.section_title:
        parts.append(f"‘{loc.section_title}’")
    if loc.line:
        parts.append(f"{loc.line}행")
    if loc.page:
        parts.append(f"{loc.page}쪽")
    return " ".join(parts) or None


def _fence(code: str) -> str:
    n = 3
    while "~" * n in code:
        n += 1
    return "~" * n


def _labelled(label: str, text: str) -> list[str]:
    """`- label: text` for one line, an indented block for several lines."""
    body = [ln.rstrip() for ln in str(text).strip().splitlines() if ln.strip()]
    if len(body) <= 1:
        return [f"- {label}: {body[0] if body else ''}".rstrip()]
    return [f"- {label}:", *[f"  {ln}" for ln in body]]


_STRUCTURAL_MSG = re.compile(
    r"['‘\"].+['’\"]의 .{0,40}(역할과 동작 방식을 설명한다|정의를 나란히 확인한다|앞선 개념과의 관계를 정리한다)"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[다요])\.\s+")
_BODY_LIMIT = {"concise": 3, "normal": 5, "detailed": 7}


def _is_structural_message(text: str) -> bool:
    """Planner placeholders like '‘AIoT’의 역할과 동작 방식을 설명한다' are not on-slide copy."""
    t = _one_line(text)
    if not t:
        return False
    if _STRUCTURAL_MSG.search(t):
        return True
    if t.startswith("앞 슬라이드에 이어") and t.endswith("설명한다."):
        return True
    if "자료에 예시가 없어" in t or "자료에 없어 강사가 보충한다" in t:
        return True
    if "다시 정리한다" in t or t.startswith("오늘 배운"):
        return True
    return False


def _is_label_point(text: str, slide: Slide) -> bool:
    """A bullet that is only a concept/title label, not a sentence the audience can read."""
    t = _one_line(text)
    if not t:
        return True
    names = {slide.title, *slide.concepts}
    names |= {f"‘{n}’" for n in slide.concepts}
    names |= {f"용어 정의: {n}" for n in slide.concepts}
    names |= {f"예시: {n}" for n in slide.concepts}
    return t in names


def _sentences(text: str) -> list[str]:
    """Split Korean source prose into sentences without inventing wording."""
    if not text or not str(text).strip():
        return []
    blob = " ".join(str(text).replace("\r", "\n").split())
    parts = _SENTENCE_SPLIT.split(blob)
    out: list[str] = []
    for i, p in enumerate(parts):
        t = p.strip()
        if not t:
            continue
        if not t.endswith((".", "?", "!", "。")) and (i < len(parts) - 1 or t.endswith(("다", "요"))):
            t += "."
        if len(t) >= 12:
            out.append(t)
    return out


def _norm_text(text: str) -> str:
    return _one_line(text).rstrip(".…")


def _bullets(label: str, items: list[str]) -> list[str]:
    items = [_one_line(i) for i in items if i and str(i).strip()]
    if not items:
        return []
    return [f"- {label}:", *[f"  - {i}" for i in items]]


def _numbered(label: str, items: list[str]) -> list[str]:
    items = [_one_line(i) for i in items if i and str(i).strip()]
    if not items:
        return []
    return [f"- {label}:", *[f"  {n}. {i}" for n, i in enumerate(items, 1)]]


@dataclass
class _Content:
    """What one slide will show (already resolved: enriched text, or the planner's text)."""

    key_message: str
    body_points: list[str] = field(default_factory=list)
    explanation: str | None = None
    example: str | None = None
    example_is_made_up: bool = False
    analogy: str | None = None
    practice: object | None = None
    code_explanation: object | None = None
    quiz: object | None = None
    summary_message: str | None = None
    visual: str = ""
    notes: str | None = None
    notes_are_direction: bool = False
    kind: str = "rule_based"  # enriched | rule_based | rule_based_fallback | placeholder
    body_fell_back: bool = False


# ---------------------------------------------------------------------------
# the builder
# ---------------------------------------------------------------------------
class GensparkPromptBuilder:
    """Builds `final_prompt` from the approved lecture. See the module docstring."""

    def build(
        self,
        *,
        project_id: str,
        profile: LectureProfile,
        plan: LecturePlan,
        spec: SlideSpecification,
        analysis: SourceAnalysis | None = None,
        material: SourceMaterial | None = None,
        enriched: EnrichedSlideSpecification | None = None,
        script: LectureScript | None = None,
        generated_at: datetime | None = None,
    ) -> PresentationPrompt:
        warnings: list[str] = []
        used = self._usable_enrichment(enriched, spec, warnings)
        by_number = {s.slide_number: s for s in used.slides} if used else {}
        code_blocks = self._code_index(analysis, material)
        formulas = self._formula_index(analysis, material)

        ctx = _Ctx(
            profile, plan, spec, analysis, material, used, by_number, code_blocks, formulas, warnings,
            script=script,
        )
        sections: list[tuple[str, str]] = [
            ("ROLE", self._role(ctx)),
            ("PRESENTATION OBJECTIVE", self._objective(ctx)),
            ("AUDIENCE", self._audience(ctx)),
            ("DURATION", self._duration(ctx)),
            ("DIFFICULTY", self._difficulty(ctx)),
            ("LECTURE STYLE", self._style(ctx)),
            ("SOURCE POLICY", self._policy(ctx)),
            ("LEARNING OBJECTIVES", self._objectives(ctx)),
            ("LECTURE STRUCTURE", self._structure(ctx)),
        ]
        slide_text, slide_infos = self._slides(ctx)
        sections.append(("SLIDE SPECIFICATION", slide_text))
        sections += [
            ("VISUAL REQUIREMENTS", self._visual(ctx)),
            ("SPEAKER NOTES REQUIREMENTS", self._speaker_notes(ctx)),
            ("SOURCE MATERIAL CONSTRAINTS", self._constraints(ctx)),
        ]
        assert tuple(n for n, _ in sections) == SECTION_NAMES

        head = f"# 강의 PPT 제작 지시서: {plan.title}\n"
        text = head + "".join(f"\n## {name}\n{body.rstrip()}\n" for name, body in sections)
        info = [PromptSectionInfo(name=n, char_count=len(b)) for n, b in sections]

        fallback = [i.slide_number for i in slide_infos if i.content == "rule_based_fallback"]
        placeholders = [i.slide_number for i in slide_infos if i.content == "placeholder"]
        if used is None:
            warnings.append(
                "슬라이드 콘텐츠 보강 없이 규칙 기반 내용으로 프롬프트를 만들었습니다. 슬라이드 본문이 간단할 수 있습니다."
            )
        if fallback:
            warnings.append(
                f"콘텐츠 보강에 실패한 슬라이드 {_ranges(fallback)}은(는) 규칙 기반 내용을 사용했습니다."
            )
        if placeholders:
            warnings.append(
                f"원문에 내용이 없는 슬라이드 {_ranges(placeholders)}은(는) '내용 지시'로 표시했습니다(공급자가 채우게 됩니다)."
            )
        if ctx.filled_slides:
            warnings.append(
                f"본문이 용어 이름뿐이었던 슬라이드 {_ranges(ctx.filled_slides)}은(는) 원문 문장으로 채웠습니다."
            )
        if len(text) > LONG_PROMPT_CHARS:
            warnings.append(
                f"프롬프트가 깁니다({len(text):,}자). 사용할 프레젠테이션 공급자의 입력 한도를 확인하세요."
            )

        validation = self._validate(text, sections, spec)
        options = self._options(profile, spec)
        version = PromptVersion(
            builder_version=BUILDER_VERSION,
            generated_at=generated_at or datetime.now(timezone.utc),
            slide_spec_hash=hash_spec(spec),
            lecture_profile_hash=hash_profile(profile),
            enrichment_prompt_version=used.version.prompt_version if used else None,
            enrichment_model=used.version.model_name if used else None,
        )
        return PresentationPrompt(
            project_id=project_id,
            lecture_id=plan.id,
            title=plan.title,
            final_prompt=text,
            prompt_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            char_count=len(text),
            line_count=text.count("\n") + 1,
            content_source=PromptContentSource.enriched if used else PromptContentSource.rule_based,
            enrichment_status=used.status.value if used else None,
            slide_count=spec.slide_count,
            section_names=list(SECTION_NAMES),
            sections=info,
            slides=slide_infos,
            fallback_slide_numbers=fallback,
            placeholder_slide_numbers=placeholders,
            code_slide_numbers_without_code=ctx.code_missing,
            options=options,
            validation=validation,
            warnings=warnings + ctx.notes,
            version=version,
        )

    # ------------------------------------------------------------------ inputs
    @staticmethod
    def _usable_enrichment(
        e: EnrichedSlideSpecification | None, spec: SlideSpecification, warnings: list[str]
    ) -> EnrichedSlideSpecification | None:
        """Enriched content is used only when it belongs to exactly this specification."""
        if e is None:
            return None
        same = (
            e.slide_count == spec.slide_count
            and all(
                a.slide_number == b.slide_number and a.slide_type == b.slide_type and a.section_id == b.section_id
                for a, b in zip(e.slides, spec.slides)
            )
            and e.version.slide_spec_hash == hash_spec(spec)
        )
        if not same:
            warnings.append("보강된 콘텐츠가 현재 슬라이드 구성과 맞지 않아 사용하지 않았습니다. 콘텐츠 보강을 다시 실행하세요.")
            return None
        if not any(s.status == SlideEnrichmentStatus.enriched for s in e.slides):
            warnings.append("콘텐츠 보강이 모든 슬라이드에서 실패해 규칙 기반 내용을 사용했습니다.")
            return None
        return e

    @staticmethod
    def _code_index(analysis, material) -> dict[tuple, CodeBlock]:
        blocks = list(material.code_blocks) if material and material.code_blocks else []
        if not blocks and analysis is not None:
            blocks = list(analysis.code_examples)
        return {(b.location.section_id, b.location.line, b.location.page): b for b in blocks if b.location}

    @staticmethod
    def _formula_index(analysis, material) -> dict[tuple, object]:
        items = list(material.formulas) if material and material.formulas else []
        if not items and analysis is not None:
            items = list(analysis.formulas)
        return {(f.location.section_id, f.location.line, f.location.page): f for f in items if f.location}

    # ------------------------------------------------------------------ sections
    def _role(self, c: "_Ctx") -> str:
        n = c.spec.slide_count
        lines = [
            "당신은 대학 강의용 프레젠테이션(PPTX)을 시각적으로 제작하는 프레젠테이션 디자이너이다.",
            "강의 구조와 슬라이드 내용은 이미 확정되었다. 당신은 강의를 기획하거나 내용을 고치지 않고, 아래 SLIDE SPECIFICATION을 슬라이드로 그려 내는 일(레이아웃, 도식, 시각 요소, 발표자 노트 배치)만 한다.",
            "",
            "반드시 지킬 규칙:",
            f"1. 슬라이드는 정확히 {n}장이다. 번호, 순서, 제목은 명세 그대로이다.",
            "2. 슬라이드를 추가, 삭제, 병합, 분할하지 않는다. 명세에 없는 표지·목차·감사·Q&A 슬라이드를 만들지 않는다.",
            "3. 본문 항목은 그 슬라이드 대본에서 고른 문장을 그대로 화면에 넣는다. 대본에 없는 설명을 만들지 않는다. 넘치면 글자 크기와 배치를 조정한다.",
            "4. 명세에 없는 사실, 수치, 사례, 인용, 이미지 설명을 만들어 넣지 않는다(SOURCE POLICY 참고).",
            "5. 각 슬라이드의 '시각 지시'는 도식과 배치를 그리는 지시문이다. 지시에 나온 요소와 방향을 그대로 따른다.",
            "6. 예상 설명 시간은 발표 계획일 뿐이다. 명세의 본문에 있는 경우를 제외하고 슬라이드에 시간을 표시하지 않는다.",
            "7. 결과물은 위 구조 그대로의 PPTX 한 개이다.",
            "8. 학습 목적과 '설명한다/정리한다'처럼 강의 진행을 말하는 문장은 화면 본문이 아니다. 화면에 넣을 글은 '핵심 메시지'(진행 지시가 아닌 경우)와 '본문 항목'이다.",
            "9. 본문 항목은 반드시 화면에 읽히는 완전한 문장으로 넣는다. 제목이나 개념 이름만 적힌 빈 상자·빈 카드·빈 도형을 만들지 않는다. 장식 이미지가 본문을 대체하지 않는다.",
            "10. 슬라이드만 봐도 그 장의 정의와 관계를 따라갈 수 있어야 한다. 화면을 채우는 거대 장식 글자(용어를 180포인트로 크게만 쓰는 것)를 넣지 않는다.",
            "11. 도식의 모든 상자·화살표에는 명세에 나온 용어 라벨을 붙인다. 그림만 있고 읽을 문장이 없는 슬라이드를 만들지 않는다.",
            "12. 청중이 대본을 들을 때 화면에 같은 내용이 있어야 한다. 대본에 있는 정의·관계 문장을 빼지 않는다.",
        ]
        return "\n".join(lines)

    def _objective(self, c: "_Ctx") -> str:
        p, plan, spec = c.profile, c.plan, c.spec
        label = _TYPE[p.lecture_type.value][0]
        aud = _AUDIENCE[p.audience_level.value][0]
        lines = [
            f"- 강의 제목: {plan.title}",
            f"- 강의 유형: {label}",
            f"- 대상: {aud}",
            f"- 분량: 슬라이드 수: {spec.slide_count}장, 발표 시간 {plan.duration_minutes}분",
            f"- 목표: {aud} 청중이 {plan.duration_minutes}분 동안 진행하는 ‘{plan.title}’ 강의에서 아래 LEARNING OBJECTIVES를 달성하도록 돕는 강의용 슬라이드를 만든다.",
            "- 성공 기준: 모든 슬라이드가 자신의 학습 목적을 분명히 보여 주고, 명세와 다른 내용이 없다.",
        ]
        return "\n".join(lines)

    def _audience(self, c: "_Ctx") -> str:
        label, guide = _AUDIENCE[c.profile.audience_level.value]
        return f"- 대상 청중: {label}\n- 디자인 지침: {guide}"

    def _duration(self, c: "_Ctx") -> str:
        plan, spec = c.plan, c.spec
        avg = round(spec.duration_minutes * 60 / spec.slide_count)
        lines = [
            f"- 총 발표 시간: {plan.duration_minutes}분",
            f"- 슬라이드 수: {spec.slide_count}장 (평균 {_dur(avg)})",
            "- 섹션별 시간 배분:",
        ]
        for s in plan.sections:
            n = sum(1 for sl in spec.slides if sl.section_id == s.id)
            lines.append(f"  - {s.order}. {s.title}: {s.duration_minutes}분 (슬라이드 {n}장)")
        lines.append("- 실습 슬라이드의 시간에는 학습자가 직접 수행하는 시간이 포함되어 있다.")
        lines.append("- 슬라이드별 예상 설명 시간은 SLIDE SPECIFICATION에 있다.")
        return "\n".join(lines)

    def _difficulty(self, c: "_Ctx") -> str:
        p = c.profile
        dl, dg = _DIFFICULTY[p.difficulty.value]
        el, eg = _DEPTH[p.explanation_depth.value]
        return f"- 난이도: {dl}\n- 표현 지침: {dg}\n- 설명 깊이: {el}\n- 깊이 지침: {eg}"

    def _style(self, c: "_Ctx") -> str:
        p, spec = c.profile, c.spec
        tl, tg = _TYPE[p.lecture_type.value]
        ol, og = _TONE[p.lecture_tone.value]
        lines = [
            f"- 강의 유형: {tl} — {tg}",
            f"- 어조/디자인: {ol} — {og}",
            f"- {_DENSITY[p.slide_density.value].format(n=spec.style.max_key_points)}",
            f"- {_EXAMPLE[p.example_level.value]}",
            f"- {_PRACTICE[p.practice_level.value]}",
            f"- {_CODE[p.code_level.value]}",
            f"- {_QUIZ[p.quiz_mode.value]}",
        ]
        return "\n".join(lines)

    def _policy(self, c: "_Ctx") -> str:
        return "\n".join(f"- {t}" for t in _POLICY[c.profile.source_policy.value])

    def _objectives(self, c: "_Ctx") -> str:
        objs = c.plan.learning_objectives
        if not objs:
            return "- (학습 목표가 지정되지 않았다. 각 슬라이드의 학습 목적을 따른다.)"
        return "\n".join(f"{i}. {_one_line(o)}" for i, o in enumerate(objs, 1))

    def _structure(self, c: "_Ctx") -> str:
        lines = ["강의는 아래 순서의 섹션으로 구성된다. 섹션과 슬라이드의 순서는 바뀌지 않는다.", ""]
        for s in c.plan.sections:
            nums = [sl.slide_number for sl in c.spec.slides if sl.section_id == s.id]
            span = "—" if not nums else (f"{nums[0]}" if len(nums) == 1 else f"{nums[0]}–{nums[-1]}")
            kind = _KIND_LABEL.get(s.kind.value, s.kind.value)
            lines.append(
                f"{s.order}. {s.title} [{kind}] · {s.duration_minutes}분 · 슬라이드 {span}번 ({len(nums)}장)"
            )
            if s.purpose:
                lines.append(f"   - 목적: {_one_line(s.purpose)}")
            if s.concepts:
                lines.append(f"   - 다루는 개념: {', '.join(s.concepts)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ slides
    def _slides(self, c: "_Ctx") -> tuple[str, list[PromptSlideInfo]]:
        n = c.spec.slide_count
        notes_item = (
            ", 발표자 노트(PPT 노트란에 넣을 내용)" if c.profile.speaker_notes.value != "none" else ""
        )
        parts = [
            f"아래 {n}장의 슬라이드를 번호 순서대로 만든다.",
            "각 블록의 항목: 레이아웃(그 유형의 기본 배치), 핵심 메시지·본문 항목(대본에서 고른 문장)·설명·예시 등, "
            f"시각 지시(도식과 배치){notes_item}.",
            "",
        ]
        infos: list[PromptSlideInfo] = []
        for slide in c.spec.slides:
            content = self._resolve(c, slide)
            self._ensure_on_slide_text(c, slide, content)
            block = self._slide_block(c, slide, content)
            parts.append(block)
            parts.append("")
            infos.append(
                PromptSlideInfo(
                    slide_number=slide.slide_number, title=slide.title, slide_type=slide.slide_type.value,
                    content=content.kind, char_count=len(block),
                )
            )
        return "\n".join(parts).rstrip(), infos

    def _resolve(self, c: "_Ctx", slide: Slide) -> _Content:
        es = c.by_number.get(slide.slide_number)
        speaker = c.profile.speaker_notes.value
        if es is not None and es.status == SlideEnrichmentStatus.enriched and es.enriched is not None:
            e = es.enriched
            prov = es.provenance
            made_up = prov.get("example") == FieldProvenance.llm_example
            out = _Content(
                key_message=e.key_message or slide.key_message,
                body_points=list(e.body_points),
                explanation=e.explanation, example=e.example, example_is_made_up=made_up,
                analogy=e.analogy, practice=e.practice_instruction, code_explanation=e.code_explanation,
                quiz=e.quiz_content, summary_message=e.summary_message,
                visual=e.visual_instruction or slide.visual_instruction, kind="enriched",
            )
            if speaker != "none":
                if e.presenter_notes:
                    out.notes = e.presenter_notes
                else:
                    out.notes, out.notes_are_direction = slide.presenter_instruction, True
            if not (out.body_points or out.explanation or out.example or out.analogy or out.practice
                    or out.code_explanation or out.quiz or out.summary_message):
                # the enriched slide shows only its key message: keep what the planner listed
                out.body_points, out.body_fell_back = list(slide.key_points), True
            return out
        kind = "rule_based_fallback" if es is not None else "rule_based"
        if slide.content_origin == ContentOrigin.suggested:
            kind = "placeholder"
        return _Content(
            key_message=slide.key_message, body_points=list(slide.key_points), visual=slide.visual_instruction,
            notes=slide.presenter_instruction if speaker != "none" else None,
            notes_are_direction=True, kind=kind,
        )

    def _ensure_on_slide_text(self, c: "_Ctx", slide: Slide, k: _Content) -> None:
        """Put the spoken script's teaching sentences on the slide so the deck matches the lecture."""
        if slide.slide_type == SlideType.title or k.kind == "placeholder":
            self._mark_used(c, k)
            return
        if c.script is not None:
            item = next((s for s in c.script.slides if s.slide_number == slide.slide_number), None)
            if item and item.text.strip():
                from .lecture_script_builder import script_body_points

                limit = max(_BODY_LIMIT.get(c.profile.slide_density.value, 5), 6)
                points = script_body_points(item.text, limit)
                if points:
                    k.body_points = points
                    if _is_structural_message(k.key_message):
                        k.key_message = points[0]
                    self._mark_used(c, k)
                    return
        limit = _BODY_LIMIT.get(c.profile.slide_density.value, 5)
        real = [p for p in k.body_points if not _is_label_point(p, slide)]
        extra = self._source_fill(c, slide, k, limit)
        teaching = slide.slide_type in (
            SlideType.definition, SlideType.concept, SlideType.example,
            SlideType.summary, SlideType.architecture, SlideType.workflow,
            SlideType.diagram, SlideType.comparison,
        )
        labels_only = not real or any(_is_label_point(p, slide) for p in k.body_points)
        if extra and (labels_only or (teaching and len(real) < limit)):
            merged: list[str] = []
            seen: set[str] = set()
            for p in real + extra:
                n = _norm_text(p)
                if not n or n in seen:
                    continue
                seen.add(n)
                merged.append(_one_line(p))
                if len(merged) >= limit:
                    break
            if merged:
                k.body_points = merged
                if labels_only:
                    c.filled_slides.append(slide.slide_number)
        self._mark_used(c, k)

    def _source_fill(self, c: "_Ctx", slide: Slide, k: _Content, limit: int) -> list[str]:
        skip = set(c.used_on_slide)
        km = _one_line(k.key_message)
        if km and not _is_structural_message(km):
            skip.add(_norm_text(km))
        out: list[str] = []
        for sent in self._source_candidates(c, slide):
            n = _norm_text(sent)
            line = _one_line(sent)
            if not n or n in skip or _is_structural_message(line) or _is_label_point(line, slide):
                continue
            if len(line) < 12:
                continue
            skip.add(n)
            out.append(line)
            if len(out) >= limit:
                break
        if not out and km and not _is_structural_message(km) and not _is_label_point(km, slide):
            out = [km]
        return out

    @staticmethod
    def _source_candidates(c: "_Ctx", slide: Slide) -> list[str]:
        cands: list[str] = []
        ref = slide.source_reference
        section = next((s for s in c.plan.sections if s.id == slide.section_id), None)
        ids = list(section.source_section_ids) if section else []
        if ref and ref.section_id and ref.section_id not in ids:
            ids.insert(0, ref.section_id)
        titles = [ref.section_title] if ref and ref.section_title else []
        if c.material and c.material.sections:
            for sec in c.material.sections:
                take = (sec.id in ids) or any(t and t in (sec.title or "") for t in titles)
                if take and sec.text:
                    cands.extend(_sentences(sec.text))
        if section:
            cands.extend(section.source_points)
        if c.analysis:
            by_name = {x.name: x for x in c.analysis.concepts}
            for name in slide.concepts:
                info = by_name.get(name)
                if info and info.description:
                    cands.extend(_sentences(info.description) or [_one_line(info.description)])
            for d in c.analysis.definitions:
                if d.term in slide.concepts and d.definition:
                    cands.extend(_sentences(d.definition) or [_one_line(d.definition)])
            for ip in c.analysis.important_points:
                loc = ip.source_location
                if not ip.text:
                    continue
                if loc and loc.section_id in ids:
                    cands.append(_one_line(ip.text))
            for sec in c.analysis.sections:
                if sec.id in ids and sec.summary:
                    cands.extend(_sentences(sec.summary) or [_one_line(sec.summary)])
        return cands

    @staticmethod
    def _mark_used(c: "_Ctx", k: _Content) -> None:
        for text in (k.key_message, k.explanation, *k.body_points):
            if text and not _is_structural_message(text):
                c.used_on_slide.add(_norm_text(text))

    def _slide_block(self, c: "_Ctx", slide: Slide, k: _Content) -> str:
        t = slide.slide_type.value
        n, total = slide.slide_number, c.spec.slide_count
        speaker = c.profile.speaker_notes.value
        L: list[str] = [f"### 슬라이드 {n}/{total} · {slide.title}"]
        L.append(f"- 섹션: {slide.section_title}")
        L.append(f"- 유형: {_TYPE_LABEL.get(t, t)} ({t})")
        L.append(f"- 예상 설명 시간: {_dur(slide.estimated_explanation_time)}")
        L.append(f"- 학습 목적: {_one_line(slide.learning_purpose)}")
        L.append(f"- 레이아웃: {_LAYOUT.get(t, _LAYOUT['concept'])}")
        if k.kind == "placeholder":
            L.append(
                "- 내용 지시: 강의자료에 이 슬라이드의 내용이 없다. 위 학습 목적에 맞는 일반적인 내용으로 간결하게 채운다"
                "(SOURCE POLICY 범위 안에서, 특정 제품·버전·수치를 단정하지 않는다)."
            )
        L += _labelled("핵심 메시지", k.key_message)
        L += _bullets("본문 항목", k.body_points)
        if k.explanation:
            L += _labelled("설명", k.explanation)
        if k.example:
            L += _labelled("예시(교육용으로 보충한 예시, 원문에 없음)" if k.example_is_made_up else "예시", k.example)
        if k.analogy:
            L += _labelled("비유(이해를 돕는 보충 설명)", k.analogy)
        if k.practice is not None:
            L += self._practice_lines(k.practice)
        if k.code_explanation is not None:
            L += self._code_explanation_lines(k.code_explanation)
        L += self._source_code_lines(c, slide)
        L += self._formula_lines(c, slide)
        if k.quiz is not None:
            L += self._quiz_lines(k.quiz, speaker)
        if k.summary_message:
            L += _labelled("마무리 메시지", k.summary_message)
        L += _labelled("시각 지시", k.visual)
        if slide.slide_type != SlideType.title:
            L.append(
                "- 화면 텍스트: 본문 항목은 이 슬라이드 대본의 문장이다. 반드시 읽히는 완전한 문장으로 넣는다. "
                "대본에 있는 내용을 빼지 않는다. 슬라이드만 보고도 정의를 읽고 관계를 따라갈 수 있게 한다. "
                "개념 이름만 적힌 빈 상자·빈 카드를 만들지 않으며, 장식 이미지가 본문을 대체하지 않는다. "
                "도식의 상자마다 명세 용어 라벨을 붙인다. "
                "화면을 채우는 거대 장식 글자를 넣지 않는다. "
                "'설명한다/정리한다' 같은 진행 지시는 화면에 넣지 않는다."
            )
        if speaker == "none":
            L.append("- 발표자 노트: 작성하지 않는다.")
        elif k.notes:
            if k.notes_are_direction:
                L += _labelled("발표 방향(이 방향에 맞는 짧은 노트를 쓰되 새 사실을 추가하지 않는다)", k.notes)
            else:
                L += _labelled("발표자 노트(PPT 노트란에 그대로 입력)", k.notes)
        ref = _loc(slide.source_reference)
        if ref:
            L.append(f"- 원문 근거(참고용, 슬라이드에 표시하지 않는다): {ref}")
        return "\n".join(L)

    @staticmethod
    def _practice_lines(p) -> list[str]:
        L: list[str] = []
        if p.goal:
            L += _labelled("실습 목표", p.goal)
        L += _bullets("준비물", p.prerequisites)
        L += _numbered("실습 단계", p.steps)
        if p.expected_result:
            L += _labelled("기대 결과", p.expected_result)
        L += _bullets("주의사항", p.cautions)
        return L

    @staticmethod
    def _code_explanation_lines(ce) -> list[str]:
        L: list[str] = []
        if ce.purpose:
            L += _labelled("코드 목적", ce.purpose)
        L += _bullets("핵심 줄 설명", ce.key_lines)
        L += _numbered("실행 흐름", ce.execution_flow)
        L += _bullets("코드 주의사항", ce.cautions)
        return L

    def _source_code_lines(self, c: "_Ctx", slide: Slide) -> list[str]:
        """The source's own code block for a code slide (or a code practice), verbatim."""
        ref = slide.source_reference
        key = (ref.section_id, ref.line, ref.page) if ref else None
        if slide.slide_type not in (SlideType.code, SlideType.practice):
            return []
        block = c.code_blocks.get(key) if key else None
        if slide.slide_type != SlideType.code and block is None:
            return []
        if block is None:
            c.code_missing.append(slide.slide_number)
            c.notes.append(
                f"코드 슬라이드 {slide.slide_number}번의 원문 코드를 찾지 못해 프롬프트에 코드가 없습니다."
            )
            return ["- 원문 코드: (원문 코드를 찾지 못했다. 코드를 새로 만들지 않고 코드 설명만 배치한다.)"]
        code = block.code.strip("\n")
        rows = code.splitlines()
        cut = ""
        if len(rows) > MAX_CODE_LINES:
            cut = f"(원문 코드 {len(rows)}줄 중 앞의 {MAX_CODE_LINES}줄만 표시)"
            rows = rows[:MAX_CODE_LINES]
            c.notes.append(f"슬라이드 {slide.slide_number}번의 코드가 길어 앞의 {MAX_CODE_LINES}줄만 프롬프트에 넣었습니다.")
        code = "\n".join(rows)
        fence = _fence(code)
        lang = block.language or ""
        head = "- 원문 코드(그대로 사용, 수정·추가·재작성 금지)" + (f" {cut}" if cut else "") + ":"
        return [head, f"  {fence}{lang}", *[f"  {ln}" if ln else "" for ln in code.splitlines()], f"  {fence}"]

    @staticmethod
    def _formula_lines(c: "_Ctx", slide: Slide) -> list[str]:
        ref = slide.source_reference
        f = c.formulas.get((ref.section_id, ref.line, ref.page)) if (ref and slide.slide_type == SlideType.formula) else None
        if f is None:
            return []
        return _labelled("원문 수식(그대로 사용)", f.expression)

    @staticmethod
    def _quiz_lines(q, speaker: str) -> list[str]:
        L: list[str] = []
        if q.question:
            L += _labelled("퀴즈 문제", q.question)
        if q.choices:
            L += _numbered("보기", q.choices)
        where = "발표자 노트에만 넣는다(화면에 보이지 않게)" if speaker != "none" else "화면 아래쪽 ‘정답’ 영역에 작게 둔다"
        if q.answer:
            L += _labelled(f"정답 — {where}", q.answer)
        if q.explanation:
            L += _labelled(f"해설 — {where}", q.explanation)
        return L

    # ------------------------------------------------------------------ closing sections
    def _visual(self, c: "_Ctx") -> str:
        p = c.profile
        visual_slides = [s.slide_number for s in c.spec.slides if s.slide_type.value in _VISUAL_TYPES]
        lines = [
            f"- {_VISUAL[p.visual_level.value]}",
            f"- 도식·표가 필요한 슬라이드: {_ranges(visual_slides) if visual_slides else '없음'}"
            + (f" (총 {len(visual_slides)}장)" if visual_slides else ""),
            "- 도식 안의 이름과 용어는 슬라이드 명세에 나온 표기를 그대로 사용한다(번역·약어 변경 금지).",
            "- 모든 슬라이드에서 서체, 색상, 제목 위치, 여백을 일관되게 유지한다. 글자는 강의실 뒤에서도 읽히는 크기로 한다.",
            "- 슬라이드 유형별 기본 배치는 각 슬라이드의 ‘레이아웃’을 따르고, ‘시각 지시’가 있으면 그것이 우선한다.",
            "- 장식용 이미지와 아이콘은 내용을 바꾸거나 새 사실을 암시하지 않는 것만 쓴다. 이미지 안에 글자를 넣지 않는다.",
            "- 어떤 슬라이드도 제목이나 개념 이름만 적힌 빈 상자·빈 카드로 두지 않는다. 본문 항목은 반드시 화면에 읽히는 완전한 문장으로 넣는다. 장식 이미지가 본문을 대체하지 않는다.",
            "- 슬라이드만 봐도 그 장의 정의와 관계를 따라갈 수 있어야 한다. 도식은 본문 옆에 두고 모든 상자에 용어 라벨을 붙인다. 그림만으로 설명하지 않는다.",
            "- 화면을 채우는 거대 장식 글자를 넣지 않는다.",
            "- 슬라이드 하나가 넘치면 글자 크기와 배치를 조정한다. 슬라이드를 나누거나 문장을 줄이지 않는다.",
        ]
        return "\n".join(lines)

    def _speaker_notes(self, c: "_Ctx") -> str:
        lines = [f"- {t}" for t in _NOTES[c.profile.speaker_notes.value]]
        if c.profile.speaker_notes.value != "none":
            lines += [
                "- 발표자 노트는 PPT의 노트란에만 넣고 슬라이드 화면에는 넣지 않는다.",
                "- 명세의 ‘발표자 노트’ 문장이 발표자가 말할 문장이다. 슬라이드 화면의 문장을 노트에 다시 옮겨 적지 않는다.",
                "- ‘발표 방향’으로 표시된 슬라이드는 명세에 노트 문장이 없다. 그 방향에 맞게 짧게 쓰되 명세에 없는 사실을 추가하지 않는다.",
            ]
        return "\n".join(lines)

    def _constraints(self, c: "_Ctx") -> str:
        a, m = c.analysis, c.material
        lines: list[str] = []
        title = (m.title if m else None) or (a.title if a else None) or c.plan.title
        lines.append(f"- 원문 제목: {title}")
        if m:
            kind = f"{m.file_type}" + (f", {m.page_count}쪽" if m.page_count else "")
            lines.append(f"- 원문 파일: {m.filename} ({kind})")
        if m and m.images_metadata:
            lines.append(
                f"- 원문에는 이미지가 {len(m.images_metadata)}개 있으나 이 프롬프트에는 포함되지 않았다. 원문 이미지를 가져온다고 가정하지 않고 시각 지시에 따라 새로 그린다."
            )
        if m and m.tables:
            lines.append(f"- 원문에 표가 {len(m.tables)}개 있다. 표 내용은 명세의 문장에 이미 반영되어 있으므로 새로 만들지 않는다.")
        cons = c.plan.source_constraints
        if cons:
            lines.append("- 원문이 정한 강의 범위 제약(아래 제약과 충돌하는 내용은 화면과 노트에 넣지 않는다):")
            for k in cons:
                ref = _loc(k.source_reference)
                lines.append(f"  - {_one_line(k.text)}" + (f" (원문 {ref})" if ref else ""))
        else:
            lines.append("- 원문이 별도로 정한 강의 범위 제약은 없다.")
        outline = self._outline(a, m)
        if outline:
            lines.append("- 원문 구성(참고용 개요):")
            lines += [f"  {o}" for o in outline]
        lines.append("- 원문에 없는 내용은 SOURCE POLICY가 허용하는 범위를 넘어 추가하지 않는다.")
        return "\n".join(lines)

    @staticmethod
    def _outline(a, m) -> list[str]:
        heads: list[tuple[int, str]] = []
        if m and m.headings:
            heads = [(h.level, h.text) for h in m.headings]
        elif a and a.sections:
            heads = [(s.level, s.title) for s in a.sections]
        if not heads:
            return []
        base = min(lv for lv, _ in heads)
        out = [f"{'  ' * max(0, min(lv - base, 3))}- {_one_line(t)}" for lv, t in heads]
        if len(out) > MAX_OUTLINE_LINES:
            extra = len(out) - MAX_OUTLINE_LINES
            out = out[:MAX_OUTLINE_LINES] + [f"- … 외 {extra}개 항목"]
        return out

    # ------------------------------------------------------------------ validation / options
    @staticmethod
    def _validate(text: str, sections: list[tuple[str, str]], spec: SlideSpecification) -> PromptValidation:
        v = PromptValidation()
        names = _HEADING.findall(text)
        if names != list(SECTION_NAMES):
            v.all_sections_present = False
            v.errors.append(f"섹션이 스펙과 다릅니다: {names}")

        total = spec.slide_count
        if f"슬라이드 수: {total}장" not in text or f"정확히 {total}장" not in text:
            v.slide_count_matches = False
            v.errors.append("프롬프트가 슬라이드 수를 올바르게 밝히지 않았습니다.")

        slide_part = dict(sections).get("SLIDE SPECIFICATION", "")
        heads = _SLIDE_HEADING.findall(slide_part)
        if [int(h[0]) for h in heads] != list(range(1, total + 1)) or any(int(h[1]) != total for h in heads):
            v.all_slides_present_once_in_order = False
            v.errors.append("슬라이드 블록이 1..N 순서로 한 번씩 들어 있지 않습니다.")

        blocks = re.split(r"(?m)^(?=### 슬라이드 )", slide_part)[1:]
        if len(blocks) == total:
            for sl, b in zip(spec.slides, blocks):
                if f"· {sl.title}\n" not in b + "\n" or _dur(sl.estimated_explanation_time) not in b:
                    v.slide_titles_present = False
                    v.errors.append(f"슬라이드 {sl.slide_number}번의 제목 또는 시간이 프롬프트와 다릅니다.")
                if "- 학습 목적:" not in b or "- 핵심 메시지:" not in b or "- 시각 지시:" not in b:
                    v.no_empty_slide_block = False
                    v.errors.append(f"슬라이드 {sl.slide_number}번 블록에 필수 항목이 없습니다.")
        else:
            v.slide_titles_present = v.no_empty_slide_block = False
            v.errors.append("슬라이드 블록 수가 명세와 다릅니다.")

        if sum(s.estimated_explanation_time for s in spec.slides) != spec.duration_minutes * 60:
            v.duration_matches = False
            v.errors.append("슬라이드 시간의 합이 강의 시간과 다릅니다.")
        return v

    @staticmethod
    def _options(p: LectureProfile, spec: SlideSpecification) -> dict[str, str]:
        return {
            "audience_level": p.audience_level.value, "difficulty": p.difficulty.value,
            "lecture_type": p.lecture_type.value, "explanation_depth": p.explanation_depth.value,
            "source_policy": p.source_policy.value, "slide_density": p.slide_density.value,
            "visual_level": p.visual_level.value, "example_level": p.example_level.value,
            "practice_level": p.practice_level.value, "code_level": p.code_level.value,
            "quiz_mode": p.quiz_mode.value, "lecture_tone": p.lecture_tone.value,
            "speaker_notes": p.speaker_notes.value, "duration_minutes": str(p.duration_minutes),
        }


@dataclass
class _Ctx:
    profile: LectureProfile
    plan: LecturePlan
    spec: SlideSpecification
    analysis: SourceAnalysis | None
    material: SourceMaterial | None
    enriched: EnrichedSlideSpecification | None
    by_number: dict[int, EnrichedSlide]
    code_blocks: dict[tuple, CodeBlock]
    formulas: dict[tuple, object]
    warnings: list[str]
    notes: list[str] = field(default_factory=list)
    code_missing: list[int] = field(default_factory=list)
    used_on_slide: set[str] = field(default_factory=set)
    filled_slides: list[int] = field(default_factory=list)
    script: LectureScript | None = None


def _ranges(numbers: list[int]) -> str:
    """[3, 4, 5, 9] -> '3–5, 9번'"""
    nums = sorted(set(numbers))
    out: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"{nums[i]}" if i == j else f"{nums[i]}–{nums[j]}")
        i = j + 1
    return ", ".join(out) + "번"
