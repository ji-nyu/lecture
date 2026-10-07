"""MockLLMClient: an offline, deterministic stand-in for the content-writing LLM.

It makes every STAGE 6A test (and the demo) run without a network or an API key.

WHAT IT IS - and is not: a template writer. It reads ONLY the JSON request (the same
information a real LLM gets: profile, slide, section, source snippets, scope notes,
allowed fields, size limits) and writes the structured answer the prompt asks for. It
adapts to audience / difficulty / lecture type / speaker notes / visual level / source
policy, reuses verbatim source sentences, and labels every field with its provenance.
It does NOT have the fluency, insight or accuracy of a real model: what it proves is that
the pipeline (request -> schema -> validation -> result) and the adaptation rules work,
not the prose quality of a real provider.

Like a well-behaved model it obeys the rules in the prompt (no examples under source_only,
no notes when speaker_notes=none, ...). Misbehaving models are simulated in the tests with
scripted clients; the GroundingValidator does not rely on the mock being polite.
"""

from __future__ import annotations

from typing import Any

from ..services.text_utils import clip_chars, normalize, split_sentences
from .base import ContentLLMClient, EnrichmentRequest

BEGINNER = {"general", "high_school", "university_beginner"}
EXPERT = {"university_advanced", "graduate", "professional"}


def tier_of(req: EnrichmentRequest) -> int:
    """0 = beginner wording, 1 = intermediate, 2 = expert wording."""
    p = req.profile
    if p.audience_level in BEGINNER or p.difficulty == "introductory":
        return 0
    if p.audience_level in EXPERT or p.difficulty == "advanced":
        return 2
    return 1


class MockLLMClient(ContentLLMClient):
    provider = "mock"
    model = "mock-content-writer-v1"

    def __init__(self) -> None:
        self.calls: list[int] = []  # slide numbers, one entry per slide asked

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def enrich_slide(self, request: EnrichmentRequest) -> dict[str, Any]:
        self.calls.append(request.slide.slide_number)
        return write_slide(request)


# ---------------------------------------------------------------------------
class _C:
    """Everything the writer knows about one slide."""

    def __init__(self, req: EnrichmentRequest):
        self.req = req
        self.s = req.slide
        self.p = req.profile
        self.tier = tier_of(req)
        self.so = self.p.source_policy == "source_only"
        self.type = self.s.slide_type
        self.name = self.s.concepts[0] if self.s.concepts else self.s.title
        self.names = self.s.concepts or [self.s.title]
        self.snips = req.source_context
        self.first = self._first_sentence()
        # a follow-up slide of the same concept ("MQTT: 이해 확인"): it has its own purpose
        self.facet = self.type == "concept" and ":" in self.s.title

    def texts(self, *kinds: str, concept: str | None = None) -> list[str]:
        return [
            x.text for x in self.snips
            if x.kind in kinds and (concept is None or x.concept == concept)
        ]

    def _first_sentence(self) -> str | None:
        for kinds in (("definition",), ("description",), ("point",)):
            t = self.texts(*kinds, concept=self.name)
            if t:
                return t[0]
        for kinds in (("definition",), ("description",), ("point",)):
            t = self.texts(*kinds)
            if t:
                return t[0]
        return None

    def sentence_of(self, name: str) -> str | None:
        for kinds in (("definition",), ("description",), ("point",)):
            t = self.texts(*kinds, concept=name)
            if t:
                return t[0]
        return None

    def related(self) -> list[str]:
        rel = [r for rels in self.s.concept_relations.values() for r in rels]
        rel += [c for c in self.req.section.concepts if c not in self.names]
        seen: dict[str, None] = {}
        for r in rel:
            if r not in self.names:
                seen[r] = None
        return list(seen)[:3]

    @property
    def rel_text(self) -> str:
        return ", ".join(self.related()) or "related concepts"


def _short(text: str, n: int) -> str:
    return clip_chars(text, n)


# ---- titles / messages -----------------------------------------------------
def _display_title(c: _C) -> str:
    n = c.name
    t = c.type
    if c.facet:
        return c.s.title
    if t == "practice":
        return f"{c.s.title} — " + {0: "따라 하기", 1: "직접 해 보기", 2: "검증과 변형"}[c.tier]
    if t in ("definition",):
        return {0: f"{n}란 무엇일까요?", 1: f"{n}의 정의", 2: f"{n}: 정의와 전제"}[c.tier]
    if t in ("concept", "architecture", "diagram", "workflow"):
        return {0: f"{n} 쉽게 이해하기", 1: f"{n}의 구조와 동작", 2: f"{n}: 설계 관점과 한계"}[c.tier]
    if t == "example":
        return {0: f"{n}, 예시로 살펴보기", 1: f"{n} 적용 예시", 2: f"{n} 사례 분석"}[c.tier]
    if t == "quiz":
        return "이해 확인 퀴즈"
    if t == "summary":
        if not c.s.concepts:  # e.g. "학습 목표 점검": the planner's own title is the right one
            return c.s.title
        return {0: "오늘 배운 내용 정리", 1: "핵심 정리", 2: "핵심 정리와 남은 쟁점"}[c.tier]
    return c.s.title


