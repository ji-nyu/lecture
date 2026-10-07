"""Prompt text for the content-writing LLM (provider independent).

The system prompt is assembled from the rules that apply to THIS lecture profile only
(audience, difficulty, lecture type, source policy, notes, visuals) plus the guide of the
slide types that are asked for, which keeps the prompt short and the instructions
unambiguous. The user message is the JSON request: profile, the slide, its section, the
relevant source snippets and the scope notes - never the whole document.

`PROMPT_VERSION` is part of every cache key and of the stored result: change the text
below => bump the version.
"""

from __future__ import annotations

import json

from ..services.text_utils import is_exclusion_note
from .base import EnrichmentRequest

PROMPT_VERSION = "enrich-v3"

_BASE = """당신은 강의 슬라이드의 '콘텐츠 작성자'입니다. 강의 구조(섹션, 슬라이드 수·순서·번호·유형, 시간)는 규칙 엔진이 이미 확정했고 당신은 그것을 바꿀 수 없습니다. 당신의 일은 각 슬라이드에 들어갈 '내용'을 쓰는 것입니다.

반드시 지킬 것:
1. 입력의 slide_number를 그대로 되돌려 준다. 슬라이드를 추가·삭제·병합·분할·재정렬하지 않는다.
2. 제공된 source_context(원문 발췌)와 강의 프로필 안에서만 작업한다. source_context에 없는 내용을 원문에 있는 것처럼 쓰지 않는다.
3. allowed_fields에 있는 필드만 작성한다. 나머지는 null 또는 빈 배열로 둔다.
4. 출력은 지정된 JSON 스키마의 JSON 객체 하나뿐이다. 마크다운, 코드펜스, 설명 문장을 붙이지 않는다.
5. 원문이 말한 것보다 강하게 쓰지 않는다('항상', '반드시', '가장 좋다' 등). 원문의 부정·조건('~하지 않는다', '~일 수 있다')을 뒤집거나 빼지 않는다.
6. 원문에 없는 숫자, 버전, 경로, 명령어, 제품명을 만들지 않는다.
7. 한국어로, 실제 강의에서 말하듯 자연스럽고 정확한 문장으로 쓴다. 같은 문장이나 표현을 반복하지 않고 '이(가)', '은(는)' 같은 기계적 표기를 쓰지 않는다. 슬라이드 글(body_points 등)은 짧게, 발표자 노트는 말하듯 자연스럽게 쓴다.
8. provenance에는 작성한 각 필드가 어디에서 왔는지 기록한다(작성하지 않은 필드는 null):
   source_grounded(원문에 그대로 있음) / paraphrased_source(원문을 같은 의미로 다시 씀) /
   llm_explanation(원문 개념을 이해시키기 위한 설명) / llm_example(교육용으로 만든 예시) /
   llm_inference(원문에서 추론한 내용) / suggested(작성하지 못해 비워 둠, 이 경우 값은 null).
   서버가 원문과 비교해 이 표시를 다시 검증하므로 사실대로 적는다.
9. visual_instruction은 슬라이드를 그리는 도구(Genspark)에게 주는 구체적인 지시문이다. 한국어로, ① 등장하는 구체적 요소(개념·구성 요소 이름) ② 요소 사이의 관계와 방향(예: A → B 화살표) ③ 배치 또는 도식 종류(왼쪽/오른쪽/중앙, 표, 흐름도, 계층도 등)를 반드시 포함한다.
   나쁜 예: "그림을 사용한다." 좋은 예: "Broker를 중앙에 배치하고 왼쪽에 Publisher, 오른쪽에 Subscriber를 둔다. Publisher → Broker → Subscriber 방향의 화살표로 Topic 기반 메시지 흐름을 표현한다."
   실제 이미지를 만들지 않는다.
10. display_title과 key_message는 절대 비우지 않는다. 슬라이드 입력에 이미 있는 값(planner가 정한 제목·핵심 메시지)을 기본으로 그대로 쓰고 source_grounded로 표시한다. source_policy가 엄격해도 이 두 필드는 null이나 suggested로 두지 않는다."""

