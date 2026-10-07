"""Spoken lecture scripts written by the LLM, grounded in the slide + source.

The rule-based LectureScriptBuilder stays as a fallback when no LLM is configured
or a slide call fails. No new facts: the model only rewrites the allowed claims
into words a lecturer can read aloud.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..models.enriched_slide_spec import EnrichedSlideSpecification, SlideEnrichmentStatus
from ..models.lecture_plan import LecturePlan
from ..models.lecture_profile import LectureProfile
from ..models.lecture_script import LectureScript, SlideScript
from ..models.slide_spec import Slide, SlideSpecification, SlideType
from ..models.source import SourceAnalysis, SourceMaterial
from ..services.llm_client import LLMClient, LLMError
from .lecture_script_builder import (
    HIGH,
    LOW,
    LectureScriptBuilder,
    _AUDIENCE,
    _chars,
    _enriched_notes,
    _facts,
    _is_chrome_line,
    _looks_cue,
    _looks_meta,
    _prefer,
    _is_broken_spoken,
    _same_claim,
    _spoken_form,
    _usable_line,
    SCRIPT_CHARS_PER_SECOND,
    _is_imported,
    rebalance_imported_times,
    _imported_page_lines,
    _remember_claims,
    _source_sentences,
    _strip_said_text,
    _without_said,
    _spoken_seconds,
    _summary_facts,
    _target_chars,
    _trim,
)
from .prompt_builder import _norm_text, _one_line
from .slide_content_enricher import hash_profile, hash_spec
from .text_utils import stable_hash

logger = logging.getLogger("ailecturegen")

PROMPT_VERSION = "script-v24"
FILL = 0.96
LECTURE_FILL = 1.0
EXPAND_TRIES = 3
SCRIPT_CACHE = "lecture_script.json"
_CLOCK = re.compile(r"약?\s*\d+\s*(분|초)(?:짜리|동안)?")

SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"script": {"type": "string"}},
    "required": ["script"],
}

SYSTEM_PROMPT = """당신은 강의 대본 작가입니다. 교수가 슬라이드를 넘기며 그대로 읽는 한국어 구어 대본만 씁니다.