def _key_message(c: _C) -> tuple[str, str]:
    """(text, provenance)"""
    t = c.type
    if t == "title":
        lead = {
            0: "처음 배우는 분도 따라올 수 있도록 핵심부터 차근차근 살펴봅니다.",
            1: "핵심 개념의 구조와 동작을 이해하고 활용합니다.",
            2: "설계와 운영 관점에서 핵심 쟁점을 짚어 봅니다.",
        }[c.tier]
        return f"{c.req.lecture_title}: {lead}", "llm_explanation"
    if t == "agenda":
        return "학습 목표와 진행 순서를 먼저 확인합니다.", "llm_explanation"
    if t == "summary":
        if not c.s.concepts:
            return "처음에 세운 학습 목표를 하나씩 점검합니다.", "llm_explanation"
        return f"핵심 개념을 한 흐름으로 정리합니다: {', '.join(c.names[:4])}", "llm_explanation"
    if t == "quiz":
        return "배운 내용을 스스로 확인합니다.", "llm_explanation"
    if t == "practice":
        tail = {0: " 한 단계씩 천천히 따라 합니다.", 1: "", 2: " 결과를 검증하고 조건을 바꿔 차이를 분석합니다."}[c.tier]
        return c.s.key_message + ("" if c.so else tail), "llm_explanation"
    s = c.first
    if s is None or c.facet:
        return c.s.key_message, "llm_explanation"
    if c.tier == 0:
        return f"쉽게 말해, {_short(s, 90)}", "paraphrased_source"
    if c.tier == 2:
        if c.so:
            return _short(s, 110) + " 이 내용이 다른 요소와 맺는 관계를 함께 본다.", "paraphrased_source"
        return _short(s, 110) + " 도입할 때의 제약과 trade-off도 함께 본다.", "llm_explanation"
    return _short(s, 120), "source_grounded"


# ---- body points / explanation ------------------------------------------------
_FLAVOR = {
    "theory": "원리를 먼저 이해한 뒤 사례로 확인합니다.",
    "practice": "이 내용은 뒤의 실습에서 직접 확인합니다.",
    "mixed": "이론을 확인한 뒤 짧은 예시로 이어 갑니다.",
    "example_based": "아래 예시와 연결해 이해합니다.",
    "exam_preparation": "시험에서는 정의와 역할을 구분하는 문제가 자주 나오므로 용어를 정확히 구분해 두세요.",
}


def _source_points(c: _C, limit: int) -> list[str]:
    out: list[str] = []
    for name in c.names:
        s = c.sentence_of(name)
        if s and _short(s, limit) not in out:
            out.append(_short(s, limit))
    for x in c.snips:
        if x.kind in ("point", "description", "definition", "example"):
            t = _short(x.text, limit)
            if t not in out:
                out.append(t)
    return out


def _concept_points(c: _C) -> tuple[list[str], str]:
    lim = c.req.limits
    if c.facet:  # keep the planner's points for this slide and add the wording for the audience
        pts = [k for k in c.s.key_points if k.strip()]
        if not c.so:
            pts.append({
                0: "어렵게 느껴지면 앞 슬라이드의 정의 문장으로 돌아가 다시 읽는다",
                1: "앞 슬라이드의 내용과 어떻게 이어지는지 확인한다",
                2: "이 개념이 다른 구성 요소에 미치는 영향과 제약을 점검한다",
            }[c.tier])
        return pts[: lim.body_points_max], "llm_explanation"
    src = _source_points(c, min(lim.point_chars, 100))
    n = c.name
    pts: list[str] = []
    if c.so:
        pts = src[: lim.body_points_max]
        return pts, "paraphrased_source"
    if c.tier == 0:
        pts = [f"먼저 '{n}'이(가) 무엇인지부터 확인한다", *src[:2], f"'{n}'이(가) 하는 일을 한 문장으로 말해 본다"]
    elif c.tier == 1:
        rel = c.related()
        pts = [*src[:2]]
        if rel:
            pts.append(f"연결: '{n}'은(는) {', '.join(rel)}와(과) 함께 동작한다")
        pts += src[2:3]
        pts.append(f"확인할 점: '{n}'이(가) 다른 요소와 어떻게 이어지는지")
    else:
        pts = [*src[:2], "동작 원리와 내부 구조를 구현 관점에서 확인한다",
               "설계·운영 관점에서 신뢰성과 확장성, 보안을 점검한다", "한계와 제약을 먼저 정리한다"]
    if c.p.lecture_type == "practice":
        pts.insert(min(2, len(pts)), f"실습에서 확인할 것: '{n}'의 동작 결과")
    if c.p.lecture_type == "exam_preparation":
        pts.append(f"자주 헷갈리는 부분: '{n}'의 정의와 역할 구분")
    pts = [p for p in pts if p][: lim.body_points_max]
    while len(pts) < lim.body_points_min:
        pts.append(f"'{n}'의 역할을 자료의 문장으로 다시 확인한다")
    return pts, "llm_explanation"