_POLICY = {
    "source_only": (
        "source_policy=source_only: 원문에 없는 새로운 사실을 추가하지 않는다. "
        "허용: 원문 문장의 요약·재서술·쉬운 문장으로 바꾸기·원문 안의 항목끼리 비교. "
        "금지: 외부 사실, 외부 예제, 원문에 없는 수치·명령어·코드·기술 세부사항, 비유. "
        "원문이 부족해 쓸 수 없는 필드는 null로 두고 provenance에 suggested로 표시한다. "
        "발표자 노트와 visual_instruction도 원문에 있는 요소만 사용한다."
    ),
    "source_first": (
        "source_policy=source_first: 원문을 중심으로 쓴다. 원문 의미를 쉬운 말로 풀어쓰고, 교육에 필요한 "
        "일반적인 배경 설명과 간단한 예시를 더할 수 있다. 원문과 충돌하면 안 되고, 특정 버전·경로·실행 환경·"
        "수치를 추측하지 않는다. 더한 설명·예시는 provenance에 llm_explanation / llm_example로 기록한다."
    ),
    "expanded": (
        "source_policy=expanded: 원문을 바탕으로 외부 일반 지식, 추가 사례, 교육용 비유를 더할 수 있다. "
        "원문에서 온 내용과 직접 더한 내용을 provenance(llm_explanation / llm_example / llm_inference)로 구분한다. "
        "원문과 충돌하면 안 된다."
    ),
}

_AUDIENCE = {
    "general": "청중=일반인: 전문용어를 최소화하고 일상적인 비유를 쓴다. 배경지식을 가정하지 않는다.",
    "high_school": "청중=고등학생: 전문용어는 처음 나올 때 풀어 쓰고, 구체적인 사례를 들며 지나친 세부는 뺀다.",
    "university_beginner": (
        "청중=대학 초급: 전문용어는 처음 나올 때 쉬운 말로 풀어 준다. 문장은 짧고 쉽게, 기본 개념 중심으로 쓴다. "
        "(허용되는 경우) 일상적인 비유를 하나 곁들인다. 발표자 노트는 상세하게, 도입·전환·이해 확인 멘트까지 포함한다."
    ),
    "university_intermediate": (
        "청중=대학 중급: 기본 용어는 한 줄로 짚고 넘어가며, 동작 원리와 구성 요소 사이의 상호작용을 중심으로 쓴다. "
        "적당한 기술 용어를 그대로 쓰고, 실습·수행으로 이어지는 확인 포인트를 붙인다. 발표자 노트는 짧고 구체적으로 쓴다."
    ),
    "university_advanced": "청중=대학 고급: 기술 세부사항과 trade-off를 포함하고 구현 관점 설명을 할 수 있다.",
    "graduate": "청중=대학원: 기술 세부사항, trade-off, 한계를 포함하고 구현 관점 설명을 한다.",
    "professional": (
        "청중=실무자: 기본 정의는 반복하지 않고 한 줄로 끝낸다. 아키텍처 관점, 설계 판단 포인트, trade-off, 한계, "
        "운영 관점을 중심으로 쓴다(단, source_policy가 허용하는 근거 안에서). 비유는 쓰지 않는다. 발표자 노트는 간결하게 쓴다."
    ),
}

_DIFFICULTY = {
    "introductory": "난이도=입문: 짧은 설명, 쉬운 사례, 수식과 복잡한 세부를 최소화한다.",
    "beginner": "난이도=초급: 기본 개념 중심으로 단계적으로 설명한다.",
    "intermediate": "난이도=중급: 원리와 동작 방식을 포함하고 기술 용어를 쓸 수 있다.",
    "advanced": "난이도=고급: 내부 메커니즘, trade-off, 한계, 세부 구현을 다루고 필요하면 수식·코드를 설명한다.",
}

_LECTURE_TYPE = {
    "theory": "강의 유형=이론: 원리와 개념 설명을 강화한다(definition/concept 중심).",
    "example_based": "강의 유형=예제 중심: 개념마다 가능한 경우 예제를 연결한다.",
    "practice": "강의 유형=실습: 실제 수행 단계를 구체화하고 각 개념을 '무엇을 하고 무엇을 확인하는가'로 연결한다(원문·source_policy 준수).",
    "mixed": "강의 유형=혼합: 이론, 예시, 실습을 균형 있게 구성한다.",
    "exam_preparation": "강의 유형=시험 대비: 핵심 개념, 비교, 자주 헷갈리는 부분, 퀴즈, 요약 중심으로 쓴다.",
}

