"""LecturePlanner (STAGE 3): SourceAnalysis + LectureProfile -> LecturePlan.

Rule based and deterministic (no LLM): the same inputs always give the same plan.
All option -> rule decisions are in `planning_policy.derive_policy`; this module
turns that policy plus the analysed source into sections, time and slides.

Grounding: the planner selects and arranges what the source contains. Definitions,
example excerpts, code and "points to stress" are SOURCE text with a reference. When
something is wanted but the source has none, the plan holds an empty `suggested`
slot (only if source_policy is source_first/expanded) or leaves it out with a
warning (source_only). It never writes new facts.

Algorithm
  1. teachable units = concepts (or, if none were found, source sections)
  2. time budget: total minus intro/summary/quiz/practice shares -> greedy pick of
     units by (importance + category bias) until the budget is used
  3. arrange units by main topic (source order, prerequisites first)
  4. attach terms / examples / code / quiz; add practice, comparison, caution,
     prerequisite, quiz and summary sections as the policy demands
  5. distribute minutes (integers, sum == duration), estimate slides
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass

from ..errors import AppError, InvalidDurationDistribution, LecturePlanningError
from ..models.lecture_plan import (
    LecturePlan,
    LectureSection,
    Origin,
    PlanCode,
    PlanExample,
    PlanMetrics,
    PlanTerm,
    PracticeActivity,
    PresentationHints,
    QuizSpec,
    SectionKind,
    SourceConstraint,
)
from ..models.lecture_profile import CodeLevel, LectureProfile
from ..models.source import ConceptCategory as C
from ..models.source import MainTopic, SourceAnalysis, SourceLocation
from ..storage.project_store import utcnow
from .planning_policy import PlanningPolicy, derive_policy

logger = logging.getLogger("ailecturegen")

MIN_SECTION_SECONDS_PER_SLIDE = 30  # a slide gets at least 30 s
MAX_PREREQUISITE_UNITS = 4
MAX_CAUTION_POINTS = 5
CAUTION_WORDS = (
    "주의", "유의", "위험", "한계", "제약", "오류", "단점", "문제", "고려사항", "trade-off", "반드시", "명심", "장애",
)
PRACTICE_CATEGORIES = (C.process, C.practice, C.code, C.architecture)

_STYLE_PURPOSE = {
    "understand": "정의와 의미를 이해한다",
    "apply": "동작 방식을 이해하고 적용 방법을 익힌다",
    "analyze": "내부 동작 원리와 설계상의 trade-off를 이해한다",
    "perform": "실습에 필요한 핵심만 간결하게 익힌다",
    "exam": "시험에 나오는 핵심 내용을 정확히 정리한다",
}
_STYLE_OBJECTIVE = {
    "understand": "‘{n}’ 개념을 설명할 수 있다.",
    "apply": "‘{n}’의 동작 방식을 설명하고 사례에 적용할 수 있다.",
    "analyze": "‘{n}’의 구조와 trade-off를 분석할 수 있다.",
    "perform": "‘{n}’ 실습을 직접 수행하고 결과를 확인할 수 있다.",
    "exam": "‘{n}’의 핵심 내용을 구분하고 문제에 적용할 수 있다.",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@dataclass
class Unit:
    """A teachable unit: a concept, or a source section when no concepts exist."""

    name: str
    importance: int
    category: C
    loc: SourceLocation
    description: str
    prerequisites: list[str]
    mentions: int
    position: int  # source order
    is_concept: bool = True


def allocate_minutes(weights: list[float], total: int) -> list[int]:
    """Integer minutes (>=1 each) proportional to `weights`, summing to exactly `total`."""
    n = len(weights)
    if n == 0 or total < n:
        raise InvalidDurationDistribution()
    s = sum(weights) or 1.0
    floats = [w / s * total for w in weights]
    base = [max(1, int(f)) for f in floats]
    diff = total - sum(base)
    while diff != 0:
        if diff > 0:
            i = max(range(n), key=lambda k: floats[k] - base[k])
            base[i] += 1
            diff -= 1
        else:
            cands = [k for k in range(n) if base[k] > 1]
            if not cands:
                raise InvalidDurationDistribution()
            i = max(cands, key=lambda k: base[k] - floats[k])
            base[i] -= 1
            diff += 1
    return base


def _join(names: list[str], n: int = 2) -> str:
    return "·".join(names[:n])


# ---------------------------------------------------------------------------
class LecturePlanner:
    name = "rule-v1"

    def plan(self, analysis: SourceAnalysis, profile: LectureProfile) -> LecturePlan:
        try:
            return self._plan(analysis, profile)
        except AppError:
            raise
        except ValueError as exc:  # pydantic validation of the plan invariants
            logger.warning("Invalid plan: %s", exc)
            raise InvalidDurationDistribution() from exc
        except Exception as exc:  # never leak internals
            logger.exception("Lecture planning failed", exc_info=exc)
            raise LecturePlanningError() from exc

    # ------------------------------------------------------------------
    def _plan(self, a: SourceAnalysis, p: LectureProfile) -> LecturePlan:
        pol = derive_policy(p)
        D = p.duration_minutes
        warnings: list[str] = list(p.warnings)
        sec_order = {s.id: s.order for s in a.sections}

        units = self._units(a, sec_order, warnings)
        by_name = {u.name: u for u in units}

        # ---- 2. time budget and unit selection
        practice_share = pol.practice_share if pol.practice_blocks else 0.0
        fixed_share = pol.intro_share + pol.summary_share + pol.final_quiz_share + practice_share
        total_s = D * 60
        special_s = 0.0
        if pol.comparison_section and len(units) >= 2:
            special_s += 120 + 30 * min(6, len(units))
        caution_points = self._caution_points(a)
        if pol.caution_section:
            special_s += 60 * max(1, len(caution_points))
        available_s = max(total_s * (1 - fixed_share) - special_s, total_s * 0.2)

        code_concepts = self._code_index(a, units)
        selected = self._select(units, pol, available_s, code_concepts, a)
        if len(selected) == len(units) and sum(self._unit_seconds(u, pol, u.name in code_concepts, a) for u in selected) < 0.6 * available_s:
            warnings.append("자료의 분량이 강의 시간에 비해 적어 섹션당 시간이 길게 배정되었습니다.")

        # ---- 3./4. sections
        used_examples: set[int] = set()
        used_code: set[int] = set()
        missing = {"example": 0, "code": 0}
        sections: list[LectureSection] = []  # without ids/time yet
        weights: dict[int, float] = {}  # id(section) -> unit seconds (None-weight = fixed)
        fixed_seconds: dict[int, float] = {}

        def new_section(**kw) -> LectureSection:
            s = LectureSection(id="tmp", order=0, duration_minutes=1, estimated_slides=1, **kw)
            sections.append(s)
            return s

        # intro
        intro = new_section(
            title="강의 소개와 학습 목표",
            purpose=f"‘{a.title}’의 전체 흐름과 학습 목표를 안내한다.",
            kind=SectionKind.intro,
            importance=3,
            teaching_notes=[f"자료의 범위 제한: {n.text}" for n in a.scope_notes[:3]],
        )
        intro.estimated_slides = 2
        fixed_seconds[id(intro)] = max(60.0, total_s * pol.intro_share)

        # prerequisite (background) section
        background = self._background(selected, by_name, pol)
        if background:
            names = [u.name for u in background]
            terms = [self._term(u, a) for u in background]
            pre = new_section(
                title="선수 지식 정리",
                purpose=f"본 내용을 이해하는 데 필요한 배경 개념(‘{_join(names, 3)}’)을 먼저 정리한다.",
                kind=SectionKind.prerequisite,
                importance=max(u.importance for u in background),
                concepts=names,
                terms=terms,
                source_section_ids=sorted({u.loc.section_id for u in background if u.loc.section_id}),
            )
            pre.estimated_slides = max(1, math.ceil(len(names) * 0.5) + math.ceil(len(terms) / 2))
            # background is a quick recap: half the time of a full concept, no examples/code
            weights[id(pre)] = sum(
                pol.concept_seconds * (0.7 + 0.15 * u.importance) * 0.5 + pol.definition_seconds
                for u in background
            )

        # concept sections
        term_targets = self._top_share(selected, pol.definition_ratio)
        example_targets = self._top_share(selected, pol.example_ratio)
        concept_sections: list[tuple[LectureSection, list[Unit]]] = []
        for topic_title, topic_sections, topic_keys, grp_units in self._group(selected, a, sec_order):
            chunks = [
                grp_units[i : i + pol.max_concepts_per_section]
                for i in range(0, len(grp_units), pol.max_concepts_per_section)
            ]
            for ci, chunk in enumerate(chunks):
                title = self._section_title(topic_title, topic_keys, chunk, ci, len(chunks))
                sec = self._concept_section(
                    title, topic_sections, chunk, a, pol, term_targets, example_targets,
                    used_examples, used_code, code_concepts, missing,
                )
                weights[id(sec)] = sum(
                    self._unit_seconds(u, pol, u.name in code_concepts, a) for u in chunk
                ) + (pol.checkpoint_seconds if sec.quiz else 0)
                sections.append(sec)
                concept_sections.append((sec, chunk))
        # (new_section appended nothing for these; concept sections were appended above)

        # practice blocks
        practice_secs = self._practice(concept_sections, pol, a, used_examples, used_code, code_concepts)
        practice_total_s = total_s * practice_share
        acts = sum(len(s.practice) for s, _ in practice_secs) or 1
        for sec, after in practice_secs:
            fixed_seconds[id(sec)] = practice_total_s * len(sec.practice) / acts
            pos = next(i for i, s in enumerate(sections) if s is after)  # identity, not ==
            sections.insert(pos + 1, sec)

        # comparison / caution (source based)
        ranked = sorted(selected, key=lambda u: (-u.importance, u.position))
        if pol.comparison_section and len(ranked) >= 2:
            top = ranked[:6]
            cmp_sec = new_section(
                title="핵심 개념 비교 정리",
                purpose="자주 혼동되는 핵심 개념을 표로 나란히 비교해 구분한다.",
                kind=SectionKind.comparison,
                importance=4,
                concepts=[u.name for u in top],
                teaching_notes=["비교표 중심으로 정리한다."],
                source_section_ids=sorted({u.loc.section_id for u in top if u.loc.section_id}),
            )
            cmp_sec.estimated_slides = 2 + (1 if pol.diagram_slides else 0)
            weights[id(cmp_sec)] = 120 + 30 * len(top)
        if pol.caution_section:
            cau = self._caution_section(a, pol, caution_points, warnings)
            if cau is not None:
                sections.append(cau[0])
                weights[id(cau[0])] = cau[1]

        # final quiz
        if pol.final_quiz and ranked:
            q = min(20, max(3, D // 10))
            quiz = new_section(
                title="확인 퀴즈",
                purpose="학습한 핵심 개념의 이해도를 점검한다.",
                kind=SectionKind.quiz,
                importance=4,
                concepts=[u.name for u in ranked[:8]],
                quiz=QuizSpec(kind="final", question_count=q, targets=[u.name for u in ranked[:8]]),
            )
            quiz.estimated_slides = max(1, math.ceil(q / 3))
            fixed_seconds[id(quiz)] = total_s * pol.final_quiz_share

        # summary
        summary = new_section(
            title="핵심 정리와 시험 대비 포인트" if pol.objective_style == "exam" else "핵심 정리",
            purpose="강의의 핵심 개념을 다시 짚고 다음 학습으로 연결한다.",
            kind=SectionKind.summary,
            importance=3,
            concepts=[u.name for u in ranked[:6]],
        )
        summary.estimated_slides = 1 + (1 if D >= 60 else 0)
        fixed_seconds[id(summary)] = total_s * pol.summary_share

        # order: the sections above were appended in build order; comparison/caution/
        # quiz/summary must come last in that order (already so), intro first.
        sections = self._fit_section_count(sections, D, warnings, weights, fixed_seconds)

        # ---- 5. time and slides
        fixed_total = sum(fixed_seconds.get(id(s), 0.0) for s in sections)
        remaining = max(total_s - fixed_total, 0.0)
        unit_total = sum(weights.get(id(s), 0.0) for s in sections) or 1.0
        floats = []
        for s in sections:
            if id(s) in fixed_seconds:
                floats.append(fixed_seconds[id(s)] / 60)
            else:
                floats.append(remaining * weights.get(id(s), 0.0) / unit_total / 60)
        minutes = allocate_minutes([max(f, 0.01) for f in floats], D)

        for i, (s, m) in enumerate(zip(sections, minutes), start=1):
            s.id = f"sec{i:02d}"
            s.order = i
            s.duration_minutes = m
            s.estimated_slides = max(1, min(s.estimated_slides, m * 60 // MIN_SECTION_SECONDS_PER_SLIDE))
            if not s.source_section_ids and s.kind == SectionKind.practice:
                s.source_section_ids = sorted(
                    {c.source_reference.section_id for c in s.practice if c.source_reference and c.source_reference.section_id}
                )
            self._scope_notes(s, a)

        if missing["example"]:
            warnings.append(
                f"자료에 해당 예제가 없어 예제 {missing['example']}개를 생략했습니다"
                "(source_only: 자료에 없는 내용은 추가하지 않습니다)."
            )
        if missing["code"]:
            warnings.append("자료에 코드 예제가 없어 코드 항목을 생략했습니다.")

        objectives = self._objectives(sections, by_name, pol, D)
        metrics = self._metrics(sections)
        return LecturePlan(
            id=uuid.uuid4().hex,
            project_id=p.project_id,
            title=a.title,
            audience=p.audience_level.value,
            duration_minutes=D,
            difficulty=p.difficulty.value,
            lecture_type=p.lecture_type.value,
            explanation_depth=p.explanation_depth.value,
            source_policy=p.source_policy.value,
            learning_objectives=objectives,
            estimated_slide_count=sum(s.estimated_slides for s in sections),
            sections=sections,
            metrics=metrics,
            presentation_hints=PresentationHints(
                lecture_tone=p.lecture_tone.value,
                speaker_notes=p.speaker_notes.value,
                visual_level=p.visual_level.value,
                slide_density=p.slide_density.value,
                source_policy=p.source_policy.value,
            ),
            source_constraints=[
                SourceConstraint(text=n.text, source_reference=n.source_location) for n in a.scope_notes
            ],
            warnings=warnings,
            profile_id=p.id,
            source_id=a.source_id,
            planner=self.name,
            generated_at=utcnow(),
        )

    # ------------------------------------------------------------------ units
    def _units(self, a: SourceAnalysis, sec_order: dict, warnings: list[str]) -> list[Unit]:
        units: list[Unit] = []
        for c in a.concepts:
            sid = c.source_location.section_id
            units.append(
                Unit(
                    name=c.name,
                    importance=c.importance,
                    category=c.category,
                    loc=c.source_location,
                    description=c.description,
                    prerequisites=list(c.prerequisite),
                    mentions=c.mention_count,
                    position=sec_order.get(sid, 0) * 100000 + (c.source_location.line or 0),
                )
            )
        if units:
            return units
        # No concepts (e.g. a Korean-only document analysed without an LLM): teach by section.
        secs = [s for s in a.sections if s.char_count > 0]
        if any(s.level >= 2 for s in secs):
            secs = [s for s in secs if s.level >= 2]
        if not secs:
            raise LecturePlanningError(
                "분석 결과에 강의 계획을 만들 수 있는 내용(개념 또는 섹션)이 없습니다. "
                "다른 강의자료를 사용하거나 ANALYZER_MODE=hybrid로 분석해 보세요."
            )
        warnings.append("분석된 개념이 없어 자료의 섹션 제목을 학습 단위로 사용했습니다. 개념 추출을 개선하면 계획이 더 정확해집니다.")
        for s in secs:
            units.append(
                Unit(
                    name=s.title,
                    importance=3,
                    category=C.principle,
                    loc=SourceLocation(section_id=s.id, section_title=s.title),
                    description=s.summary,
                    prerequisites=[],
                    mentions=1,
                    position=s.order * 100000,
                    is_concept=False,
                )
            )
        return units

    @staticmethod
    def _block_matches(u: Unit, blk) -> bool:
        """A code block belongs to a unit if it sits in the unit's source section, or
        the unit's name appears in it as a whole word."""
        if blk.section_id and u.loc.section_id == blk.section_id:
            return True
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(u.name)}(?![A-Za-z0-9_])", blk.code, re.I) is not None

    def _code_index(self, a: SourceAnalysis, units: list[Unit]) -> set[str]:
        """Names of units that have a source code example."""
        return {u.name for blk in a.code_examples for u in units if self._block_matches(u, blk)}

    def _unit_seconds(self, u: Unit, pol: PlanningPolicy, has_code: bool, a) -> float:
        """Expected teaching time of one unit (definition/example cost weighted by their share)."""
        s = pol.concept_seconds * (0.7 + 0.15 * u.importance)
        s += pol.definition_seconds * pol.definition_ratio
        s += pol.example_seconds * pol.example_ratio
        if has_code and pol.code_level != CodeLevel.none:
            s += pol.code_seconds
        if pol.checkpoint_quiz:
            s += 15
        return s

    def _select(self, units, pol, available_s, code_concepts, a) -> list[Unit]:
        def score(u: Unit) -> float:
            return u.importance + pol.category_bias.get(u.category, 0.0) + min(u.mentions, 20) / 100

        ranked = sorted(units, key=lambda u: (-score(u), u.position))
        chosen: list[Unit] = []
        used = 0.0
        for u in ranked:
            sec = self._unit_seconds(u, pol, u.name in code_concepts, a)
            if used + sec > available_s and len(chosen) >= pol.min_concepts:
                break
            chosen.append(u)
            used += sec
        return sorted(chosen, key=lambda u: u.position)

    def _background(self, selected: list[Unit], by_name: dict, pol: PlanningPolicy) -> list[Unit]:
        if not pol.prerequisite_section:
            return []
        have = {u.name for u in selected}
        need: dict[str, Unit] = {}
        for u in selected:
            for n in u.prerequisites:
                if n not in have and n in by_name:
                    need[n] = by_name[n]
        return sorted(need.values(), key=lambda u: (-u.importance, u.position))[:MAX_PREREQUISITE_UNITS]

    # ---------------------------------------------------------------- grouping
    @staticmethod
    def _section_title(topic_title: str, topic_keys: list[str], chunk: list[Unit], i: int, n: int) -> str:
        """The topic title says what the SOURCE group is about; it is only used when this
        section really contains most of that group's key concepts. Otherwise the title
        names the concepts actually taught here (a title must not promise content the
        section does not have)."""
        names = [u.name for u in chunk]
        core = topic_keys[:3]
        covered = sum(1 for k in core if k in names)
        title = topic_title if core and covered >= min(2, len(core)) else " · ".join(names[:3])
        return title if n == 1 else f"{title} ({i + 1}/{n})"

    def _group(self, selected: list[Unit], a: SourceAnalysis, sec_order: dict):
        """[(title, section_ids, key_concepts, units)] following the source's main topics."""
        topics: list[MainTopic] = list(a.main_topics)
        if not topics:
            topics = [
                MainTopic(
                    title=a.title,
                    summary="",
                    section_ids=[s.id for s in a.sections],
                    key_concepts=[],
                )
            ]
        sec_to_topic = {sid: i for i, t in enumerate(topics) for sid in t.section_ids}
        key_to_topic = {}
        for i, t in enumerate(topics):
            for k in t.key_concepts:
                key_to_topic.setdefault(k, i)
        buckets: dict[int, list[Unit]] = {}
        for u in selected:
            idx = sec_to_topic.get(u.loc.section_id or "")
            if idx is None:
                idx = key_to_topic.get(u.name, 0)
            buckets.setdefault(idx, []).append(u)
        out = []
        for idx in sorted(buckets):
            units = self._prereq_order(sorted(buckets[idx], key=lambda u: u.position))
            out.append((topics[idx].title, list(topics[idx].section_ids), list(topics[idx].key_concepts), units))
        return out

    @staticmethod
    def _prereq_order(units: list[Unit]) -> list[Unit]:
        """Keep source order, but never teach a concept before its prerequisite in the same group."""
        out: list[Unit] = []
        names = {u.name for u in units}
        pending = list(units)
        guard = 0
        while pending and guard < 1000:
            guard += 1
            u = pending.pop(0)
            waiting = [n for n in u.prerequisites if n in names and n not in {x.name for x in out}]
            if waiting and any(x.name in waiting for x in pending):
                pending.append(u)  # try again after its prerequisites
                continue
            out.append(u)
        out.extend(pending)
        return out

    # ---------------------------------------------------- concept section body
    def _term(self, u: Unit, a: SourceAnalysis) -> PlanTerm:
        d = next((x for x in a.definitions if x.term == u.name), None)
        if d is not None:
            return PlanTerm(term=u.name, text=d.definition, origin=Origin.source, source_reference=d.source_location)
        return PlanTerm(term=u.name, text=u.description, origin=Origin.source, source_reference=u.loc)

    @staticmethod
    def _top_share(selected: list[Unit], ratio: float) -> set[str]:
        """Names of the most important `ratio` share of the selected units."""
        n = round(ratio * len(selected))
        ranked = sorted(selected, key=lambda u: (-u.importance, u.position))
        return {u.name for u in ranked[:n]}

    def _concept_section(
        self, title, topic_sections, chunk, a, pol, term_targets, example_targets,
        used_examples, used_code, code_concepts, missing,
    ) -> LectureSection:
        names = [u.name for u in chunk]
        terms = [self._term(u, a) for u in chunk if u.name in term_targets]

        examples: list[PlanExample] = []
        for u in chunk:
            if u.name not in example_targets:
                continue
            src = next(
                (
                    (i, e)
                    for i, e in enumerate(a.examples)
                    if i not in used_examples and u.name in e.related_concepts
                ),
                None,
            ) or next(
                (
                    (i, e)
                    for i, e in enumerate(a.examples)
                    if i not in used_examples and u.loc.section_id and e.source_location.section_id == u.loc.section_id
                ),
                None,
            )
            first = pol.example_first and u.category in (C.process, C.architecture, C.principle) and u.importance >= 3
            if src is not None:
                used_examples.add(src[0])
                examples.append(
                    PlanExample(
                        title=f"{'사례' if src[1].kind == 'case_study' else '예시'}: {u.name}",
                        text=src[1].text,
                        origin=Origin.source,
                        concepts=[u.name],
                        example_first=first,
                        source_reference=src[1].source_location,
                    )
                )
            elif pol.allow_suggested:
                examples.append(
                    PlanExample(
                        title=f"{'산업 사례' if pol.industry_examples else '예시'}: {u.name}",
                        origin=Origin.suggested,
                        concepts=[u.name],
                        example_first=first,
                    )
                )
            else:
                missing["example"] += 1

        code: list[PlanCode] = []
        if pol.code_level != CodeLevel.none:
            # A code block is taught with the topic it sits in, never elsewhere.
            for i, blk in enumerate(a.code_examples):
                if i in used_code or (blk.section_id and blk.section_id not in topic_sections):
                    continue
                target = next((u for u in chunk if self._block_matches(u, blk)), None)
                if target is None:
                    continue
                used_code.add(i)
                code.append(
                    PlanCode(
                        language=blk.language,
                        mode=pol.code_level.value,
                        origin=Origin.source,
                        concepts=[target.name],
                        source_reference=blk.location,
                    )
                )
            if not a.code_examples and pol.code_level != CodeLevel.none:
                missing["code"] = 1

        quiz = None
        if pol.checkpoint_quiz:
            top = sorted(chunk, key=lambda u: -u.importance)[:3]
            quiz = QuizSpec(kind="checkpoint", question_count=max(1, len(chunk) // 2), targets=[u.name for u in top])

        points = [
            ip.text
            for ip in a.important_points
            if ip.source_location.section_id in topic_sections
        ][:2]

        sec = LectureSection(
            id="tmp",
            order=0,
            title=title,
            purpose=f"‘{_join(names, 3)}’ — {_STYLE_PURPOSE[pol.objective_style]}.",
            kind=SectionKind.concept,
            duration_minutes=1,
            importance=max(u.importance for u in chunk),
            concepts=names,
            terms=terms,
            examples=examples,
            code=code,
            quiz=quiz,
            source_points=points,
            source_section_ids=list(topic_sections),
            estimated_slides=1,
        )
        n = len(chunk)
        slides = max(1, round(n * pol.slides_per_concept))
        slides += math.ceil(len(terms) / 2) + len(examples) + len(code) + (1 if quiz else 0)
        if pol.diagram_slides and any(u.category in (C.process, C.architecture) for u in chunk):
            slides += 1
        sec.estimated_slides = slides
        return sec

    # --------------------------------------------------------------- practice
    def _practice(self, concept_sections, pol, a, used_examples, used_code, code_concepts):
        """[(practice_section, concept_section_it_follows)]"""
        if not pol.practice_blocks or not concept_sections:
            return []
        n_blocks = min(pol.practice_blocks, len(concept_sections))
        size = len(concept_sections) / n_blocks
        out = []
        for b in range(n_blocks):
            lo, hi = round(b * size), round((b + 1) * size)
            group = concept_sections[lo:hi]
            if not group:
                continue
            units = [u for _, chunk in group for u in chunk]
            ranked = sorted(
                units,
                key=lambda u: (-(u.importance + (1 if u.category in PRACTICE_CATEGORIES else 0)), u.position),
            )[: pol.activities_per_block]
            activities = [self._activity(u, pol, a, code_concepts) for u in ranked]
            steps = sum(len(x.steps) for x in activities)
            sec = LectureSection(
                id="tmp",
                order=0,
                title=f"실습 {b + 1}: {_join([u.name for u in ranked], 2)}",
                purpose=(
                    "단계별로 직접 수행하며 개념을 확인한다."
                    if pol.steps_per_activity >= 4
                    else "짧은 적용 활동으로 이해를 확인한다."
                ),
                kind=SectionKind.practice,
                duration_minutes=1,
                importance=max(u.importance for u in ranked),
                concepts=[u.name for u in ranked],
                practice=activities,
                source_section_ids=sorted({u.loc.section_id for u in ranked if u.loc.section_id}),
                estimated_slides=1,
            )
            # One slide per activity (its numbered steps), plus one result-check slide when the
            # practice is guided/full. Practice is mostly hands-on TIME, not slides (spec).
            sec.estimated_slides = len(activities) + (1 if pol.steps_per_activity >= 4 else 0)
            out.append((sec, group[-1][0]))
        return out

    def _activity(self, u: Unit, pol: PlanningPolicy, a: SourceAnalysis, code_concepts: set[str]) -> PracticeActivity:
        ex = next((e for e in a.examples if u.name in e.related_concepts), None)
        # only EXECUTABLE code turns an activity into "run the code"; snippets are just shown
        has_code = u.name in code_concepts and pol.code_level == CodeLevel.executable
        n = pol.steps_per_activity
        name = u.name
        if n <= 2:
            steps = [
                f"‘{name}’의 핵심 내용을 자료에서 찾아 확인한다.",
                "확인한 내용을 자료의 예시에 적용해 본다." if ex else "확인한 내용을 간단한 상황에 적용해 본다.",
            ]
        elif n <= 4:
            steps = [
                "실습 목표와 준비물을 확인한다.",
                f"강사의 시연을 따라 ‘{name}’ 절차를 수행한다.",
                "수행 결과를 확인한다.",
                "결과를 자료의 설명과 비교한다.",
            ]
        else:
            steps = [
                "실습 환경과 준비물을 점검한다.",
                f"자료의 설명에 따라 ‘{name}’ 절차를 단계별로 수행한다.",
                "각 단계의 결과를 확인하고 기록한다.",
                "조건을 바꿔 다시 수행한다.",
                "예상과 다른 결과가 나오면 원인을 분석한다.",
                "수행 내용과 배운 점을 정리한다.",
            ]
        mode = "exercise"
        if has_code and n >= 2:
            mode = "code"
            steps[1] = f"제공된 코드 예제를 실행하고 ‘{name}’의 동작을 확인한다."
        basis = "source_code" if has_code else "source_example" if ex else "source_concept"
        return PracticeActivity(
            title=f"{name} 적용 실습",
            objective=f"‘{name}’ 개념을 직접 적용해 이해를 확인한다.",
            steps=steps,
            concepts=[name],
            mode=mode,
            basis=basis,
            source_reference=(ex.source_location if ex else u.loc),
        )

    # ------------------------------------------------------------------ caution
    @staticmethod
    def _caution_points(a: SourceAnalysis) -> list:
        """Source sentences about risks/limits. Generic emphasis ("핵심", "목표") is not a
        caution, so it never fills a caution section."""
        return [p for p in a.important_points if any(k in p.text for k in CAUTION_WORDS)][:MAX_CAUTION_POINTS]

    def _caution_section(self, a, pol, points, warnings):
        cautions = [c for c in a.concepts if c.category == C.caution][:MAX_CAUTION_POINTS]
        if not points and not cautions:
            if not pol.allow_suggested:
                warnings.append(f"자료에 ‘{pol.caution_title}’에 해당하는 내용이 없어 해당 섹션을 생략했습니다.")
                return None
            sec = LectureSection(
                id="tmp", order=0, title=pol.caution_title, kind=SectionKind.caution,
                purpose="자료에서 다루지 않은 한계와 흔한 오류를 강사가 일반적인 내용으로 보충한다.",
                duration_minutes=1, importance=3, estimated_slides=1,
                teaching_notes=["자료에 근거가 없는 항목입니다(suggested). 강사가 보충하세요."],
            )
            return sec, 120.0
        sec = LectureSection(
            id="tmp", order=0, title=pol.caution_title, kind=SectionKind.caution,
            purpose="자료가 강조한 주의점과 한계를 정리해 흔한 오류를 예방한다.",
            duration_minutes=1, importance=4,
            concepts=[c.name for c in cautions],
            source_points=[pt.text for pt in points],
            source_section_ids=sorted({pt.source_location.section_id for pt in points if pt.source_location.section_id}),
            estimated_slides=max(1, math.ceil(max(len(points), len(cautions)) / 2)),
        )
        return sec, 60.0 * max(1, len(points))

    # ---------------------------------------------------------- post-processing
    def _fit_section_count(self, sections, D, warnings, weights, fixed):
        """Every section needs >= 1 minute: drop the least valuable ones if D is tiny."""
        if len(sections) <= D:
            return sections
        rank = {
            SectionKind.practice: 0, SectionKind.comparison: 1, SectionKind.caution: 2,
            SectionKind.quiz: 3, SectionKind.prerequisite: 4, SectionKind.concept: 5,
        }
        secs = list(sections)
        dropped = 0
        while len(secs) > D:
            cands = [s for s in secs if s.kind in rank]
            if not cands:
                raise InvalidDurationDistribution()
            victim = min(cands, key=lambda s: (rank[s.kind], s.importance))
            secs.remove(victim)
            dropped += 1
        warnings.append(f"강의 시간이 짧아 섹션 {dropped}개를 제외했습니다.")
        return secs

    def _scope_notes(self, s: LectureSection, a: SourceAnalysis) -> None:
        if s.kind == SectionKind.intro:
            return
        ids = set(s.source_section_ids)
        for n in a.scope_notes:
            if n.source_location.section_id in ids:
                s.teaching_notes.append(f"자료의 범위 제한: {n.text}")

    def _objectives(self, sections, by_name, pol, D) -> list[str]:
        concept_secs = [s for s in sections if s.kind == SectionKind.concept]
        want = max(3, min(6, round(D / 15) + 1))
        if len(concept_secs) > want:
            step = len(concept_secs) / want
            concept_secs = [concept_secs[int(i * step)] for i in range(want)]
        out = []
        for s in concept_secs:
            top = sorted(s.concepts, key=lambda n: -(by_name[n].importance if n in by_name else 0))[:2]
            out.append(_STYLE_OBJECTIVE[pol.objective_style].format(n=_join(top, 2)))
        return out or ["강의자료의 핵심 내용을 이해한다."]

    def _metrics(self, sections: list[LectureSection]) -> PlanMetrics:
        by_kind: dict[str, int] = {}
        for s in sections:
            by_kind[s.kind.value] = by_kind.get(s.kind.value, 0) + s.duration_minutes
        teach = (SectionKind.concept, SectionKind.prerequisite)
        concepts = {n for s in sections if s.kind in teach for n in s.concepts}
        explain = sum(
            s.duration_minutes
            for s in sections
            if s.kind in (SectionKind.concept, SectionKind.prerequisite, SectionKind.comparison, SectionKind.caution)
        )
        return PlanMetrics(
            section_count=len(sections),
            slide_count=sum(s.estimated_slides for s in sections),
            concept_count=len(concepts),
            definition_count=sum(len(s.terms) for s in sections),
            example_count=sum(len(s.examples) for s in sections),
            practice_activity_count=sum(len(s.practice) for s in sections),
            practice_step_count=sum(len(x.steps) for s in sections for x in s.practice),
            code_count=sum(len(s.code) for s in sections),
            quiz_question_count=sum(s.quiz.question_count for s in sections if s.quiz),
            explanation_minutes=explain,
            practice_minutes=by_kind.get("practice", 0),
            minutes_by_kind=by_kind,
        )