def _explanation(c: _C) -> tuple[str | None, str]:
    s = c.first
    n = c.name
    if c.facet:
        tail = "" if c.so else {
            0: " 어려운 부분은 천천히, 한 번에 하나씩 확인합니다.",
            1: " 앞에서 배운 내용과 연결해 확인합니다.",
            2: " 한계와 예외 상황까지 함께 점검합니다.",
        }[c.tier]
        return c.s.learning_purpose + tail, "llm_explanation"
    if s is None:
        return None, "suggested"
    flavor = _FLAVOR.get(c.p.lecture_type, "")
    if c.tier == 0:
        text = f"{s} 처음 접하는 용어이므로 천천히 짚어 봅시다. 지금은 '{n}'이(가) 어떤 역할을 하는지만 기억해도 충분합니다."
    elif c.tier == 1:
        rel = c.related()
        text = f"{s} 이 슬라이드에서는 '{n}'이(가) 어떻게 동작하고 다른 개념과 어떻게 연결되는지에 초점을 둡니다."
        if rel:
            text += f" 함께 볼 개념은 {', '.join(rel)}입니다."
    else:
        text = (
            f"{s} 구현과 운영 관점에서는 '{n}'이(가) 맡는 책임의 경계와, 이를 선택했을 때 감수해야 하는 제약을 "
            "함께 검토해야 합니다."
        )
    if c.so:
        text = f"{s} 이 내용이 다른 요소와 맺는 관계를 자료에 맞게 정리합니다."
    elif flavor:
        text += " " + flavor
    return text, "llm_explanation"


_ANALOGY = {
    "definition": "낱말의 뜻을 사전에서 찾아 확인하는 일과 비슷합니다.",
    "concept": "학교에서 구성원마다 맡은 역할이 있고 서로 연락을 주고받으며 일을 나누는 모습에 비유할 수 있습니다.",
    "architecture": "건물의 층별 안내도처럼 어디에 무엇이 있는지 한눈에 보여 주는 지도라고 생각하면 쉽습니다.",
    "diagram": "건물의 층별 안내도처럼 어디에 무엇이 있는지 한눈에 보여 주는 지도라고 생각하면 쉽습니다.",
    "workflow": "요리 레시피처럼 순서를 따라 하나씩 진행하는 과정이라고 생각하면 쉽습니다.",
}


def _example(c: _C) -> tuple[str | None, str]:
    src = c.texts("example", concept=c.name) or c.texts("example")
    if src:
        return f"자료의 예시: {_short(src[0], 140)}", "paraphrased_source"
    if c.so:
        return None, "suggested"
    n = c.name
    text = {
        0: f"예를 들어, 수업 시간에 '{n}'을(를) 처음 배우는 학생이 자신의 말로 한 문장씩 설명해 보는 상황을 떠올려 봅시다.",
        1: f"예를 들어, 실습 과제에서 '{n}'이(가) 어느 단계에 쓰이는지 표시해 보는 상황을 생각해 볼 수 있습니다.",
        2: f"예를 들어, 운영 중인 시스템에서 '{n}'이(가) 응답하지 않을 때 어떤 부분이 영향을 받는지 점검하는 상황입니다.",
    }[c.tier]
    return text + _EXAMPLE_TAIL.get(c.p.lecture_type, ""), "llm_example"


_EXAMPLE_TAIL = {
    "theory": " 이 예시로 개념 사이의 관계를 정리해 봅니다.",
    "practice": " 이 상황을 실습 화면에서 직접 재현해 봅니다.",
    "example_based": " 이 예시를 이후 설명의 기준으로 삼습니다.",
    "exam_preparation": " 시험에서는 이런 상황에서 개념을 구분하는 문제가 자주 나옵니다.",
    "mixed": "",
}