_NOTES = {
    "none": "speaker_notes=none: presenter_notes를 작성하지 않는다(null).",
    "concise": "speaker_notes=concise: presenter_notes는 핵심 설명 {min}~{max}문장.",
    "full": (
        "speaker_notes=full: presenter_notes는 이 슬라이드의 예상 설명 시간({sec}초)에 맞는 상세 대본. "
        "분량은 약 {target}자, 최대 {maxc}자(말하는 속도 기준). 정확한 글자 수보다 자연스러운 흐름이 우선이다."
    ),
}

_VISUAL = {
    "low": "visual_level=low: 텍스트 중심. 표나 도식은 꼭 필요한 최소한만 지시한다.",
    "medium": "visual_level=medium: 비교표, 구조도, 아이콘, workflow를 적절히 지시한다.",
    "high": "visual_level=high: 가능한 모든 주요 슬라이드에 시각적 표현(도식·아이콘·강조)을 지시한다.",
}

_TYPES = {
    "title": "title: 강의 제목 슬라이드. key_message는 강의를 한 줄로 소개하고, 발표자 노트는 인사와 강의 소개.",
    "agenda": "agenda: 학습 목표와 구성. body_points에 목표(objectives)와 진행 순서를 짧게 정리한다.",
    "definition": "definition: 원문의 정의를 정확히 옮기고 explanation에서 쉬운 말로 풀어쓴다. body_points는 정의의 핵심 요소.",
    "concept": "concept: 핵심 포인트 3~5개(body_points), explanation에서 왜 필요한지·어떻게 동작하는지를 설명한다.",
    "comparison": "comparison: 비교 대상과 기준을 분명히 하고 항목별 차이를 body_points로 쓴다(원문 안의 항목끼리).",
    "architecture": "architecture: 구성 요소와 각 역할, 데이터·제어 흐름의 방향을 body_points로 쓴다. visual_instruction에 배치와 화살표 방향을 구체적으로 쓴다.",
    "workflow": "workflow: 단계 순서와 각 단계의 입력·출력을 body_points로 쓴다. visual_instruction은 흐름도로 지시한다.",
    "diagram": "diagram: 구성 요소와 관계를 body_points로 쓰고 visual_instruction에 도식의 배치를 구체적으로 쓴다.",
    "example": "example: 원문의 예시를 우선 사용하고 상황→동작→결과 순으로 쓴다. 원문에 예시가 없으면 source_policy가 허용하는 범위에서만 쓴다.",
    "practice": (
        "practice: practice_instruction(goal, prerequisites, steps, expected_result, cautions)을 쓴다. steps는 자연스러운 수행 순서, "
        "expected_result는 결과를 확인하는 방법. 원문에 없는 환경·패키지·버전·경로를 가정하지 않는다."
    ),
    "code": "code: code_explanation(purpose, key_lines, execution_flow, cautions)을 쓴다. 원문에 있는 코드만 설명하고 새 코드를 만들지 않는다.",
    "quiz": "quiz: quiz_content(question, choices, answer, explanation). answer는 choices 중 하나이고 원문에서 확인할 수 있는 문제만 낸다.",
    "summary": "summary: summary_message 한 문장과 핵심 body_points 3~5개로 앞에서 다룬 내용을 정리한다. 새로운 내용을 추가하지 않는다.",
}

_SAFETY = (
    "실습·코드 안전: 위험하거나 파괴적인 명령(삭제, 포맷, sudo, 시스템 종료 등)을 만들지 않는다. "
    "시스템 설정 변경이 필요하면 '[시스템 설정 변경]'으로 명확히 표시한다. 원문에 OS·버전·의존성이 없으면 "
    "임의로 특정하거나 실행 환경을 추측하지 않는다."
)