규칙:
1. <facts>에 있는 사실만 사용한다. 정의·수치·예시·주장을 새로 만들지 않는다.
2. 발표 방향이나 불릿 라벨이 아니라, 실제로 입으로 하는 말을 쓴다. '선행 개념: MQTT'처럼 콜론이 있는 명세 라벨을 그대로 읽지 않는다.
3. 몇 분·몇 초 동안 설명한다고 말하지 않는다. 글자 수나 AI라는 말도 하지 않는다.
4. '화면에 나온 문장을 잠시 눈에 담아 주세요' 같은 빈 시간 메우기 문장은 쓰지 않는다.
5. 제목/목차 슬라이드에서는 강의를 열고 순서를 안내하되, 강의 전체 시간은 말하지 않는다.
6. 핵심 정리 슬라이드에서는 배운 개념의 뜻만 다시 말한다. 자료 서두의 집필 안내(개념명이 아니다, 문장 속에 데이터가 있다 등)는 무시한다.
7. 목표 글자 수에 맞춰 같은 화면의 사실을 풀어서 설명한다. 짧은 라벨을 '다시 말하면'으로 반복하지 않는다. 정의나 사례 문장을 강의 말로 충분히 풀어 목표 분량까지 채운다. 목표보다 짧게 쓰면 안 된다.
8. 모든 문장은 합니다체 존댓말로 통일한다. 한다/이다/반말과 해요체를 섞지 않는다. 사실의 어미만 바꾸고 내용은 바꾸지 않는다.
9. <facts>는 이 슬라이드 화면과 같은 장의 자료 문장이다. 다른 장의 사실은 끌어오지 않는다. 화면 글을 가리키며, 같은 장의 자료 문장으로 설명을 이어 간다.
10. 괄호 안의 약어·영문 표기는 읽지 않는다. '사물인터넷(IoT)'는 '사물인터넷'이라고만 말한다.
11. 대시(—), 화살표(→), 슬래시(/), 가운뎃점(·)은 읽지 않는다. '발행/구독'은 '발행과 구독'으로, 'AIoT → MQTT'는 'AIoT, MQTT'로 말한다.
12. '20분', '6분짜리', '03 / 11', 'DEFINITION' 같은 시간·장식 표기는 말하지 않는다. 이 슬라이드 화면에 있는 문장만 말한다.
13. 앞 슬라이드에서 이미 말한 정의·설명은 다시 읽지 않는다. 이 화면에 새로 나온 내용만 말한다. 핵심 정리 슬라이드만 예외로 짧게 다시 모은다.
14. 짧은 화면 라벨('핵심 구성 요소', '주소 메시지를 분류하는 키')을 한 줄로 읽지 않는다. 그 뜻을 완전한 강의 문장으로 말한다.
15. 문장은 끝까지 말한다. '보내거나입니다', '만들어진…입니다', '빠르지만입니다'처럼 끊긴 조각에 입니다만 붙이지 않는다. 같은 문장을 순서만 바꿔 반복하지 않는다.
16. 말줄임표로 끊긴 화면 글은 읽지 않는다. 같은 화면의 완전한 문장만 풀어 말한다.
17. 맞춤법과 조사를 확인한다. 영어 단어 뒤에는 받침이 없으므로 와/를/가/를 쓴다. 'Publish과', 'MQTT을'은 틀린 말이다.
18. JSON 객체 하나만 답한다: {"script": "대본 전체"}"""


def script_fingerprint(
    plan: LecturePlan,
    spec: SlideSpecification,
    profile: LectureProfile | None,
    analysis: SourceAnalysis | None,
    llm: LLMClient | None,
) -> str:
    concepts = [(c.name, c.description) for c in analysis.concepts] if analysis else []
    return stable_hash(
        {
            "v": PROMPT_VERSION,
            "pace": SCRIPT_CHARS_PER_SECOND,
            "spec": hash_spec(spec),
            "plan": {
                "title": plan.title,
                "objectives": plan.learning_objectives,
                "audience": plan.audience,
            },
            "profile": hash_profile(profile) if profile else None,
            "concepts": concepts,
            "llm": getattr(llm, "model", None) if llm else None,
        }
    )


def clear_script_cache(store, project_id: str) -> None:
    path = store.project_dir(project_id) / SCRIPT_CACHE
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("lecture_script.json could not be removed")


def load_or_build_scripts(store, project, llm) -> LectureScript:
    """Cached spoken script for this plan/spec, rebuilt when the fingerprint changes."""
    from ..models.source import SourceMaterial

    if (
        project.lecture_plan is not None
        and _is_imported(project.lecture_plan)
        and project.slide_specification is not None
    ):
        total = project.lecture_plan.duration_minutes * 60
        if rebalance_imported_times(project.slide_specification, total):
            project.discard_video()
            clear_script_cache(store, project.id)
            store.save(project)
    material = None
    path = store.project_dir(project.id) / "source_material.json"
    try:
        if path.is_file():
            material = SourceMaterial.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        logger.warning("source_material.json could not be read; building scripts without it")
    fp = script_fingerprint(
        project.lecture_plan,
        project.slide_specification,
        project.lecture_profile,
        project.source_analysis,
        llm,
    )
    cached_path = store.project_dir(project.id) / SCRIPT_CACHE
    try:
        if cached_path.is_file():
            cached = LectureScript.model_validate(json.loads(cached_path.read_text(encoding="utf-8")))
            if cached.fingerprint == fp:
                return cached
    except (OSError, ValueError):
        logger.warning("lecture_script.json could not be read; rebuilding scripts")
    result = LectureScriptWriter().build(
        project_id=project.id,
        plan=project.lecture_plan,
        spec=project.slide_specification,
        profile=project.lecture_profile,
        analysis=project.source_analysis,
        material=material,
        enriched=project.enriched_specification,
        llm=llm,
    )
    if project.lecture_plan is not None and _is_imported(project.lecture_plan):
        project.discard_video()
        store.save(project)
    result.fingerprint = fp
    store.write_artifact(project.id, SCRIPT_CACHE, result)
    return result


class LectureScriptWriter:
    def build(
        self,
        *,
        project_id: str,
        plan: LecturePlan,
        spec: SlideSpecification,
        profile: LectureProfile | None = None,
        analysis: SourceAnalysis | None = None,
        material: SourceMaterial | None = None,
        enriched: EnrichedSlideSpecification | None = None,
        llm: LLMClient | None = None,
    ) -> LectureScript:
        fallback = LectureScriptBuilder()
        notes = _enriched_notes(enriched)
        visible = _enriched_visible(enriched)
        slides: list[SlideScript] = []
        said: list[str] = []
        for i, slide in enumerate(spec.slides):
            prev_title = spec.slides[i - 1].title if i else None
            next_title = spec.slides[i + 1].title if i + 1 < len(spec.slides) else None
            text, origin = self._one(
                fallback,
                plan,
                profile,
                slide,
                analysis,
                material,
                notes.get(slide.slide_number),
                visible.get(slide.slide_number),
                llm,
                prev_title,
                next_title,
                i + 1,
                len(spec.slides),
                said,
            )
            slides.append(
                SlideScript(
                    slide_number=slide.slide_number,
                    title=slide.title,
                    slide_type=slide.slide_type.value,
                    estimated_seconds=slide.estimated_explanation_time,
                    spoken_seconds=_spoken_seconds(text),
                    text=text,
                    source=origin,
                )
            )
        if llm is not None and plan.duration_minutes:
            _top_up_lecture(
                slides, spec, plan, profile, analysis, material, notes, visible, llm, said,
            )
        kinds = {s.source for s in slides}
        if kinds == {"llm"}:
            writer = "llm"
        elif "llm" in kinds:
            writer = "mixed"
        else:
            writer = "composed"
        result = LectureScript(
            project_id=project_id,
            title=plan.title,
            slide_count=len(slides),
            total_seconds=sum(s.estimated_seconds for s in slides),
            slides=slides,
            writer=writer,
            model_name=getattr(llm, "model", None) if llm else None,
        )
        return result

    def _one(
        self,
        fallback: LectureScriptBuilder,
        plan: LecturePlan,
        profile: LectureProfile | None,
        slide: Slide,
        analysis: SourceAnalysis | None,
        material: SourceMaterial | None,
        notes: str | None,
        visible: list[str] | None,
        llm: LLMClient | None,
        prev_title: str | None,
        next_title: str | None,
        index: int,
        total: int,
        said: list[str] | None = None,
    ) -> tuple[str, str]:
        if llm is None:
            return fallback._for_slide(plan, profile, slide, analysis, material, notes, said)
        facts = _allowed_facts(plan, profile, slide, analysis, material, visible, said)
        target = _target_chars(slide)
        recap = slide.slide_type == SlideType.summary
        imported = _is_imported(plan)
        try:
            text = _draft_to_length(
                llm, plan, profile, slide, facts, target, prev_title, next_title, index, total,
                imported=imported, recap=recap, said=said,
            )
            if _chars(text) >= int(target * FILL) or _keep_llm_draft(text, target):
                _remember_claims(said, text)
                return text, "llm"
            logger.info(
                "LLM script for slide %s was too short (%s chars, target %s); using composed fallback",
                slide.slide_number,
                _chars(text),
                target,
            )
        except LLMError:
            logger.info("LLM script failed for slide %s; using composed fallback", slide.slide_number)
        return fallback._for_slide(plan, profile, slide, analysis, material, notes, said)


def _enriched_visible(enriched: EnrichedSlideSpecification | None) -> dict[int, list[str]]:
    if enriched is None:
        return {}
    out: dict[int, list[str]] = {}
    for s in enriched.slides:
        if s.status != SlideEnrichmentStatus.enriched or s.enriched is None:
            continue
        e = s.enriched
        bits: list[str] = []
        for x in (e.key_message, e.explanation, e.example, e.summary_message, *(e.body_points or [])):
            spoken = _usable_line(x) if x else None
            if spoken:
                bits.append(spoken)
        if bits:
            out[s.slide_number] = bits
    return out


def _allowed_facts(
    plan: LecturePlan,
    profile: LectureProfile | None,
    slide: Slide,
    analysis: SourceAnalysis | None,
    material: SourceMaterial | None,
    visible: list[str] | None = None,
    said: list[str] | None = None,
) -> list[str]:
    if _is_imported(plan):
        extra = list(visible or []) + _imported_page_lines(slide, material)
        facts = _prefer(slide, _facts(slide, extra))
        if slide.slide_type != SlideType.summary:
            facts = _without_said(facts, said)
        return facts
    if slide.slide_type == SlideType.summary:
        facts = _summary_facts(plan, analysis, slide)
        facts.extend(_one_line(o) for o in plan.learning_objectives if o)
        return [f for f in facts if f and not _looks_meta(f)]
    if slide.slide_type in (SlideType.title, SlideType.agenda):
        bits = [plan.title, *plan.learning_objectives, *[s.title for s in plan.sections]]
        bits.extend(p for p in slide.key_points if p)
        if profile is not None:
            bits.append(_AUDIENCE.get(profile.audience_level.value, ""))
        out: list[str] = []
        for x in bits:
            line = _usable_line(x)
            if line:
                out.append(line)
        return _without_said(out, said)
    extra = list(visible or []) + _source_sentences(slide, plan, analysis, material)
    return _prefer(slide, _facts(slide, _without_said(extra, said)))


def _draft_to_length(
    llm: LLMClient,
    plan: LecturePlan,
    profile: LectureProfile | None,
    slide: Slide,
    facts: list[str],
    target: int,
    prev_title: str | None,
    next_title: str | None,
    index: int,
    total: int,
    *,
    imported: bool,
    recap: bool,
    said: list[str] | None,
    draft: str | None = None,
) -> str:
    high = int(target * HIGH)
    text = _clean(
        _ask(llm, plan, profile, slide, facts, target, prev_title, next_title, index, total, draft),
        high,
    )
    for _ in range(EXPAND_TRIES):
        if imported and not recap:
            text = _strip_said_text(text, said) or text
        if _chars(text) >= int(target * FILL):
            return text
        nxt = _clean(
            _ask(llm, plan, profile, slide, facts, target, prev_title, next_title, index, total, draft=text),
            high,
        )
        if imported and not recap:
            nxt = _strip_said_text(nxt, said) or nxt
        merged = _merge_spoken(text, nxt)
        if _chars(merged) > _chars(text):
            text = _trim(merged, high)
        elif _chars(nxt) >= _chars(text):
            text = nxt
    if imported and not recap:
        text = _strip_said_text(text, said) or text
    return text


def _top_up_lecture(
    slides: list[SlideScript],
    spec: SlideSpecification,
    plan: LecturePlan,
    profile: LectureProfile | None,
    analysis: SourceAnalysis | None,
    material: SourceMaterial | None,
    notes: dict[int, str],
    visible: dict[int, list[str]],
    llm: LLMClient,
    said: list[str] | None,
) -> None:
    """If the spoken total is short of the lecture duration, expand the thinnest slides."""
    need = plan.duration_minutes * 60
    imported = _is_imported(plan)
    by_num = {s.slide_number: s for s in spec.slides}
    for _ in range(6):
        spoken = sum(s.spoken_seconds for s in slides)
        if spoken >= int(need * LECTURE_FILL):
            return
        ranked = sorted(
            (s for s in slides if s.source == "llm" and s.spoken_seconds < int(s.estimated_seconds * FILL)),
            key=lambda s: s.spoken_seconds / max(1, s.estimated_seconds),
        )
        target_slide = next((s for s in ranked if s.estimated_seconds >= 20), None)
        if target_slide is None:
            return
        slide = by_num.get(target_slide.slide_number)
        if slide is None:
            return
        recap = slide.slide_type == SlideType.summary
        facts = _allowed_facts(plan, profile, slide, analysis, material, visible.get(slide.slide_number), None)
        idx = next(i for i, s in enumerate(spec.slides) if s.slide_number == slide.slide_number)
        prev_title = spec.slides[idx - 1].title if idx else None
        next_title = spec.slides[idx + 1].title if idx + 1 < len(spec.slides) else None
        try:
            text = _draft_to_length(
                llm, plan, profile, slide, facts, _target_chars(slide),
                prev_title, next_title, idx + 1, len(spec.slides),
                imported=imported, recap=recap, said=said, draft=target_slide.text,
            )
        except LLMError:
            return
        target_slide.text = text
        target_slide.spoken_seconds = _spoken_seconds(text)


def _ask(
    llm: LLMClient,
    plan: LecturePlan,
    profile: LectureProfile | None,
    slide: Slide,
    facts: list[str],
    target: int,
    prev_title: str | None,
    next_title: str | None,
    index: int,
    total: int,
    draft: str | None = None,
) -> str:
    kw: dict[str, Any] = {}
    if getattr(llm, "supports_schema", False):
        kw["schema"] = SCRIPT_SCHEMA
    data = llm.complete_json(
        task="script_slide",
        system=SYSTEM_PROMPT,
        user=_user_message(plan, profile, slide, facts, target, prev_title, next_title, index, total, draft),
        **kw,
    )
    text = data.get("script") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise LLMError("LLM 대본이 비어 있습니다.")
    return text


def _user_message(
    plan: LecturePlan,
    profile: LectureProfile | None,
    slide: Slide,
    facts: list[str],
    target: int,
    prev_title: str | None,
    next_title: str | None,
    index: int,
    total: int,
    draft: str | None,
) -> str:
    audience = _AUDIENCE.get(plan.audience, "여러분")
    tone = "자연스러운 강의체"
    if profile is not None:
        audience = _AUDIENCE.get(profile.audience_level.value, audience)
        tone = {
            "academic": "학술적인 합니다체 존댓말",
            "professional": "실무 설명체의 합니다체 존댓말",
            "conversational": "쉬운 합니다체 존댓말",
        }.get(profile.lecture_tone.value, "자연스러운 합니다체 존댓말")
    lines = [
        f"강의 제목: {plan.title}",
        f"청중: {audience}",
        f"어조: {tone}",
        f"슬라이드 {index}/{total}: {slide.title}",
        f"유형: {slide.slide_type.value}",
        f"학습 목적: {slide.learning_purpose or slide.title}",
        f"이전 슬라이드: {prev_title or '(없음)'}",
        f"다음 슬라이드: {next_title or '(없음)'}",
        f"목표 글자 수: {target}자",
        f"이 슬라이드는 {slide.estimated_explanation_time}초 동안 말할 분량이다. 같은 화면의 사실을 풀어서 목표 글자 수에 맞춘다.",
        "이 슬라이드 화면에 새로 나온 문장만 설명한다. 앞에서 이미 말한 정의나 설명은 반복하지 않는다.",
        "목표 글자 수보다 짧게 쓰지 않는다. 짧은 라벨을 한 줄로 읽거나 다시 말하면으로 반복하지 않는다.",
        "괄호 안의 약어는 읽지 않는다. 합니다체만 쓴다. —, /, →, · 기호는 말로 바꿔 읽는다.",
        "영어 단어 조사는 와, 를, 가, 는이다. Publish과, MQTT을 같은 오타를 내지 않는다.",
        "완전한 문장으로만 쓰고, 화면 항목 제목을 그대로 나열하지 않는다.",
        "",
        "<facts>",
    ]
    lines.extend(f"- {f}" for f in facts[:40])
    if not facts:
        lines.append("- (이 슬라이드에 쓸 수 있는 추가 사실은 없습니다. 제목과 목적만으로 짧게 말하세요.)")
    lines.append("</facts>")
    if draft:
        lines.extend(
            [
                "",
                f"아래 초안은 {_chars(draft)}자입니다. {target}자보다 짧습니다. 같은 사실만 써서 각 항목을 더 자세히 풀어 {target}자가 되게 다시 쓰세요.",
                "<draft>",
                draft,
                "</draft>",
            ]
        )
    return "\n".join(lines)


def _merge_spoken(a: str, b: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for para in f"{a}\n\n{b}".split("\n\n"):
        line = _one_line(para)
        key = _norm_text(line)
        if not line or not key or key in seen or _is_broken_spoken(line):
            continue
        if any(_same_claim(line, prev) for prev in out):
            continue
        seen.add(key)
        out.append(line)
    return "\n\n".join(out)


_COMPLETE = re.compile(r"(습니다|입니다|니다|까)[.!?…]*$")


def _keep_llm_draft(text: str, target: int) -> bool:
    """Keep a spoken LLM draft instead of the mechanical filler, even if a bit short."""
    if _chars(text) < 40:
        return False
    return bool(re.search(r"(습니다|입니다|니다|까)", text))


def _clean(text: str, max_chars: int) -> str:
    kept: list[str] = []
    for para in text.replace("\r", "").split("\n"):
        line = _one_line(para)
        line = _CLOCK.sub("", line).strip(" ,.")
        if not line or _looks_meta(line) or _looks_cue(line) or _is_chrome_line(line):
            continue
        if _is_broken_spoken(line):
            continue
        if not _COMPLETE.search(line) and not re.search(r"(습니다|입니다|니다|까|다)[.!?…]?", line):
            continue
        spoken = _spoken_form(line)
        if spoken and not _is_broken_spoken(spoken):
            kept.append(spoken)
    return _trim("\n\n".join(kept), max_chars)