# ---- visual instructions (English: read by the renderer) ------------------------
def _visual(c: _C) -> str:
    v = c.p.visual_level
    n = c.name
    rel = c.rel_text
    t = c.type
    tier = {0: " Use large friendly icons and very short labels.",
            1: "",
            2: " Add a callout that lists constraints and trade-offs."}[c.tier]
    if t == "title":
        return "Centered title slide: the lecture title large, the audience and duration as a subtitle." + (
            " Add a subtle topic illustration." if v != "low" else "")
    if t == "agenda":
        return "Numbered list of the learning objectives on the left" + (
            ", a horizontal timeline of the sections on the right." if v != "low" else ".")
    if t == "comparison":
        base = {
            "low": f"Simple two-column table comparing {', '.join(c.names[:3])} row by row; no decoration.",
            "medium": f"Comparison table with one column per item ({', '.join(c.names[:3])}) and one row per criterion; highlight the differences.",
            "high": f"Side-by-side comparison cards with an icon per item ({', '.join(c.names[:3])}), a criteria table below and color-coded differences.",
        }[v]
        return base + tier
    if t in ("architecture", "diagram"):
        base = {
            "low": f"A minimal box-and-arrow sketch: '{n}' and {rel}, labelled arrows only.",
            "medium": f"Place '{n}' in the center and show {rel} as connected nodes; label each arrow with the kind of data or control that flows.",
            "high": f"Full architecture diagram: '{n}' as the central node with an icon, {rel} around it, color-coded roles, numbered arrows for the flow and a legend.",
        }[v]
        return base + tier
    if t == "workflow":
        base = {
            "low": "A short numbered list of the steps; an arrow between steps only if it fits.",
            "medium": f"Horizontal flowchart of the steps for '{n}', one box per step with a short label.",
            "high": f"Flowchart with an icon per step for '{n}', numbered steps, a highlighted caution step and a start/end marker.",
        }[v]
        return base + tier
    if t == "example":
        return f"A quote box with the example, and the concept '{n}' highlighted next to it." + (
            " Add a small scene illustration of the situation." if v == "high" else "") + tier
    if t == "practice":
        return "Numbered checklist of the steps with a check box each; reserve a placeholder for a result screenshot." + (
            " Show the expected result in a highlighted panel." if v != "low" else "") + tier
    if t == "code":
        return "Monospaced code block on the left, the line-by-line explanation on the right" + (
            ", with the key line highlighted." if v != "low" else ".") + tier
    if t == "quiz":
        return "Question card at the top and the choices as separate buttons; keep the answer for the next reveal." + tier
    if t == "summary":
        return "Checklist of the key points, one icon per point" + (
            "; connect the points with arrows to show the flow." if v == "high" else ".") + tier
    base = {
        "low": f"Text-centered slide: the key message on top and up to {c.req.limits.body_points_max} bullet points; no decoration.",
        "medium": f"Key message on top, bullets on the left and a simple diagram on the right with '{n}' and {rel}.",
        "high": f"Visual-first slide: '{n}' as a large icon node with {rel} around it, a short caption per connection, color-coded roles.",
    }[v]
    return base + tier


