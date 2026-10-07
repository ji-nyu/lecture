"""GroundingValidator (STAGE 6A): never trust the LLM's content.

For one slide it takes the schema-valid LLM answer and returns the content that is allowed
to be kept, with an honest provenance for every field and the list of what was changed.

Checks (all deterministic, no LLM):
  fields        only `allowed_fields`; presenter notes only when speaker_notes != none
  scope         sentences about a topic the source excludes ("~는 다루지 않는다") are removed
  safety        destructive commands (rm -rf, sudo, format, DROP TABLE, ...) are removed;
                OS / tool / version names the source never mentions are removed
                ("do not guess the environment"); steps that change the system are marked
  source_policy source_only: no example/analogy made up by the LLM, no term / number /
                command that is not in the source, key message and points must restate the
                source; source_first / expanded: additions are allowed but must be labelled
                llm_* - a claim of `source_grounded` / `paraphrased_source` is verified
                against the source snippets and downgraded when the text does not match
  conflict      a sentence that is nearly a source sentence but says the opposite is removed
  size          body points, explanation and presenter notes are cut to the limits of the
                request (notes: sentence count for `concise`, characters ~ slide time)

LIMIT (also in the README): this is a surface check - terms, numbers, commands, topics and
character overlap. It cannot prove that a fluent sentence contains no new fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm.base import EnrichmentRequest, LLMSlideOutput
from ..models.enriched_slide_spec import (
    CodeExplanation,
    EnrichedContent,
    FieldProvenance,
    PracticeContent,
    QuizContent,
)
from ..models.source import SourceLocation
from .source_context import core_fields
from .text_utils import (
    bigrams,
    clip_chars,
    coverage,
    fact_numbers,
    jaccard,
    known_latin,
    latin_tokens,
    norm_key,
    normalize,
    split_sentences,
    truncate_sentences,
    violates_scope,
)

P = FieldProvenance

DESTRUCTIVE_RE = re.compile(
    r"(\brm\s+-\w*[rf]|\brm\s+/|\bsudo\b|\bmkfs\b|\bdd\s+if=|\bformat\s+[a-z]:|\bdel\s+/[sfq]"
    r"|\brmdir\s+/s|\bshutdown\b|\breboot\b|\bchmod\s+(?:-\w+\s+)?0?777|\bdrop\s+(?:table|database)\b"
    r"|\btruncate\s+table\b|:\(\)\s*\{|\|\s*(?:ba)?sh\b|\bkill\s+-9|\breg\s+delete|>\s*/dev/sd"
    r"|디스크를?\s*포맷|모든\s*파일을?\s*삭제|시스템을?\s*삭제)",
    re.I,
)
ENV_RE = re.compile(
    r"(windows|ubuntu|debian|centos|fedora|macos|mac\s?os|linux|raspbian|android|docker"
    r"|python\s?[23](?:\.\d+)*|node(?:\.js)?\s?\d+|java\s?\d+|윈도우|리눅스|우분투)",
    re.I,
)
VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
BACKTICK_RE = re.compile(r"`([^`]+)`")
SYSTEM_CHANGE_RE = re.compile(
    r"(pip3?\s+install|npm\s+install|apt(?:-get)?\s+install|brew\s+install|systemctl"
    r"|service\s+\w+\s+(?:start|restart)|설치|환경\s*변수|방화벽|포트를?\s*(?:열|개방)|설정\s*파일)",
    re.I,
)
SYSTEM_TAG = "[시스템 설정 변경] "
_NEGATION = ("않", "아니", "없", "불가", "못하", "안 ")

_FACTUAL_EXEMPT = {"visual_instruction"}  # a layout instruction states no facts
_SOURCE_LIKE = (P.source_grounded, P.paraphrased_source)
SOURCE_ONLY_MIN_COVERAGE = 0.30  # key message / points must restate the source
PARAPHRASE_MIN_COVERAGE = 0.40
GROUNDED_MIN_COVERAGE = 0.85


SHORT_NOTES_RATIO = 0.35  # full notes under 35% of the spoken-length target are reported (a heuristic)

_LAYOUT_WORDS = (
    "배치", "왼쪽", "오른쪽", "중앙", "가운데", "상단", "하단", "좌측", "우측", "위쪽", "아래", "표", "다이어그램", "도식",
    "흐름도", "구조도", "계층", "타임라인", "카드", "박스", "아이콘", "체크리스트", "목록", "나란히", "열",
    "layout", "left", "right", "center", "top", "bottom", "diagram", "table", "flow", "icon", "card", "box",
    "list", "checklist", "column", "timeline",
)
_RELATION_WORDS = ("→", "->", "⇒", "화살표", "연결", "잇", "흐름", "방향", "arrow", "connect", "flow")
_DIAGRAM_TYPES = ("architecture", "workflow", "diagram")


def visual_problems(req: EnrichmentRequest, text: str) -> list[str]:
    """Is the instruction concrete enough for a renderer? (reported as warnings, never removed)"""
    low = text.lower()
    problems: list[str] = []
    names = [c for c in [*req.slide.concepts, *req.section.concepts] if c]
    names += [w for w in re.findall(r"[A-Za-z가-힣][A-Za-z가-힣0-9]{1,}", req.slide.title)]
    if not any(n.lower() in low for n in names):
        problems.append("슬라이드의 구체적인 요소(개념 이름)가 지시문에 없습니다.")
    if not any(w in low for w in _LAYOUT_WORDS):
        problems.append("배치 또는 도식 종류가 지시문에 없습니다.")
    if req.slide.slide_type in _DIAGRAM_TYPES and not any(w in low for w in _RELATION_WORDS):
        problems.append("요소 사이의 관계(화살표·연결 방향)가 지시문에 없습니다.")
    if len(text) < 30:
        problems.append("지시문이 너무 짧습니다.")
    return problems


@dataclass
class ValidatedSlide:
    content: EnrichedContent
    provenance: dict[str, FieldProvenance]
    warnings: list[str] = field(default_factory=list)
    adjusted: bool = False
    references: list[SourceLocation] = field(default_factory=list)
    grounded: bool = False
    failure_reason: str | None = None
    failure_message: str | None = None


class _Slide:
    """Working state for one slide."""

    def __init__(self, v: "GroundingValidator", req: EnrichmentRequest):
        self.v = v
        self.req = req
        self.policy = req.profile.source_policy
        self.warnings: list[str] = []
        texts = [req.slide.title, req.slide.learning_purpose, req.slide.key_message, *req.slide.key_points]
        texts += [s.text for s in req.source_context]
        self.ref_text = "\n".join(texts)
        self.ref_bigrams = bigrams(self.ref_text)
        self.ref_sentences = [s for t in texts for s in split_sentences(t)]
        self.ref_sentence_grams = [(s, bigrams(s)) for s in self.ref_sentences if len(s) >= 8]
        # words the slide itself introduces are not "new" (concept names, title words, ...)
        self.local_vocab = latin_tokens(self.ref_text) | latin_tokens(" ".join(req.slide.concepts))
        self.local_numbers = fact_numbers(self.ref_text)

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    # ---- segment level ---------------------------------------------------
    def segment_problem(self, seg: str, fld: str) -> str | None:
        """Why a sentence / list item must be removed, or None when it may stay."""
        if DESTRUCTIVE_RE.search(seg):
            return "위험하거나 파괴적일 수 있는 명령이 포함되어 제거했습니다."
        if violates_scope(seg, self.req.scope_notes):
            return "자료에서 다루지 않는다고 한 범위의 내용이라 제거했습니다."
        if fld in _FACTUAL_EXEMPT:
            return None
        for m in ENV_RE.finditer(seg):
            if norm_key(m.group(0)) not in self.v.corpus_key:
                return f"원문에 없는 실행 환경('{m.group(0)}')을 가정해 제거했습니다."
        if fld in ("practice_instruction", "code_explanation"):
            for m in VERSION_RE.finditer(seg):
                if m.group(0).lstrip("v") not in self.v.corpus_key:
                    return f"원문에 없는 버전('{m.group(0)}')을 가정해 제거했습니다."
        if self.policy == "source_only":
            for m in BACKTICK_RE.finditer(seg):
                if norm_key(m.group(1)) not in self.v.corpus_key:
                    return "원문에 없는 명령/코드라 제거했습니다(source_only)."
            new = self.new_terms(seg)
            if new:
                return f"원문에 없는 용어/수치({', '.join(sorted(new)[:3])})가 있어 제거했습니다(source_only)."
        conflict = self.conflicts_with_source(seg)
        if conflict:
            return "원문 문장과 반대되는 내용이라 제거했습니다."
        return None

    def new_terms(self, text: str) -> set[str]:
        vocab = self.v.vocab | self.local_vocab
        new = {t for t in latin_tokens(text) if not known_latin(t, vocab)}
        nums = {n for n in fact_numbers(text) if n not in self.v.numbers and n not in self.local_numbers}
        return new | nums

    def conflicts_with_source(self, seg: str) -> bool:
        if len(seg) < 12:
            return False
        g = bigrams(seg)
        neg = any(n in seg for n in _NEGATION)
        for s, sg in self.ref_sentence_grams:
            if jaccard(g, sg) >= 0.5 and any(n in s for n in _NEGATION) != neg:
                return True
        return False

    # ---- field level -------------------------------------------------------
    def clean_text(self, text: str | None, fld: str) -> str | None:
        if not text or not normalize(text):
            return None
        kept = []
        for seg in split_sentences(text):
            why = self.segment_problem(seg, fld)
            if why:
                self.warn(f"{fld}: {why}")
            else:
                kept.append(seg)
        return " ".join(kept) or None

    def clean_list(self, items: list[str], fld: str) -> list[str]:
        kept = []
        for it in items:
            it = normalize(it)
            if not it:
                continue
            why = self.segment_problem(it, fld)
            if why:
                self.warn(f"{fld}: {why}")
            else:
                kept.append(it)
        return kept

    def verify(self, claim: FieldProvenance | None, text: str, fld: str) -> FieldProvenance:
        """The provenance the text really deserves (a claim is only kept when it holds)."""
        if claim is None:
            if fld in _FACTUAL_EXEMPT or fld == "display_title":
                return P.llm_explanation
            self.warn(f"{fld}: 출처 표시가 없어 llm_inference로 처리했습니다.")
            return P.llm_inference
        if claim in _SOURCE_LIKE:
            cov = coverage(text, self.ref_bigrams)
            contained = norm_key(text) in self.v.corpus_key
            new = self.new_terms(text)
            if new:
                self.warn(f"{fld}: 원문에 없는 용어/수치({', '.join(sorted(new)[:3])})가 있어 원문 기반 표시를 낮췄습니다.")
                return P.llm_explanation
            if claim == P.source_grounded and not (contained or cov >= GROUNDED_MIN_COVERAGE):
                if cov >= PARAPHRASE_MIN_COVERAGE:
                    self.warn(f"{fld}: 원문과 정확히 일치하지 않아 paraphrased_source로 낮췄습니다.")
                    return P.paraphrased_source
                self.warn(f"{fld}: 원문 근거가 부족해 llm_explanation으로 낮췄습니다.")
                return P.llm_explanation
            if claim == P.paraphrased_source and not (contained or cov >= PARAPHRASE_MIN_COVERAGE):
                self.warn(f"{fld}: 원문과 겹치는 내용이 부족해 llm_explanation으로 낮췄습니다.")
                return P.llm_explanation
        return claim


class GroundingValidator:
    def __init__(self, source_text: str):
        self.source_text = source_text or ""
        self.corpus_key = norm_key(self.source_text)
        self.vocab = latin_tokens(self.source_text)
        self.numbers = fact_numbers(self.source_text)

    # ------------------------------------------------------------------
    def validate(self, req: EnrichmentRequest, out: LLMSlideOutput) -> ValidatedSlide:
        s = _Slide(self, req)
        allowed = set(req.allowed_fields)
        raw = out.model_dump()
        prov: dict[str, FieldProvenance] = {}
        content = EnrichedContent()

        def claim(f: str) -> FieldProvenance | None:
            return out.provenance.get(f)

        # -- fields that are not allowed for this slide / profile are dropped
        for f in ("display_title", "key_message", "body_points", "explanation", "example", "analogy",
                  "practice_instruction", "code_explanation", "visual_instruction", "presenter_notes",
                  "quiz_content", "summary_message"):
            has = bool(raw.get(f)) and (raw[f] != [] )
            if has and f not in allowed:
                why = "presenter_notes는 speaker_notes=none이라 제거했습니다." if f == "presenter_notes" else f"{f}는 이 슬라이드에 필요하지 않아 제거했습니다."
                s.warn(why)
                raw[f] = [] if f == "body_points" else None

        # -- simple text fields ---------------------------------------------
        for f in ("display_title", "key_message", "explanation", "example", "analogy", "visual_instruction",
                  "presenter_notes", "summary_message"):
            text = raw.get(f)
            if text is None:
                continue
            c = claim(f)
            if c == P.suggested:
                s.warn(f"{f}: suggested 표시된 필드는 내용을 비워 둡니다.")
                prov[f] = P.suggested
                continue
            if self.policy_drops(req, f, c, s):
                prov[f] = P.suggested
                continue
            cleaned = s.clean_text(text, f)
            if cleaned is None:
                if normalize(text):
                    prov[f] = P.suggested
                continue
            if req.profile.source_policy == "source_only" and f in ("key_message", "explanation", "summary_message"):
                cleaned = self.require_restatement(s, cleaned, f)
                if cleaned is None:
                    prov[f] = P.suggested
                    continue
            final = s.verify(c, cleaned, f)
            if self.policy_drops(req, f, final, s):
                prov[f] = P.suggested
                continue
            if f == "explanation":
                limit = int(req.limits.explanation_chars * 1.3)
                if len(cleaned) > limit:
                    cleaned = truncate_sentences(cleaned, limit) or clip_chars(cleaned, limit)
                    s.warn("explanation: 분량 제한을 넘어 문장 단위로 줄였습니다.")
            if f == "presenter_notes":
                cleaned = self.fit_notes(s, req, cleaned)
            if f == "visual_instruction":
                for problem in visual_problems(req, cleaned):
                    s.warn(f"visual_instruction: {problem}")
            setattr(content, f, cleaned)
            prov[f] = final

        # -- body points ----------------------------------------------------
        if raw.get("body_points"):
            c = claim("body_points")
            pts = s.clean_list(raw["body_points"], "body_points")
            if c == P.suggested:
                pts = []
            if req.profile.source_policy == "source_only":
                kept = []
                for p in pts:
                    if coverage(p, s.ref_bigrams) >= SOURCE_ONLY_MIN_COVERAGE:
                        kept.append(p)
                    else:
                        s.warn("body_points: 원문을 다시 쓴 내용이 아니라 제거했습니다(source_only).")
                pts = kept
            pts = [clip_chars(p, req.limits.point_chars) for p in pts]
            if len(pts) > req.limits.body_points_max:
                s.warn(f"body_points: {req.limits.body_points_max}개로 줄였습니다.")
                pts = pts[: req.limits.body_points_max]
            if pts and len(pts) < req.limits.body_points_min:
                s.warn(f"body_points: 최소 {req.limits.body_points_min}개보다 적습니다.")
            if pts and not self.policy_drops(req, "body_points", c, s):
                content.body_points = pts
                prov["body_points"] = s.verify(c, " ".join(pts), "body_points")
            else:
                prov["body_points"] = P.suggested

        # -- structured fields ----------------------------------------------
        if raw.get("practice_instruction"):
            pc = out.practice_instruction
            c = claim("practice_instruction")
            if c == P.suggested or self.policy_drops(req, "practice_instruction", c, s):
                prov["practice_instruction"] = P.suggested
            else:
                res = self.clean_practice(s, pc)
                if res is None:
                    prov["practice_instruction"] = P.suggested
                else:
                    content.practice_instruction = res
                    prov["practice_instruction"] = s.verify(c, _flat(res), "practice_instruction")
        if raw.get("code_explanation"):
            ce = out.code_explanation
            c = claim("code_explanation")
            if c == P.suggested or self.policy_drops(req, "code_explanation", c, s):
                prov["code_explanation"] = P.suggested
            else:
                res = self.clean_code(s, ce)
                if res is None:
                    prov["code_explanation"] = P.suggested
                else:
                    content.code_explanation = res
                    prov["code_explanation"] = s.verify(c, _flat(res), "code_explanation")
        if raw.get("quiz_content"):
            q = out.quiz_content
            c = claim("quiz_content")
            if c == P.suggested or self.policy_drops(req, "quiz_content", c, s):
                prov["quiz_content"] = P.suggested
            else:
                res = self.clean_quiz(s, q)
                if res is None:
                    prov["quiz_content"] = P.suggested
                else:
                    content.quiz_content = res
                    prov["quiz_content"] = s.verify(c, _flat(res), "quiz_content")

        # -- core fields the type calls for but that ended empty become placeholders
        for f in core_fields(req.slide.slide_type):
            if f in allowed and _is_empty(getattr(content, f)):
                if prov.get(f) != P.suggested:
                    prov[f] = P.suggested
                s.warn(f"{f}: 원문 근거 또는 정책상 작성할 수 없어 비워 두었습니다(suggested).")

        result = ValidatedSlide(content=content, provenance=prov, warnings=s.warnings)
        result.adjusted = bool(s.warnings)

        # -- required fields -------------------------------------------------
        if not content.key_message:
            result.failure_reason = "required_field_missing"
            result.failure_message = "핵심 메시지를 검증 기준에 맞게 만들지 못해 기존 슬라이드 내용을 유지합니다."
            return result
        if not content.display_title:
            content.display_title = req.slide.title  # cosmetic field: keep the planner's title

        refs: list[SourceLocation] = []
        seen: set[tuple] = set()
        for loc in [req.slide.source_reference, *[sn.location for sn in req.source_context]]:
            if loc is None:
                continue
            key = (loc.section_id, loc.line, loc.page)
            if key not in seen:
                seen.add(key)
                refs.append(loc)
        result.references = refs[:5]
        result.grounded = prov.get("key_message") in _SOURCE_LIKE and bool(refs)
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def policy_drops(req: EnrichmentRequest, f: str, c: FieldProvenance | None, s: _Slide) -> bool:
        """source_only: content that is by nature external is removed."""
        if req.profile.source_policy != "source_only" or f in _FACTUAL_EXEMPT:
            return False
        if f == "analogy":
            s.warn("analogy: 원문에 없는 비유라 제거했습니다(source_only).")
            return True
        if c == P.llm_example:
            s.warn(f"{f}: LLM이 만든 예시는 source_only에서 허용되지 않아 제거했습니다.")
            return True
        if f == "example" and c is not None and c not in _SOURCE_LIKE:
            s.warn("example: 원문에 없는 예시라 제거했습니다(source_only).")
            return True
        return False

    @staticmethod
    def require_restatement(s: _Slide, text: str, f: str) -> str | None:
        kept = []
        for seg in split_sentences(text):
            if len(seg) < 12 or coverage(seg, s.ref_bigrams) >= SOURCE_ONLY_MIN_COVERAGE:
                kept.append(seg)
            else:
                s.warn(f"{f}: 원문을 다시 쓴 내용이 아닌 문장을 제거했습니다(source_only).")
        return " ".join(kept) or None

    @staticmethod
    def fit_notes(s: _Slide, req: EnrichmentRequest, text: str) -> str:
        n = req.notes
        if n.mode == "none":
            return ""
        sentences = split_sentences(text)
        if n.mode == "concise" and len(sentences) > n.max_sentences:
            s.warn(f"presenter_notes: {n.max_sentences}문장으로 줄였습니다.")
            text = " ".join(sentences[: n.max_sentences])
        if len(text) > n.max_chars:
            cut = truncate_sentences(text, n.max_chars) or clip_chars(text, n.max_chars)
            s.warn(f"presenter_notes: 슬라이드 시간({req.slide.estimated_explanation_time}초)에 비해 길어 줄였습니다.")
            text = cut
        if len(split_sentences(text)) < n.min_sentences:
            s.warn("presenter_notes: 권장 문장 수보다 짧습니다.")
        elif n.mode == "full" and n.target_chars and len(text) < n.target_chars * SHORT_NOTES_RATIO:
            s.warn(
                f"presenter_notes: 슬라이드 시간({req.slide.estimated_explanation_time}초)에 비해 짧습니다"
                f"({len(text)}자, 목표 약 {n.target_chars}자)."
            )
        return text

    # -- structured fields ---------------------------------------------------
    @staticmethod
    def clean_practice(s: _Slide, p: PracticeContent | None) -> PracticeContent | None:
        if p is None:
            return None
        steps = []
        for st in s.clean_list(p.steps, "practice_instruction"):
            if SYSTEM_CHANGE_RE.search(st) and not st.startswith(SYSTEM_TAG):
                st = SYSTEM_TAG + st
                s.warn("practice_instruction: 시스템 설정을 바꾸는 단계에 표시를 붙였습니다.")
            steps.append(st)
        if not steps:
            return None
        out = PracticeContent(
            goal=s.clean_text(p.goal, "practice_instruction"),
            prerequisites=s.clean_list(p.prerequisites, "practice_instruction"),
            steps=steps,
            expected_result=s.clean_text(p.expected_result, "practice_instruction"),
            cautions=s.clean_list(p.cautions, "practice_instruction"),
        )
        return out

    @staticmethod
    def clean_code(s: _Slide, c: CodeExplanation | None) -> CodeExplanation | None:
        if c is None:
            return None
        out = CodeExplanation(
            purpose=s.clean_text(c.purpose, "code_explanation"),
            key_lines=s.clean_list(c.key_lines, "code_explanation"),
            execution_flow=s.clean_list(c.execution_flow, "code_explanation"),
            cautions=s.clean_list(c.cautions, "code_explanation"),
        )
        return out if (out.purpose or out.key_lines) else None

    @staticmethod
    def clean_quiz(s: _Slide, q: QuizContent | None) -> QuizContent | None:
        if q is None or not q.question:
            return None
        question = s.clean_text(q.question, "quiz_content")
        answer = s.clean_text(q.answer, "quiz_content")
        if not question or not answer:
            return None
        choices = s.clean_list(q.choices, "quiz_content")
        if choices and norm_key(answer) not in {norm_key(c) for c in choices}:
            s.warn("quiz_content: 정답이 선택지에 없어 제거했습니다.")
            return None
        return QuizContent(
            question=question, choices=choices, answer=answer,
            explanation=s.clean_text(q.explanation, "quiz_content"),
        )


def _flat(model) -> str:
    parts: list[str] = []
    for v in model.model_dump().values():
        parts.extend(v if isinstance(v, list) else [v])
    return " ".join(p for p in parts if p)


def _is_empty(v) -> bool:
    if v is None:
        return True
    if isinstance(v, (list, str)):
        return not v
    return False