def _scope_block(scope_notes: list[str]) -> str:
    """The scope notes as explicit instructions (they are also part of the JSON request)."""
    out_of_scope = [n for n in scope_notes if is_exclusion_note(n)]
    guidance = [n for n in scope_notes if not is_exclusion_note(n)]
    lines: list[str] = []
    if out_of_scope:
        lines.append("OUT OF SCOPE (이 강의 범위에서 제외된 내용):")
        lines += [f"- {n}" for n in out_of_scope]
        lines.append(
            "위 내용은 이 강의 범위에서 제외되어 있으므로 새로운 설명, 세부 내용, 예시로 만들지 않는다. "
            "발표자 노트와 visual_instruction에서도 마찬가지다."
        )
    if guidance:
        lines.append("범위 지침(이 범위 안에서만 설명한다):")
        lines += [f"- {n}" for n in guidance]
    return "\n".join(lines)


def build_system_prompt(req: EnrichmentRequest, batch: bool = False, types: list[str] | None = None) -> str:
    p, n = req.profile, req.notes
    if batch and n.mode != "none":  # each slide has its own budget in its own `notes`
        notes_rule = f"speaker_notes={n.mode}: 슬라이드마다 자신의 notes 예산(문장 수·글자 수)을 따른다."
    else:
        notes_rule = _NOTES[n.mode].format(
            min=n.min_sentences, max=n.max_sentences, sec=req.slide.estimated_explanation_time,
            target=n.target_chars, maxc=n.max_chars,
        )
    slide_types = types or [req.slide.slide_type]
    type_guide = "슬라이드 유형별 작성 방법:\n" + "\n".join(f"- {_TYPES[t]}" for t in slide_types if t in _TYPES)
    parts = [
        _BASE,
        "[강의 프로필]",
        _POLICY.get(p.source_policy, ""),
        _AUDIENCE.get(p.audience_level, ""),
        _DIFFICULTY.get(p.difficulty, ""),
        _LECTURE_TYPE.get(p.lecture_type, ""),
        notes_rule,
        _VISUAL.get(p.visual_level, ""),
        f"어조={p.lecture_tone}, 설명 깊이={p.explanation_depth}, 슬라이드 밀도={p.slide_density}, "
        f"예시 수준={p.example_level}, 실습 수준={p.practice_level}, 코드 수준={p.code_level}, 퀴즈 방식={p.quiz_mode}.",
        (
            "body_points 개수와 글자 수는 슬라이드마다 자신의 limits를 따른다."
            if batch
            else f"body_points는 {req.limits.body_points_min}~{req.limits.body_points_max}개, "
            f"각 {req.limits.point_chars}자 이내."
        ),
        type_guide,
        _scope_block(req.scope_notes),
    ]
    if any(t in ("practice", "code") for t in slide_types) or p.lecture_type == "practice":
        parts.append(_SAFETY)
    parts.append(
        "출력은 JSON 스키마의 모든 키를 가진 JSON 객체다: slide_number, display_title, key_message, body_points[], "
        "explanation, example, analogy, practice_instruction{goal,prerequisites[],steps[],expected_result,cautions[]}, "
        "code_explanation{purpose,key_lines[],execution_flow[],cautions[]}, visual_instruction, presenter_notes, "
        "quiz_content{question,choices[],answer,explanation}, summary_message, provenance{필드명: 값 또는 null}. "
        "이 슬라이드에 필요 없는 필드는 null(목록은 [])로 둔다."
    )
    return "\n".join(x for x in parts if x)


def build_user_message(req: EnrichmentRequest) -> str:
    return json.dumps(req.model_dump(mode="json"), ensure_ascii=False)


def build_batch_system_prompt(reqs: list[EnrichmentRequest]) -> str:
    types = list(dict.fromkeys(r.slide.slide_type for r in reqs))
    base = build_system_prompt(reqs[0], batch=True, types=types)
    return (
        base
        + '\n여러 슬라이드를 한 번에 처리한다. 답은 {"slides": [슬라이드별 JSON 객체, ...]} 형식이며 '
        "요청에 있는 slide_number마다 정확히 하나씩 답한다(추가·누락 금지)."
    )


def build_batch_user_message(reqs: list[EnrichmentRequest]) -> str:
    first = reqs[0]
    payload = {
        "lecture_title": first.lecture_title,
        "profile": first.profile.model_dump(mode="json"),
        "scope_notes": first.scope_notes,
        "slides": [
            r.model_dump(mode="json", exclude={"profile", "lecture_title", "scope_notes", "prompt_version"})
            for r in reqs
        ],
    }
    return json.dumps(payload, ensure_ascii=False)