# ---- presenter notes --------------------------------------------------------------
def _notes(
    c: _C, explanation: str | None, analogy: str | None, example: str | None, core_override: str | None = None
) -> str | None:
    n = c.req.notes
    if n.mode == "none":
        return None
    name = c.name
    tone = c.p.lecture_tone
    t = c.type
    title = c.s.title
    opening = {
        0: f"여러분, 이번에는 '{name}'을(를) 아주 쉽게 살펴보겠습니다.",
        1: f"이번 슬라이드에서는 '{name}'의 구조와 동작을 확인합니다.",
        2: f"여기서는 '{name}'을(를) 구현과 운영 관점에서 짚어 보겠습니다.",
    }[c.tier]
    core = c.first
    tier_line = {
        0: "어려운 용어가 나오면 잠시 멈추고 뜻을 다시 말해 주세요.",
        1: "앞에서 본 개념과 어떻게 연결되는지 화살표를 따라 설명합니다.",
        2: "한계와 제약을 먼저 짚고, 대안이 있다면 비교해서 말합니다.",
    }[c.tier]
    question = {
        0: "여기까지 이해되셨나요? 어려운 부분이 있으면 바로 질문해 주세요.",
        1: f"정리해 볼까요? '{name}'의 역할을 한 문장으로 말해 보세요.",
        2: "이 설계를 선택하면 무엇을 얻고 무엇을 감수해야 하는지 함께 논의해 봅시다.",
    }[c.tier]
    if t == "definition":
        opening = {0: f"먼저 '{name}'이(가) 무슨 뜻인지 알아봅시다.", 1: f"'{name}'의 정의를 확인합니다.",
                   2: f"'{name}'의 정의와 전제를 짚습니다."}[c.tier]
        tier_line = {0: "정의 문장을 천천히 읽고, 어려운 낱말은 바로 풀어서 말해 주세요.",
                     1: "정의 문장을 읽고 핵심 단어를 짚습니다.",
                     2: "정의가 성립하는 전제와 적용 범위를 함께 언급합니다."}[c.tier]
    elif t == "practice":
        opening = {0: f"이번에는 '{title}'을(를) 하나씩 따라 해 봅니다.", 1: f"'{title}'을(를) 직접 수행해 봅니다.",
                   2: f"'{title}'에서 결과를 검증하고 조건을 바꿔 봅니다."}[c.tier]
        tier_line = {0: "시연을 보여 준 뒤 학습자가 한 단계씩 따라 하게 합니다.",
                     1: "각 단계를 직접 수행하게 하고 결과를 확인합니다.",
                     2: "결과가 다를 때의 원인을 학습자가 스스로 분석하게 합니다."}[c.tier]
        question = {0: "막히는 단계가 있으면 손을 들어 알려 주세요.", 1: "결과가 자료의 설명과 같은지 확인해 보세요.",
                    2: "예상과 다른 결과가 나왔다면 원인을 가설로 세워 보세요."}[c.tier]
        core = None
    elif t == "example":
        opening = f"'{name}'의 예시를 살펴봅니다."
        tier_line = "예시가 어떤 개념을 설명하는지 연결해서 말합니다."
    elif t == "comparison":
        opening = f"{', '.join(c.names[:3])}을(를) 나란히 비교해 봅니다."
        tier_line = "비교 기준을 먼저 말하고 항목별 차이를 짚습니다."
    elif t == "code":
        opening = "자료의 코드를 함께 읽어 봅니다."
        tier_line = "코드가 실행되는 순서를 위에서 아래로 따라갑니다."
        core = None
    elif t in ("architecture", "diagram", "workflow"):
        tier_line = {0: "그림의 화살표를 손으로 따라가며 보여 줍니다.",
                     1: "구성 요소 사이의 연결과 흐름 방향을 짚어 설명합니다.",
                     2: "데이터 흐름과 제어 흐름, 실패 지점을 구분해서 말합니다."}[c.tier]
    elif t in ("title", "agenda", "quiz", "summary"):
        opening = {
            "title": f"안녕하세요. '{c.req.lecture_title}' 강의를 시작합니다.",
            "agenda": "먼저 오늘의 학습 목표와 진행 순서를 안내합니다.",
            "quiz": "배운 내용을 확인하는 시간입니다.",
            "summary": "이제 오늘 내용을 정리해 보겠습니다.",
        }[t]
        tier_line = {0: "부담 없이 편하게 따라와 주세요.", 1: "전체 흐름을 먼저 머릿속에 그려 봅니다.",
                     2: "각 주제의 쟁점과 한계를 염두에 두고 들어 주세요."}[c.tier]
        core = core_override if t == "summary" else None
        if t in ("title", "agenda"):
            question = ""
        if t == "quiz":
            question = "정답을 먼저 떠올려 본 뒤 해설과 비교해 보세요."
    if tone == "conversational":
        opening = "자, " + opening
    elif tone == "professional":
        opening = "핵심부터 말씀드리겠습니다. " + opening
    if c.so:
        tier_line = "자료의 문장을 근거로 설명합니다."
    if n.mode == "concise":
        parts = [opening]
        if core:
            parts.append(_short(core, 100))
        parts.append(tier_line)
        return _fit(parts, n.max_chars, n.max_sentences)
    # full: add sentences in order of importance until the time budget is used
    pool: list[str] = [opening]
    if c.s.previous_slide_title:
        pool.append(f"앞에서는 '{c.s.previous_slide_title}'을(를) 다루었습니다.")
    if core:
        pool.append(core)
    pool.append(tier_line)
    if analogy:
        pool.append(analogy)
    if example:
        pool.append(example)
    flavor = _FLAVOR.get(c.p.lecture_type)
    if flavor and not c.so and t not in ("practice", "code", "title", "agenda", "quiz", "summary"):
        pool.append(flavor)
    if c.type == "practice":
        pool.append("막히는 학습자가 있으면 해당 단계의 자료 문장을 함께 다시 읽습니다.")
        pool += [f"단계 안내: {k}" for k in c.s.key_points[:4]]
    if t not in ("title", "agenda", "summary", "quiz", "practice"):
        extra = [x.text for x in c.snips if x.text != core and x.kind in ("point", "description", "definition", "example")]
        pool += [f"자료에는 다음 내용도 있습니다: {_short(x, 120)}" for x in extra]
    if explanation and not c.so:
        pool.append(_short(explanation.split(". ")[-1], 120))
    pool.append(question)
    if c.s.next_slide_title:
        pool.append(f"다음 슬라이드는 '{c.s.next_slide_title}'입니다.")
    return _fit(pool, n.target_chars, n.max_sentences, hard=n.max_chars)


