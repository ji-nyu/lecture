"""SlidePlanner (STAGE 4): LecturePlan -> SlideSpecification.

Rule based and deterministic (no LLM). The planner - not a presentation provider -
decides the complete slide structure, and gives every slide a reason to exist.

Input
  LecturePlan                 the structure, time and content decided in STAGE 3
  SourceAnalysis (optional)   only to look up verbatim source sentences, concept
                              categories and source locations. Without it the slides
                              are still valid but carry names instead of sentences
                              (a warning says so).

Algorithm (per section)
  1. Build the slide "seeds" the section calls for (definitions, concept slides,
     diagram, examples, code, practice steps, quiz, comparison table...).
  2. Fit them to the section's `estimated_slides`, so the specification has exactly the
     slide count the professor saw in the plan preview. Too many: fold or drop the
     least valuable seed. Too few: split a slide that has several points.
  3. Split the section's seconds over its slides by weight (integer seconds, the
     total is exactly duration_minutes * 60).
  4. Write visual / presenter instructions from the OPTIONS that shape slides:
     visual_level, slide_density, speaker_notes, lecture_tone, source_policy, audience,
     difficulty, code mode.

Grounding: `key_message` / `key_points` only ever contain verbatim source text (clipped,
with an ellipsis) or neutral structure ("‘X’의 역할을 설명한다"). A slot the source cannot
fill is marked `content_origin = suggested` and carries no text.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from ..errors import AppError, SlidePlanningError
from ..models.lecture_plan import LecturePlan, LectureSection, Origin, SectionKind
from ..models.slide_spec import (
    ContentOrigin,
    Slide,
    SlideMetrics,
    SlideSpecification,
    SlideStyle,
    SlideType,
)
from ..models.source import SourceAnalysis, SourceLocation
from ..storage.project_store import utcnow
from .planning_policy import BEGINNER_AUDIENCES, EXPERT_AUDIENCES

logger = logging.getLogger("ailecturegen")

MAX_POINTS = {"concise": 3, "normal": 5, "detailed": 7}
MIN_SECONDS_PER_SLIDE = 15
MESSAGE_CHARS = 150
POINT_CHARS = {"concise": 60, "normal": 80, "detailed": 110}

# visual slide types
_VISUAL_TYPES = {SlideType.diagram, SlideType.architecture, SlideType.workflow, SlideType.comparison}

# Priorities used when a section's slide budget is smaller than what it calls for.
PRIO = {
    "title": 10, "concept": 10, "practice": 9, "objectives": 8, "definition": 8, "summary": 8,
    "comparison": 8, "quiz": 6, "example": 6, "code": 6, "caution": 7, "agenda": 5, "check": 5,
    "diagram": 3, "facet": 2,
}


# ---------------------------------------------------------------------------
@dataclass
class Seed:
    role: str
    slide_type: SlideType
    title: str
    purpose: str
    message: str
    origin: ContentOrigin = ContentOrigin.structural
    points: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    ref: SourceLocation | None = None
    weight: float = 90.0
    foldable: bool = False  # may be folded into a neighbour when the budget is short
    silent: bool = False  # dropping it needs no warning
    capped: bool = True  # key_points limited by slide_density
    extra: dict = field(default_factory=dict)
    from_source: bool = False  # `message` is a verbatim source sentence

    @property
    def prio(self) -> int:
        return PRIO.get(self.role, 5)


@dataclass
class CInfo:
    name: str
    description: str | None
    category: str | None
    importance: int
    prerequisites: list[str]
    loc: SourceLocation | None


class Ctx:
    def __init__(self, plan: LecturePlan, analysis: SourceAnalysis | None):
        self.plan = plan
        self.analysis = analysis
        h = plan.presentation_hints
        self.style = SlideStyle(
            slide_density=h.slide_density,
            visual_level=h.visual_level,
            lecture_tone=h.lecture_tone,
            speaker_notes=h.speaker_notes,
            source_policy=h.source_policy,
            audience=plan.audience,
            difficulty=plan.difficulty,
            max_key_points=MAX_POINTS.get(h.slide_density, 5),
        )
        self._concepts = {c.name: c for c in analysis.concepts} if analysis else {}
        self.beginner = plan.audience in BEGINNER_AUDIENCES
        self.expert = plan.audience in EXPERT_AUDIENCES
        self.style_key = (
            "exam" if plan.lecture_type == "exam_preparation"
            else "perform" if plan.lecture_type == "practice"
            else "analyze" if (self.expert or plan.difficulty == "advanced")
            else "understand" if (self.beginner or plan.difficulty == "introductory")
            else "apply"
        )

    def info(self, name: str) -> CInfo:
        c = self._concepts.get(name)
        if c is None:
            return CInfo(name, None, None, 3, [], None)
        return CInfo(name, c.description, c.category.value, c.importance, list(c.prerequisite), c.source_location)

    @property
    def visual(self) -> str:
        return self.style.visual_level

    @property
    def char_limit(self) -> int:
        return POINT_CHARS.get(self.style.slide_density, 80)


def clip(text: str, n: int) -> str:
    """A verbatim prefix of `text` (whitespace normalised), ending in an ellipsis if cut."""
    t = " ".join(text.split())
    if len(t) <= n:
        return t
    cut = t[:n]
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:·") + "…"


def _names(names: list[str], n: int = 3) -> str:
    return " · ".join(names[:n])


def allocate_seconds(weights: list[float], total: int) -> list[int]:
    """Integer seconds per slide, proportional to `weights`, summing to exactly `total`."""
    n = len(weights)
    if n == 0 or total < n:
        raise SlidePlanningError("슬라이드에 시간을 배분할 수 없습니다. 강의 시간이나 옵션을 조정해 주세요.")
    floor = max(1, min(MIN_SECONDS_PER_SLIDE, total // n))
    remain = total - floor * n
    s = sum(weights) or 1.0
    raw = [remain * w / s for w in weights]
    base = [int(r) for r in raw]
    rest = remain - sum(base)
    order = sorted(range(n), key=lambda i: (raw[i] - base[i]), reverse=True)
    for i in order[:rest]:
        base[i] += 1
    return [floor + b for b in base]


# ---------------------------------------------------------------------------
class SlidePlanner:
    name = "rule-v1"

    def plan(self, plan: LecturePlan, analysis: SourceAnalysis | None = None) -> SlideSpecification:
        try:
            return self._plan(plan, analysis)
        except AppError:
            raise
        except ValueError as exc:  # pydantic invariants of the specification
            logger.warning("Invalid slide specification: %s", exc)
            raise SlidePlanningError() from exc
        except Exception as exc:  # never leak internals
            logger.exception("Slide planning failed", exc_info=exc)
            raise SlidePlanningError() from exc

    # ------------------------------------------------------------------
    def _plan(self, plan: LecturePlan, analysis: SourceAnalysis | None) -> SlideSpecification:
        ctx = Ctx(plan, analysis)
        warnings: list[str] = []
        if analysis is None:
            warnings.append("원문 분석 정보 없이 슬라이드를 계획해 원문 문장 대신 개념 이름만 담았습니다.")

        slides: list[Slide] = []
        for sec in plan.sections:
            seeds = self._section_seeds(ctx, sec)
            seeds = self._fit(seeds, sec, warnings)
            secs = allocate_seconds([s.weight for s in seeds], sec.duration_minutes * 60)
            for seed, seconds in zip(seeds, secs):
                slides.append(self._slide(ctx, sec, seed, seconds, len(slides) + 1))

        if len(slides) != plan.estimated_slide_count:
            warnings.append(
                f"슬라이드 수가 강의 계획의 예상({plan.estimated_slide_count}장)과 다릅니다({len(slides)}장)."
            )
        warnings.extend(w for w in plan.warnings if w not in warnings and "슬라이드" in w)
        return SlideSpecification(
            lecture_id=plan.id,
            project_id=plan.project_id,
            title=plan.title,
            duration_minutes=plan.duration_minutes,
            slide_count=len(slides),
            plan_estimated_slide_count=plan.estimated_slide_count,
            slides=slides,
            metrics=self._metrics(slides),
            style=ctx.style,
            warnings=warnings,
            planner=self.name,
            generated_at=utcnow(),
        )

    # ============================================================== seeds
    def _section_seeds(self, ctx: Ctx, sec: LectureSection) -> list[Seed]:
        k = sec.kind
        if k == SectionKind.intro:
            return self._intro(ctx, sec)
        if k in (SectionKind.concept, SectionKind.prerequisite):
            return self._concepts(ctx, sec)
        if k == SectionKind.practice:
            return self._practice(ctx, sec)
        if k == SectionKind.comparison:
            return self._comparison(ctx, sec)
        if k == SectionKind.caution:
            return self._caution(ctx, sec)
        if k == SectionKind.quiz:
            return self._final_quiz(ctx, sec)
        return self._summary(ctx, sec)

    # ------------------------------------------------------------- intro
    def _intro(self, ctx: Ctx, sec: LectureSection) -> list[Seed]:
        plan = ctx.plan
        rest = [s for s in plan.sections if s.id != sec.id]
        agenda_pts = [f"{i}. {s.title} ({s.duration_minutes}분)" for i, s in enumerate(rest, 1)][:12]
        title = Seed(
            "title", SlideType.title, plan.title,
            "강의 주제와 대상, 진행 방식을 소개하고 학습 동기를 만든다.",
            f"{plan.duration_minutes}분 · {_AUDIENCE_LABEL.get(plan.audience, plan.audience)} · "
            f"{_TYPE_LABEL.get(plan.lecture_type, plan.lecture_type)}",
            weight=30, capped=False,
            extra={"constraints": [c.text for c in plan.source_constraints[:2]]},
        )
        objectives = Seed(
            "objectives", SlideType.agenda, "학습 목표",
            "이 강의를 마치면 무엇을 할 수 있는지 안내해 학습 방향을 잡는다.",
            "이 강의가 끝나면 아래 목표를 달성한다.",
            points=list(plan.learning_objectives)[:6], weight=60, capped=False,
        )
        agenda = Seed(
            "agenda", SlideType.agenda, "강의 구성과 시간 배분",
            "전체 흐름과 섹션별 시간 배분을 안내해 학습자가 진행 상황을 예측하게 한다.",
            "강의는 다음 순서로 진행한다.",
            points=agenda_pts, weight=45, capped=False, silent=True,
        )
        return [title, objectives, agenda]

    # --------------------------------------------------- concept / prerequisite
    def _concepts(self, ctx: Ctx, sec: LectureSection) -> list[Seed]:
        n_slides = sec.estimated_slides
        names = list(sec.concepts) or [sec.title]
        infos = [ctx.info(n) for n in names]
        pos = {n: i for i, n in enumerate(names)}
        terms = list(sec.terms)
        def_chunks = [terms[i : i + 2] for i in range(0, len(terms), 2)]
        fixed = len(def_chunks) + len(sec.examples) + len(sec.code) + (1 if sec.quiz else 0)
        k = max(1, n_slides - fixed)

        visual_cats = [i for i in infos if i.category in ("process", "architecture")]
        diagram = 1 if (ctx.visual == "high" and k >= 2 and visual_cats) else 0
        kc = k - diagram

        # ---- definitions first
        seeds: list[Seed] = []
        used_defs: set[str] = set()
        for ch in def_chunks:
            seeds.append(self._definition(ctx, sec, ch))
            used_defs.update(t.text for t in ch if t.text)

        # ---- concept groups (+ facets of a concept when the plan wants finer granularity)
        if kc <= len(names):
            size, extra = divmod(len(names), kc)
            groups, at = [], 0
            for g in range(kc):
                take = size + (1 if g < extra else 0)
                groups.append(names[at : at + take])
                at += take
            facets = {}
        else:
            groups = [[n] for n in names]
            facets = {}
            ranked = sorted(infos, key=lambda i: (-i.importance, pos[i.name]))
            for j in range(kc - len(names)):
                nm = ranked[j % len(ranked)].name
                facets[nm] = facets.get(nm, 0) + 1

        points_left = list(sec.source_points)
        examples = list(sec.examples)
        codes = list(sec.code)
        placed_ex: set[int] = set()
        placed_code: set[int] = set()

        def take_examples(group: list[str], first: bool) -> list[Seed]:
            out = []
            for i, e in enumerate(examples):
                if i in placed_ex or bool(e.example_first) != first:
                    continue
                if any(c in group for c in e.concepts) or (not e.concepts and group is groups[0] and first is False):
                    placed_ex.add(i)
                    out.append(self._example(ctx, e))
            return out

        for group in groups:
            seeds.extend(take_examples(group, True))
            seeds.append(self._concept_slide(ctx, sec, group, used_defs, points_left))
            for nm in group:
                for j in range(facets.get(nm, 0)):
                    seeds.append(self._facet(ctx, sec, nm, j))
            seeds.extend(take_examples(group, False))
            for i, c in enumerate(codes):
                if i not in placed_code and any(x in group for x in c.concepts):
                    placed_code.add(i)
                    seeds.append(self._code(ctx, c))
        # anything whose concept was not in a group is taught after the last group
        for i, e in enumerate(examples):
            if i not in placed_ex:
                seeds.append(self._example(ctx, e))
        for i, c in enumerate(codes):
            if i not in placed_code:
                seeds.append(self._code(ctx, c))

        if diagram:
            seeds.append(self._diagram(ctx, sec, visual_cats))
        if sec.quiz:
            q = sec.quiz
            seeds.append(
                Seed(
                    "quiz", SlideType.quiz, "중간 확인 퀴즈",
                    f"방금 배운 ‘{_names(q.targets)}’ 개념의 이해도를 바로 점검한다.",
                    f"{q.question_count}문항으로 방금 다룬 내용을 확인한다.",
                    points=list(q.targets), concepts=list(q.targets), weight=60,
                    extra={"quiz": q.kind},
                )
            )
        return seeds

    def _definition(self, ctx: Ctx, sec: LectureSection, chunk) -> Seed:
        first = chunk[0]
        names = [t.term for t in chunk]
        purpose = (
            f"‘{names[0]}’의 의미를 정확히 이해해 이후 설명의 기준으로 삼는다."
            if len(chunk) == 1
            else f"‘{_names(names)}’의 의미를 각각 정확히 구분해 이해한다."
        )
        verbatim = False
        if len(chunk) == 1:
            if first.text:
                msg, origin = clip(first.text, MESSAGE_CHARS), ContentOrigin.source
                verbatim = True
                points = [clip(first.text, ctx.char_limit)]
            else:
                msg, origin = f"‘{first.term}’의 정의는 자료에 없어 강사가 보충한다.", ContentOrigin.suggested
                points = [first.term]
            extra = [p for p in sec.source_points if p and p not in points]
            for p in extra:
                line = clip(p, ctx.char_limit)
                if line and line not in points:
                    points.append(line)
                if len(points) >= ctx.style.max_key_points:
                    break
        else:
            msg, origin = f"‘{_names(names)}’의 정의를 나란히 확인한다.", ContentOrigin.source
            points = [f"{t.term}: {clip(t.text, ctx.char_limit)}" if t.text else t.term for t in chunk]
        return Seed(
            "definition", SlideType.definition,
            f"용어 정의: {_names(names)}", purpose, msg, origin, points, names,
            ref=first.source_reference, weight=45 * len(chunk) + 15, foldable=True, from_source=verbatim,
        )

    def _concept_slide(self, ctx: Ctx, sec, group: list[str], used_defs: set[str], points_left: list[str]) -> Seed:
        infos = [ctx.info(n) for n in group]
        main = max(infos, key=lambda i: (i.importance, -group.index(i.name)))
        # message: the main concept's own source sentence, unless a definition slide already shows it
        msg, origin, ref, verbatim = None, ContentOrigin.structural, None, False
        if main.description and main.description not in used_defs:
            msg, origin, ref = clip(main.description, MESSAGE_CHARS), ContentOrigin.source, main.loc
            verbatim = True
            used_defs.add(main.description)
        else:  # a stressed source sentence, but only one that is actually about this concept
            about = next((p for p in points_left if main.name.lower() in p.lower()), None)
            if about is not None:
                points_left.remove(about)
                msg, origin, ref = clip(about, MESSAGE_CHARS), ContentOrigin.source, main.loc
                verbatim = True
        if msg is None:
            msg = f"‘{main.name}’의 역할과 동작 방식을 설명한다."
            ref = main.loc
        style = ctx.style.slide_density
        points: list[str] = []
        for i in infos:
            desc = clip(i.description, ctx.char_limit) if i.description else None
            if desc:
                points.append(desc if (style == "concise" or i is main) else f"{i.name}: {desc}")
            # a bare concept name becomes an empty labelled box in the renderer
        if style != "concise" and len(group) == 1:
            if main.prerequisites:
                points.append(f"선행 개념: {_names(main.prerequisites)}")
            later = [
                c for c in sec.concepts
                if c != main.name and main.name in ctx.info(c).prerequisites
            ][:3]
            if later:
                points.append(f"연관 개념: {_names(later)}")
        if style == "detailed":
            about_all = [p for p in points_left if any(n.lower() in p.lower() for n in group)]
            points.extend(clip(p, ctx.char_limit) for p in about_all[:2])
        if len(points) < ctx.style.max_key_points:
            leftover = [p for p in points_left if any(n.lower() in p.lower() for n in group)]
            leftover += [p for p in points_left if p not in leftover]
            for p in leftover:
                if len(points) >= ctx.style.max_key_points:
                    break
                if p in points_left:
                    points_left.remove(p)
                line = clip(p, ctx.char_limit)
                if line and line not in points:
                    points.append(line)
        if not points:
            points = [i.name for i in infos]
        stype = SlideType.concept
        if ctx.visual != "low":
            if main.category == "process":
                stype = SlideType.workflow
            elif main.category == "architecture":
                stype = SlideType.architecture
        if main.category == "formula":
            stype = SlideType.formula
        pkey = _CONCEPT_PURPOSE[ctx.style_key].format(n=_names(group))
        return Seed(
            "concept", stype, _names(group), pkey, msg, origin, points, list(group),
            ref=ref, weight=150 + 15 * (len(group) - 1), from_source=verbatim,
        )

    def _facet(self, ctx: Ctx, sec, name: str, j: int) -> Seed:
        """Extra slides when the plan wants a finer split of a concept: first how it connects
        to the concepts before it (only if it has any), then understanding checks."""
        info = ctx.info(name)
        if j == 0 and info.prerequisites:
            return Seed(
                "facet", SlideType.concept, f"{name}: 다른 개념과의 연결",
                f"‘{name}’의 앞선 개념과의 연결을 이해한다.",
                f"‘{name}’의 앞선 개념과의 관계를 정리한다.",
                points=[f"선행 개념: {_names(info.prerequisites)}"], concepts=[name],
                ref=info.loc, weight=90, silent=True,
            )
        return Seed(
            "facet", SlideType.concept, f"{name}: 이해 확인",
            f"‘{name}’ 개념을 자신의 말로 설명해 보며 이해를 스스로 점검한다.",
            f"‘{name}’ 개념을 한 문장으로 설명해 본다.", points=[name], concepts=[name],
            weight=60, silent=True,
        )

    def _diagram(self, ctx: Ctx, sec, cats: list[CInfo]) -> Seed:
        names = [c.name for c in cats][:6]
        arch = any(c.category == "architecture" for c in cats)
        return Seed(
            "diagram", SlideType.architecture if arch else SlideType.workflow,
            f"{_names(names)} {'구조도' if arch else '흐름도'}",
            f"‘{_names(names)}’의 구성 요소와 관계를 한눈에 파악한다.",
            "구성 요소가 서로 어떻게 연결되는지 도식으로 정리한다.",
            points=names, concepts=names, weight=100, silent=True,
        )

    def _example(self, ctx: Ctx, e) -> Seed:
        c = e.concepts[0] if e.concepts else "핵심 개념"
        heading = None
        text = e.text
        if text and e.title.startswith("사례") and ": " in text:
            # a case study's text is "<heading>: <body>" (the analyzer joins them); the body is the
            # verbatim source sentence, the heading becomes a key point
            heading, text = text.split(": ", 1)
        if text:
            msg, origin = clip(text, MESSAGE_CHARS), ContentOrigin.source
        else:
            msg = f"자료에 예시가 없어 ‘{c}’의 일반적인 사례로 보충한다."
            origin = ContentOrigin.suggested
        pts = [heading] if heading else []
        if text:
            pts.append(clip(text, ctx.char_limit))
        elif msg and origin != ContentOrigin.suggested:
            pts.append(msg)
        return Seed(
            "example", SlideType.example, e.title,
            f"‘{c}’의 실제 사용 사례를 확인해 개념을 구체적으로 이해한다.",
            msg, origin, points=pts, concepts=list(e.concepts),
            ref=e.source_reference, weight=90, from_source=bool(text),
            extra={"example_first": e.example_first},
        )

    def _code(self, ctx: Ctx, c) -> Seed:
        name = c.concepts[0] if c.concepts else "핵심 개념"
        pts = []
        if c.language:
            pts.append(f"언어: {c.language}")
        pts.append("실행해 결과를 확인한다" if c.mode == "executable" else "핵심 줄을 읽으며 설명한다")
        return Seed(
            "code", SlideType.code, f"코드 예제: {name}",
            f"자료의 코드를 읽고 ‘{name}’의 동작을 확인한다.",
            "자료에 실린 코드를 그대로 사용해 동작을 설명한다.",
            ContentOrigin.source, points=pts, concepts=list(c.concepts), ref=c.source_reference,
            weight=120 if c.mode == "executable" else 90, extra={"mode": c.mode, "language": c.language},
        )

    # ---------------------------------------------------------------- practice
    def _practice(self, ctx: Ctx, sec: LectureSection) -> list[Seed]:
        seeds = []
        for a in sec.practice:
            grounded = a.basis in ("source_example", "source_code")
            seeds.append(
                Seed(
                    "practice", SlideType.practice, a.title, a.objective,
                    "지시에 따라 단계를 직접 수행하고 결과를 확인한다.",
                    ContentOrigin.source if grounded else ContentOrigin.structural,
                    points=list(a.steps), concepts=list(a.concepts), ref=a.source_reference,
                    weight=120 + 60 * len(a.steps), capped=False,
                    extra={"mode": a.mode, "steps": len(a.steps)},
                )
            )
        if sec.estimated_slides > len(sec.practice):
            seeds.append(
                Seed(
                    "check", SlideType.practice, "실습 결과 확인과 정리",
                    "실습 결과를 자료의 설명과 비교하고 배운 점을 정리한다.",
                    "각 단계의 결과를 확인하고 예상과 다른 점을 함께 분석한다.",
                    points=["결과를 자료의 설명과 비교한다", "예상과 다른 결과의 원인을 분석한다", "배운 점을 정리한다"],
                    concepts=list(sec.concepts), weight=150, capped=False, silent=True,
                    extra={"steps": 0},
                )
            )
        return seeds

    # -------------------------------------------------------------- comparison
    def _comparison(self, ctx: Ctx, sec: LectureSection) -> list[Seed]:
        n = sec.estimated_slides
        names = list(sec.concepts)
        tables = n if n < 3 else n - 1
        tables = max(1, min(tables, len(names)))
        size, extra = divmod(len(names), tables)
        seeds, at = [], 0
        for g in range(tables):
            take = size + (1 if g < extra else 0)
            grp = names[at : at + take]
            at += take
            pts = []
            for nm in grp:
                i = ctx.info(nm)
                if ctx.style.slide_density != "concise" and i.description:
                    pts.append(f"{nm}: {clip(i.description, ctx.char_limit)}")
                else:
                    pts.append(nm)
            suffix = f" ({g + 1}/{tables})" if tables > 1 else ""
            seeds.append(
                Seed(
                    "comparison", SlideType.comparison, f"핵심 개념 비교: {_names(grp)}{suffix}",
                    f"‘{_names(grp)}’의 차이를 기준별로 구분해 혼동을 줄인다.",
                    f"‘{_names(grp)}’ 개념을 같은 기준으로 비교해 차이를 구분한다.",
                    ContentOrigin.source, pts, grp, weight=150,
                )
            )
        if n >= 3:
            seeds.append(
                Seed(
                    "diagram", SlideType.diagram, "개념 관계도",
                    "비교한 개념들이 서로 어떻게 연결되는지 한눈에 파악한다.",
                    "개념 사이의 관계를 하나의 그림으로 정리한다.",
                    points=names[:6], concepts=names[:6], weight=100, silent=True,
                )
            )
        return seeds

    # ----------------------------------------------------------------- caution
    def _caution(self, ctx: Ctx, sec: LectureSection) -> list[Seed]:
        pts = list(sec.source_points) + [
            f"{c}: {ctx.info(c).description}" if ctx.info(c).description else c for c in sec.concepts
        ]
        n = sec.estimated_slides
        purpose = f"‘{sec.title}’ 내용을 알고 흔한 오류와 설계상의 한계를 예방한다."
        if not pts:
            return [
                Seed(
                    "caution", SlideType.concept, sec.title,
                    "자료가 다루지 않은 한계와 흔한 오류를 강사가 일반적인 내용으로 보충한다.",
                    "이 주제는 자료에 없어 일반적인 내용으로 보충한다.", ContentOrigin.suggested,
                    weight=100,
                )
            ]
        n = max(1, min(n, len(pts)))
        seeds = []
        for i in range(n):
            chunk = pts[i::n]
            suffix = f" ({i + 1}/{n})" if n > 1 else ""
            seeds.append(
                Seed(
                    "caution", SlideType.concept, f"{sec.title}{suffix}", purpose,
                    clip(chunk[0], MESSAGE_CHARS), ContentOrigin.source,
                    [clip(p, ctx.char_limit + 20) for p in chunk], list(sec.concepts)[:3], weight=90 * len(chunk),
                    from_source=chunk[0] in sec.source_points,
                )
            )
        return seeds

    # ------------------------------------------------------------- final quiz
    def _final_quiz(self, ctx: Ctx, sec: LectureSection) -> list[Seed]:
        q = sec.quiz
        total = q.question_count if q else 3
        targets = list(q.targets) if q else list(sec.concepts)
        n = max(1, sec.estimated_slides)
        base, extra = divmod(total, n)
        seeds = []
        for i in range(n):
            cnt = max(1, base + (1 if i < extra else 0))
            grp = targets[i::n] or targets
            suffix = f" ({i + 1}/{n})" if n > 1 else ""
            seeds.append(
                Seed(
                    "quiz", SlideType.quiz, f"{sec.title}{suffix}",
                    f"‘{_names(grp)}’ 등 핵심 개념의 이해도를 점검한다.",
                    f"{cnt}문항으로 핵심 개념 이해도를 확인한다.",
                    points=list(grp), concepts=list(grp), weight=70 * cnt, extra={"quiz": "final"},
                )
            )
        return seeds

    # ----------------------------------------------------------------- summary
    def _summary(self, ctx: Ctx, sec: LectureSection) -> list[Seed]:
        exam = ctx.style_key == "exam"
        pts = []
        for nm in sec.concepts:
            i = ctx.info(nm)
            if ctx.style.slide_density == "detailed" and i.description:
                pts.append(f"{nm}: {clip(i.description, ctx.char_limit)}")
            else:
                pts.append(nm)
        seeds = [
            Seed(
                "summary", SlideType.summary, sec.title,
                "시험에 나오는 핵심 개념을 다시 확인하고 혼동하기 쉬운 부분을 정리한다."
                if exam
                else "강의의 핵심 개념을 다시 연결해 전체 그림을 정리한다.",
                "오늘 배운 핵심 개념을 한 흐름으로 다시 정리한다.",
                points=pts, concepts=list(sec.concepts), weight=90,
            )
        ]
        if sec.estimated_slides >= 2:
            seeds.append(
                Seed(
                    "summary", SlideType.summary, "학습 목표 점검",
                    "처음에 제시한 학습 목표를 달성했는지 스스로 점검하고 다음 학습으로 연결한다.",
                    "강의를 시작할 때의 학습 목표를 다시 확인한다.",
                    points=list(ctx.plan.learning_objectives)[:6], weight=60, capped=False,
                )
            )
        return seeds

    # ============================================================ fitting
    def _fit(self, seeds: list[Seed], sec: LectureSection, warnings: list[str]) -> list[Seed]:
        n = max(1, sec.estimated_slides)
        # ---- too many
        while len(seeds) > n:
            idx = min(range(len(seeds)), key=lambda i: (seeds[i].prio, -i))
            victim = seeds.pop(idx)
            if victim.role == "agenda" and any(s.role == "objectives" for s in seeds):
                obj = next(s for s in seeds if s.role == "objectives")
                obj.points.append("강의 구성: " + " → ".join(p.split(". ", 1)[-1] for p in victim.points))
                obj.title = "학습 목표와 강의 구성"
            elif victim.foldable and seeds:
                tgt = seeds[max(0, idx - 1)]
                for p in victim.points:
                    if p not in tgt.points:
                        tgt.points.append(p)
                for c in victim.concepts:
                    if c not in tgt.concepts:
                        tgt.concepts.append(c)
            elif not victim.silent:
                warnings.append(
                    f"‘{sec.title}’: 시간이 짧아 ‘{victim.title}’ 슬라이드를 제외했습니다."
                )
        # ---- too few: split a slide that lists several points
        while len(seeds) < n:
            cands = [
                (i, s) for i, s in enumerate(seeds)
                if len(s.points) >= 2 and s.role not in ("title", "example", "code")
            ]
            if not cands:
                break
            i, s = max(cands, key=lambda t: (len(t[1].points), t[1].prio))
            half = math.ceil(len(s.points) / 2)
            second = Seed(
                s.role, s.slide_type, s.title, s.purpose,
                f"앞 슬라이드에 이어 ‘{_names(s.concepts) or s.title}’ 관련 내용을 설명한다.",
                s.origin, s.points[half:], list(s.concepts), s.ref, s.weight, s.foldable, s.silent,
                s.capped, dict(s.extra),
            )
            s.points = s.points[:half]
            s.weight = s.weight * 0.6
            second.weight = s.weight
            s.title = f"{s.title} (1/2)" if "(1/2)" not in s.title else s.title
            second.title = s.title.replace("(1/2)", "(2/2)")
            seeds.insert(i + 1, second)
        if len(seeds) < n:
            warnings.append(f"‘{sec.title}’: 슬라이드를 계획({n}장)만큼 나눌 내용이 부족합니다({len(seeds)}장).")
        return seeds

    # ============================================================ slide
    def _slide(self, ctx: Ctx, sec: LectureSection, seed: Seed, seconds: int, number: int) -> Slide:
        points = list(seed.points)
        if seed.capped:
            points = points[: ctx.style.max_key_points]
        return Slide(
            slide_number=number,
            section_id=sec.id,
            section_title=sec.title,
            title=seed.title,
            slide_type=seed.slide_type,
            learning_purpose=seed.purpose,
            key_message=seed.message,
            message_from_source=seed.from_source,
            key_points=points,
            source_reference=seed.ref,
            visual_instruction=self._visual(ctx, seed),
            presenter_instruction=self._presenter(ctx, sec, seed, seconds),
            estimated_explanation_time=seconds,
            concepts=list(seed.concepts),
            content_origin=seed.origin,
        )

    # ---------------------------------------------------------- visual text
    def _visual(self, ctx: Ctx, s: Seed) -> str:
        lv = ctx.visual
        names = _names(s.concepts, 4) or s.title
        t = s.slide_type
        T = SlideType
        if t == T.title:
            base = "강의 제목과 부제(대상·시간)만 중앙에 크게 배치한다."
            extra = {"low": " 배경은 단색으로 두고 장식을 넣지 않는다.",
                     "medium": " 주제와 어울리는 간단한 배경 이미지를 사용한다.",
                     "high": " 강의 주제를 상징하는 대표 이미지를 전체 배경으로 사용한다."}
        elif t == T.agenda:
            base = "번호를 붙인 목록으로 항목을 순서대로 배치한다."
            extra = {"low": " 텍스트 목록만 사용한다.",
                     "medium": " 항목마다 아이콘을 하나씩 붙인다.",
                     "high": " 시간 배분이 보이는 가로 타임라인으로 시각화한다."}
        elif t == T.definition:
            base = "정의 문장을 강조 박스에 넣고 용어는 굵게 표시한다."
            extra = {"low": " 그림 없이 텍스트만 사용한다.",
                     "medium": " 용어를 나타내는 아이콘 1개를 곁들인다.",
                     "high": " 정의 문장을 화면의 중심에 두고, 옆에 용어 구성만 라벨이 붙은 작은 도식으로 보여 준다. 거대 장식 글자를 넣지 않는다."}
        elif t == T.comparison:
            base = f"‘{names}’ 개념을 행으로, 역할·특징·사용 시점을 열로 한 비교표로 만든다."
            extra = {"low": " 단색 표로 간결하게 만든다.",
                     "medium": " 차이가 큰 칸을 굵게 강조한다.",
                     "high": " 개념별 색상 코드를 정하고 차이가 큰 칸을 색으로 강조한다."}
        elif t in (T.architecture, T.diagram):
            base = f"‘{names}’ 요소를 박스로 그리고 화살표로 관계와 통신 방향을 표시한다."
            extra = {"low": " 최소한의 선과 텍스트만 사용한다.",
                     "medium": " 핵심 구성 요소만 색으로 구분한다.",
                     "high": " 본문 문장을 왼쪽에 두고, 오른쪽에 구성 요소 이름과 화살표가 적힌 도식을 둔다. 그림만으로 설명하지 않는다."}
        elif t == T.workflow:
            base = f"‘{names}’의 순서를 번호가 붙은 단계와 화살표로 나타낸다."
            extra = {"low": " 단계 이름만 나열한다.",
                     "medium": " 단계마다 짧은 설명을 붙인다.",
                     "high": " 단계 문장을 번호와 함께 두고, 옆에 화살표로 순서를 그린다. 단계 이름을 빼지 않는다."}
        elif t == T.example:
            base = "사례 문장을 인용 박스로 배치하고 관련 개념 이름을 옆에 표시한다."
            extra = {"low": " 텍스트만 사용한다.",
                     "medium": " 상황을 나타내는 작은 도식을 곁들인다.",
                     "high": " 상황·동작·결과를 문장으로 쓰고, 장면을 보여 주는 작은 일러스트를 곁들인다."}
        elif t == T.code:
            base = "자료의 코드를 고정폭 글꼴의 코드 블록으로 그대로 배치하고 핵심 줄을 강조한다. 코드를 고치거나 새로 쓰지 않는다."
            extra = {"low": " 코드 블록 외에는 아무것도 넣지 않는다.",
                     "medium": " 핵심 줄에 번호 표시를 붙인다.",
                     "high": " 코드 옆에 실행 흐름과 결과 화면을 함께 배치한다."}
            if s.extra.get("mode") == "executable":
                extra = {k: v + " 실행 결과 화면을 넣을 자리를 마련한다." for k, v in extra.items()}
        elif t == T.practice:
            base = "번호가 붙은 실습 단계를 체크리스트로 배치한다."
            extra = {"low": " 단계 텍스트만 사용한다.",
                     "medium": " 핵심 단계에 화면 캡처를 넣을 자리를 둔다.",
                     "high": " 모든 단계에 화면 캡처나 그림을 곁들인다."}
        elif t == T.quiz:
            base = "문제 카드 형식으로 질문과 선택 영역을 배치한다."
            extra = {"low": " 텍스트 카드만 사용한다.",
                     "medium": " 정답 공개 영역을 따로 둔다.",
                     "high": " 문항마다 아이콘과 진행 표시를 넣는다."}
        elif t == T.formula:
            base = "수식을 크게 배치하고 기호마다 의미를 표시한다."
            extra = {"low": " 수식과 기호 설명만 사용한다.",
                     "medium": " 수식 아래에 값 대입 예시를 둔다.",
                     "high": " 수식의 의미를 그래프나 그림으로 함께 보여 준다."}
        elif t == T.summary:
            base = "핵심 항목을 체크리스트로 정리한다."
            extra = {"low": " 텍스트 목록만 사용한다.",
                     "medium": " 항목마다 아이콘을 붙인다.",
                     "high": " 핵심 문장을 목록으로 두고, 옆에 개념 이름만 적은 작은 연결도를 둔다. 목록을 그림으로 대체하지 않는다."}
        else:  # concept
            base = (
                "핵심 메시지를 위쪽에 두고 본문 항목을 목록이나 카드로 배치한다. "
                "개념 이름만 적힌 빈 상자를 만들지 않는다."
            )
            extra = {"low": " 도표나 이미지 없이 텍스트만 사용한다.",
                     "medium": " 개념을 나타내는 아이콘이나 도식 1개를 곁들인다.",
                     "high": " 본문 문장을 먼저 배치하고, 옆에 라벨이 붙은 작은 도식을 둔다. 이미지가 본문 글을 대체하지 않는다."}
        return base + extra[lv]

    # ------------------------------------------------------- presenter text
    def _presenter(self, ctx: Ctx, sec: LectureSection, s: Seed, seconds: int) -> str:
        notes = ctx.style.speaker_notes
        role = s.role
        first = _ROLE_BASE.get(role, _ROLE_BASE["concept"])
        if role == "example":
            if s.origin == ContentOrigin.suggested:
                first = "자료에 예시가 없으므로 일반적인 사례를 강사가 보충하되 자료의 내용과 충돌하지 않게 한다."
            elif s.extra.get("example_first"):
                first = "이 사례를 먼저 보여 주고 어떤 개념이 쓰였는지 학습자가 짐작하게 한 뒤 다음 슬라이드에서 개념으로 정리한다."
        if role == "code":
            first = (
                "코드를 실제로 실행해 결과를 시연하고 학습자가 따라 하게 한다."
                if s.extra.get("mode") == "executable"
                else "코드를 위에서 아래로 읽으며 핵심 줄의 역할을 설명한다."
            )
        if role == "practice":
            first = (
                "각 단계를 시연한 뒤 학습자가 직접 수행하게 하고, 이 슬라이드 시간의 대부분은 수행 시간으로 쓴다."
                if s.extra.get("steps", 0) >= 4
                else "짧은 활동을 안내하고 학습자가 바로 적용해 보게 한다."
            )
        if role == "quiz" and s.extra.get("quiz") == "checkpoint":
            first = "정답을 바로 알려 주지 말고 학습자가 먼저 답하게 한 뒤 해설한다."

        tone = _TONE[ctx.style.lecture_tone]
        if notes == "none":  # no speaker notes: one short line (+ the tone to speak in)
            return f"{first} 어조: {_TONE_LABEL[ctx.style.lecture_tone]}."
        parts = [first, tone["line"]]
        if notes == "concise":
            return " ".join(parts)

        # full notes
        if role == "title" and s.extra.get("constraints"):
            parts.append("자료의 범위 제한을 먼저 밝힌다: " + " / ".join(clip(c, 80) for c in s.extra["constraints"]))
        if role in ("concept", "definition", "facet", "diagram", "comparison"):
            if ctx.beginner:
                parts.append("전문용어는 처음 나올 때 정의하고 쉬운 예시로 다시 풀어 설명한다.")
            elif ctx.expert:
                parts.append("기본 개념은 짧게 복습하고 내부 동작과 실무 적용을 중심으로 설명한다.")
            if ctx.plan.difficulty == "advanced":
                parts.append("설계상의 한계와 trade-off를 함께 언급한다.")
            elif ctx.plan.difficulty == "introductory":
                parts.append("수식과 세부 구현은 최소화하고 개념 중심으로 설명한다.")
        if role == "quiz":
            parts.append("틀린 답도 이유를 함께 짚어 오개념을 바로잡는다.")
        parts.append(tone["full"])
        parts.append(f"권장 시간은 약 {seconds}초이다.")
        if s.ref is not None:
            where = s.ref.section_title or ""
            loc = f"{s.ref.line}행" if s.ref.line else f"{s.ref.page}쪽" if s.ref.page else ""
            parts.append(f"근거: 자료 ‘{where}’ {loc}".rstrip() + ".")
        parts.append(_SOURCE_NOTE[ctx.style.source_policy])
        return " ".join(parts)

    # ============================================================ metrics
    def _metrics(self, slides: list[Slide]) -> SlideMetrics:
        by_type: dict[str, int] = {}
        by_sec: dict[str, int] = {}
        for s in slides:
            by_type[s.slide_type.value] = by_type.get(s.slide_type.value, 0) + 1
            by_sec[s.section_id] = by_sec.get(s.section_id, 0) + 1
        pts = [len(s.key_points) for s in slides]
        return SlideMetrics(
            slide_count=len(slides),
            total_seconds=sum(s.estimated_explanation_time for s in slides),
            slides_by_type=by_type,
            slides_by_section=by_sec,
            source_slide_count=sum(1 for s in slides if s.content_origin == ContentOrigin.source),
            suggested_slide_count=sum(1 for s in slides if s.content_origin == ContentOrigin.suggested),
            avg_key_points=round(sum(pts) / len(pts), 2),
            max_key_points=max(pts),
            visual_slide_count=sum(1 for s in slides if s.slide_type in _VISUAL_TYPES),
        )


# ---------------------------------------------------------------------------
_AUDIENCE_LABEL = {
    "general": "일반", "high_school": "고등학생", "university_beginner": "대학 초급",
    "university_intermediate": "대학 중급", "university_advanced": "대학 고급",
    "graduate": "대학원", "professional": "실무 전문가",
}
_TYPE_LABEL = {
    "theory": "이론 중심", "example_based": "예제 중심", "practice": "실습 중심",
    "mixed": "이론+실습 혼합", "exam_preparation": "시험 대비",
}
_CONCEPT_PURPOSE = {
    "understand": "‘{n}’의 정의와 의미를 쉬운 말로 이해한다.",
    "apply": "‘{n}’의 동작 방식을 이해하고 적용 방법을 익힌다.",
    "analyze": "‘{n}’의 내부 동작 원리와 설계상의 trade-off를 분석한다.",
    "perform": "‘{n}’ 실습에 필요한 핵심 지식만 간결하게 익힌다.",
    "exam": "‘{n}’의 핵심 내용을 정확히 구분해 정리한다.",
}
_ROLE_BASE = {
    "title": "강의 주제와 대상, 진행 시간을 소개하고 학습 동기를 만든다.",
    "objectives": "학습 목표를 하나씩 읽고 강의가 끝났을 때 할 수 있어야 할 일을 분명히 한다.",
    "agenda": "전체 흐름과 시간 배분을 짧게 안내한다.",
    "definition": "정의 문장을 그대로 읽은 뒤 핵심 단어를 짚어 설명한다.",
    "concept": "핵심 메시지를 먼저 말한 뒤 각 항목을 순서대로 설명한다.",
    "facet": "앞서 배운 개념과 연결해 짧게 설명한다.",
    "diagram": "도식의 구성 요소를 하나씩 짚으며 관계를 설명한다.",
    "example": "사례를 읽고 어떤 개념이 어떻게 쓰였는지 연결한다.",
    "code": "코드를 위에서 아래로 읽으며 핵심 줄의 역할을 설명한다.",
    "practice": "각 단계를 시연한 뒤 학습자가 직접 수행하게 한다.",
    "check": "결과를 함께 확인하고 예상과 다른 점을 짚는다.",
    "quiz": "학습자가 먼저 답하게 한 뒤 해설한다.",
    "comparison": "표의 행을 따라 차이를 짚고 혼동하기 쉬운 지점을 강조한다.",
    "caution": "자료가 강조한 주의점을 사례와 함께 설명한다.",
    "summary": "핵심을 다시 짚고 다음 학습으로 연결한다.",
}
_TONE_LABEL = {"academic": "학술적", "professional": "전문적", "conversational": "대화체"}
_TONE = {
    "academic": {
        "line": "학술적인 어조로 용어를 정확하게 사용해 설명한다.",
        "full": "근거와 정의를 분명히 밝히며 단정적으로 서술한다.",
    },
    "professional": {
        "line": "전문적이고 간결한 어조로 설명한다.",
        "full": "실무 상황과 연결해 핵심만 명확하게 전달한다.",
    },
    "conversational": {
        "line": "청중에게 질문을 던지며 대화하듯 편안한 어조로 설명한다.",
        "full": "‘여러분은 어떻게 생각하나요?’처럼 중간에 질문을 넣어 참여를 이끈다.",
    },
}
_SOURCE_NOTE = {
    "source_only": "자료에 없는 내용은 추가하지 않는다.",
    "source_first": "필요한 일반 설명은 보충할 수 있으나 자료와 충돌하는 내용은 말하지 않는다.",
    "expanded": "외부 지식과 사례를 활용할 수 있으나 자료와 충돌하지 않게 한다.",
}