def _fit(sentences: list[str], target: int, max_sentences: int, hard: int | None = None) -> str:
    """Take sentences in order until the character target is reached (never beyond `hard`)."""
    out: list[str] = []
    total = 0
    limit = hard if hard is not None else target
    for s in sentences:
        s = normalize(s)
        if not s or any(s in o for o in out):  # never repeat a sentence
            continue
        if len(out) >= max_sentences and max_sentences > 0:
            break
        if out and total + len(s) + 1 > limit:
            continue
        out.append(s)
        total += len(s) + 1
        if total >= target and len(out) >= 2:
            break
    return " ".join(out)


# ---- practice / code / quiz / summary -------------------------------------------------
def _practice(c: _C) -> dict[str, Any]:
    n = c.name
    code = c.texts("code")
    line = next((ln.strip() for ln in code[0].splitlines() if ln.strip()), None) if code else None
    # The steps come from the planner's key points (the structure stays); the writer adds
    # what to do with them. A source code line is used verbatim, never invented.
    steps = [k.rstrip() for k in c.s.key_points if k.strip()] or [
        f"자료의 설명에 따라 '{n}' 관련 절차를 순서대로 수행한다."
    ]
    if c.p.lecture_type == "theory":  # a demonstration rather than a full exercise
        steps = steps[:4]
    if line and len(steps) >= 2:
        steps.insert(2, f"자료의 코드 `{line}` 를 그대로 입력해 실행한다.")
    elif line:
        steps.append(f"자료의 코드 `{line}` 를 그대로 입력해 실행한다.")
    if c.tier == 0:
        steps.append("막히면 한 단계 앞으로 돌아가 자료의 문장을 다시 읽는다.")
    if not c.so:
        if c.p.lecture_type == "practice" or c.tier >= 1:
            steps.append("조건을 하나씩 바꾸어 다시 수행하고 결과의 차이를 기록한다.")
        if c.tier == 2:
            steps.append("실패 상황(응답 없음, 잘못된 입력)에서의 동작을 확인하고 원인을 분석한다.")
    goal = {
        0: "한 단계씩 따라 하며 자료의 설명과 같은 결과가 나오는지 확인한다.",
        1: "직접 수행해 자료의 설명과 같은 결과가 나오는지 확인한다.",
        2: "결과를 검증하고 조건을 바꿨을 때의 차이를 분석한다.",
    }[c.tier]
    return {
        "goal": goal,
        "prerequisites": ["자료에 안내된 실습 환경", "앞 슬라이드의 개념 이해"],
        "steps": steps,
        "expected_result": "자료의 설명과 일치하는 결과를 확인할 수 있다.",
        "cautions": [
            "자료에 운영체제나 버전 정보가 없다면 임의로 가정하지 않는다.",
            "시스템 설정을 바꿔야 한다면 실습 전에 미리 알린다.",
        ],
    }


def _code(c: _C) -> dict[str, Any] | None:
    code = c.texts("code")
    if not code:
        return None
    lines = [ln.strip() for ln in code[0].splitlines() if ln.strip()][:3]
    flow = ["코드는 위에서 아래로 한 줄씩 실행된다.", "각 줄의 결과를 자료의 설명과 비교한다."]
    if c.tier == 2 and not c.so:
        flow.append("오류 처리와 재시도 조건을 함께 점검한다.")
    return {
        "purpose": f"이 코드는 '{c.name}'와(과) 관련된 동작을 자료에서 보여 준 예입니다.",
        "key_lines": [f"`{ln}` 자료가 설명한 동작을 코드로 옮긴 줄" for ln in lines],
        "execution_flow": flow,
        "cautions": ["자료에 실행 환경이 없다면 환경을 임의로 가정하지 않는다."],
    }


def _quiz(c: _C) -> dict[str, Any] | None:
    n = c.name
    correct = c.sentence_of(n) or c.first
    if not correct:
        return None
    distract = []
    for x in c.snips:
        if x.kind in ("definition", "description") and x.concept and x.concept != n and x.text != correct:
            if x.text not in distract:
                distract.append(x.text)
    if len(distract) >= 2:
        choices = [_short(correct, 90), *[_short(d, 90) for d in distract[:2]]]
        pos = c.s.slide_number % len(choices)
        choices = choices[1:pos + 1] + [choices[0]] + choices[pos + 1:]
        q = f"다음 중 '{n}'에 대한 설명으로 알맞은 것은?"
        if c.tier == 2:
            q += " 근거를 자료에서 찾아 답하시오."
        return {"question": q, "choices": choices, "answer": _short(correct, 90),
                "explanation": f"정답은 자료의 설명과 일치하는 문장입니다. {_short(correct, 100)}"}
    return {"question": f"'{n}'을(를) 자신의 말로 설명해 보세요.", "choices": [],
            "answer": _short(correct, 140), "explanation": "자료의 정의와 비교해 빠진 부분이 없는지 확인합니다."}


def _summary(c: _C) -> tuple[str, list[str]]:
    if not c.s.concepts:  # objective check: the objectives themselves are the points
        pts = list(c.req.objectives or c.s.key_points)[: c.req.limits.body_points_max]
        head = "처음에 세운 학습 목표를 하나씩 확인하고 다음 학습으로 연결합니다."
        if c.s.next_slide_title:
            head += f" 다음 내용: '{c.s.next_slide_title}'."
        return head, pts
    names = c.names[:4]
    head = {
        0: f"오늘 배운 핵심은 {', '.join(names)}입니다. 각각의 역할을 한 문장으로 말할 수 있으면 충분합니다.",
        1: f"{', '.join(names)}의 관계를 한 흐름으로 정리합니다.",
        2: f"{', '.join(names)}의 설계 관점과 한계를 함께 정리합니다.",
    }[c.tier]
    if c.so:
        head = f"{', '.join(names)}의 핵심 내용을 자료에 맞게 정리합니다."
    if c.s.next_slide_title:
        head += f" 다음 내용: '{c.s.next_slide_title}'."
    pts = []
    for nm in names:
        s = c.sentence_of(nm)
        pts.append(f"{nm}: {_short(s, 70)}" if s else f"{nm}: 역할과 관계를 다시 확인한다")
    if c.p.lecture_type == "exam_preparation":
        pts.append(f"자주 헷갈리는 부분: {' 와(과) '.join(names[:2])}의 역할 구분")
    if c.tier == 2 and not c.so:
        pts.append("남은 쟁점: 한계와 대안 검토")
    return head, pts[: c.req.limits.body_points_max]


def _comparison(c: _C) -> tuple[list[str], str]:
    items = [(nm, c.sentence_of(nm)) for nm in c.names[:3]]
    pts = ["비교 기준: 자료의 정의와 역할" if c.so else "비교 기준: 역할, 연결 대상, 동작 방식"]
    for nm, s in items:
        pts.append(f"{nm}: {_short(s, 80)}" if s else f"{nm}: 자료에서 정의를 확인한다")
    with_s = [(nm, s) for nm, s in items if s]
    if len(with_s) >= 2:
        pts.append(f"차이점: '{with_s[0][0]}'은(는) {_short(with_s[0][1], 45)} / '{with_s[1][0]}'은(는) {_short(with_s[1][1], 45)}")
    if (c.tier == 2 or c.p.lecture_type == "exam_preparation") and not c.so:
        pts.append("trade-off: 선택 기준은 자료에서 언급한 목적과 제약에 따라 달라진다")
    text = f"{', '.join(c.names[:3])}의 정의와 역할을 나란히 놓고 차이를 확인합니다."
    return pts[: c.req.limits.body_points_max + 1], text


def _structure_points(c: _C) -> tuple[list[str], str]:
    """architecture / workflow / diagram"""
    pts: list[str] = []
    if c.type == "workflow":
        src = _source_points(c, 90)
        pts = [f"단계 {i + 1}: {s}" for i, s in enumerate(src[:4])]
        cautions = [x.text for x in c.snips if x.kind == "point"]
        if cautions:
            pts.append(f"주의: {_short(cautions[0], 80)}")
    else:
        used: set[str] = set()
        for nm in c.names[:3]:
            s = c.sentence_of(nm)
            pts.append(f"구성 요소 — {nm}: {_short(s, 80)}" if s else f"구성 요소 — {nm}")
            if s:
                used.add(s)
        for nm, pre in c.s.concept_relations.items():
            pts.append(f"관계: '{nm}'은(는) {', '.join(pre)}와(과) 이어진다")
        for x in c.snips:  # other source sentences that talk about this component
            if x.text not in used and any(nm.lower() in x.text.lower() for nm in c.names):
                pts.append(f"흐름: {_short(x.text, 80)}")
                used.add(x.text)
        if not c.so:
            rel = c.related()
            if rel and len(pts) < c.req.limits.body_points_min:
                pts.append(f"함께 보는 요소: {', '.join(rel)}")
    text = {
        0: f"'{c.name}'이(가) 무엇과 이어지는지 그림으로 먼저 보면 쉽습니다. 화살표는 정보가 가는 방향을 뜻합니다.",
        1: f"'{c.name}'이(가) 다른 요소와 어떻게 연결되고 무엇이 오가는지를 그림과 함께 설명합니다.",
        2: f"'{c.name}'을(를) 중심으로 데이터 흐름과 제어 흐름을 나누어 보고, 실패 지점을 함께 표시합니다.",
    }[c.tier]
    if c.so:
        text = f"'{c.name}'이(가) 다른 요소와 어떻게 연결되는지를 자료의 문장에 맞게 설명합니다."
    return pts[: c.req.limits.body_points_max], text


# ---------------------------------------------------------------------------
def write_slide(req: EnrichmentRequest) -> dict[str, Any]:
    c = _C(req)
    allowed = set(req.allowed_fields)
    out: dict[str, Any] = {"slide_number": req.slide.slide_number}
    prov: dict[str, str] = {}
    t = c.type

    if "display_title" in allowed:
        out["display_title"] = _display_title(c)
        prov["display_title"] = "llm_explanation"
    km, kp = _key_message(c)
    out["key_message"] = km
    prov["key_message"] = kp

    explanation = analogy = example = None
    if "body_points" in allowed:
        if t == "agenda":
            out["body_points"] = list(req.objectives)[: req.limits.body_points_max]
            prov["body_points"] = "source_grounded" if req.objectives else "suggested"
        elif t == "summary":
            head, pts = _summary(c)
            out["body_points"] = pts
            out["summary_message"] = head
            prov["body_points"] = "paraphrased_source"
            prov["summary_message"] = "llm_explanation"
        elif t == "comparison":
            out["body_points"], explanation = _comparison(c)
            prov["body_points"] = "paraphrased_source" if c.so else "llm_explanation"
        elif t in ("architecture", "diagram", "workflow"):
            out["body_points"], explanation = _structure_points(c)
            prov["body_points"] = "paraphrased_source" if c.so else "llm_explanation"
        else:
            out["body_points"], prov["body_points"] = _concept_points(c)
    if "explanation" in allowed:
        if explanation is None:
            if t == "example":
                explanation = f"이 예시는 '{c.name}'이(가) 실제로 어떻게 쓰이는지 보여 줍니다."
                prov["explanation"] = "llm_explanation"
            elif t == "definition" and c.first:
                lead = {0: "쉽게 풀면 이런 뜻입니다.", 1: "정의를 정확히 확인합니다.", 2: "정의와 전제를 함께 확인합니다."}[c.tier]
                explanation = f"{lead} {c.first}"
                prov["explanation"] = "paraphrased_source"
            else:
                explanation, prov["explanation"] = _explanation(c)
        else:
            prov["explanation"] = "llm_explanation"
        out["explanation"] = explanation
    if "analogy" in allowed and not c.so:
        analogy = _ANALOGY.get(t)
        out["analogy"] = analogy
        prov["analogy"] = "llm_explanation"
    if "example" in allowed:
        example, prov["example"] = _example(c)
        out["example"] = example
    if "practice_instruction" in allowed:
        out["practice_instruction"] = _practice(c)
        prov["practice_instruction"] = "llm_explanation"
    if "code_explanation" in allowed:
        ce = _code(c)
        out["code_explanation"] = ce
        prov["code_explanation"] = "llm_explanation" if ce else "suggested"
    if "quiz_content" in allowed:
        q = _quiz(c)
        out["quiz_content"] = q
        prov["quiz_content"] = "paraphrased_source" if q else "suggested"
    if "visual_instruction" in allowed:
        out["visual_instruction"] = _visual(c)
        prov["visual_instruction"] = "llm_explanation"
    if "presenter_notes" in allowed:
        out["presenter_notes"] = _notes(c, explanation, analogy, example, core_override=out.get("summary_message"))
        prov["presenter_notes"] = "llm_explanation"
    out["provenance"] = prov
    return out


__all__ = ["MockLLMClient", "write_slide", "tier_of"]
