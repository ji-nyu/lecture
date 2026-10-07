"""STAGE 3: LecturePlanner. Same source, different options -> really different plans."""

import itertools
import json

import pytest
from pydantic import ValidationError

from app.errors import InvalidDurationDistribution, LecturePlanningError
from app.models.lecture_plan import LecturePlan, Origin, SectionKind
from app.services.evidence_validator import normalize
from app.services.hybrid_analyzer import HybridAnalyzer
from app.services.lecture_planner import LecturePlanner, allocate_minutes

from fake_llm import GOOD_EXTRACT, KOREAN_DOC, KOREAN_NARRATIVE, FakeLLM
from plan_helpers import (
    BASE, CASE_A, CASE_B, CASE_C, CODE_DOC, MQTT_DOCS, analyze_mqtt, analyze_text, comparable, parse, plan, profile,
)

needs_mqtt = pytest.mark.skipif(not MQTT_DOCS, reason="testdocument/*.txt not present")


@pytest.fixture()
def mqtt(tmp_path):
    return analyze_mqtt(tmp_path)  # (material, analysis)


def kinds(p):
    return [s.kind for s in p.sections]


def assert_valid(p: LecturePlan):
    """The spec's hard rules."""
    assert sum(s.duration_minutes for s in p.sections) == p.duration_minutes  # SuccessMetric_03
    assert all(s.estimated_slides >= 1 for s in p.sections)  # every section has a slide
    assert p.estimated_slide_count == sum(s.estimated_slides for s in p.sections)
    assert all(s.duration_minutes >= 1 for s in p.sections)
    assert [s.order for s in p.sections] == list(range(1, len(p.sections) + 1))
    assert len({s.id for s in p.sections}) == len(p.sections)
    assert p.sections[0].kind == SectionKind.intro and p.sections[-1].kind == SectionKind.summary
    assert p.learning_objectives and p.title


# ============================================================ spec Example Test
@needs_mqtt
def test_profile_a_vs_b_matches_the_spec_expectations(mqtt):
    _, a = mqtt
    pa, pb = plan(a, **CASE_A), plan(a, **CASE_B)
    assert_valid(pa), assert_valid(pb)
    ma, mb = pa.metrics, pb.metrics

    # Profile A (beginner, theory): concept explanation up, term definitions up, practice down
    assert ma.explanation_minutes > mb.explanation_minutes
    assert ma.definition_count > mb.definition_count
    assert ma.practice_activity_count == 0 and ma.practice_minutes == 0
    # Profile B (intermediate, practice): practice up, step-by-step up, explanation down
    assert mb.practice_minutes > ma.practice_minutes
    assert mb.practice_activity_count > 0 and mb.practice_step_count >= 12
    assert mb.explanation_minutes < ma.explanation_minutes
    assert mb.practice_minutes >= mb.explanation_minutes * 0.8  # practice gets real time

    # Completion criteria: section composition, time distribution, examples, practice, slide count
    assert kinds(pa) != kinds(pb)
    assert [s.duration_minutes for s in pa.sections] != [s.duration_minutes for s in pb.sections]
    assert ma.example_count != mb.example_count
    assert ma.slide_count != mb.slide_count
    assert ma.section_count != mb.section_count
    assert SectionKind.practice in kinds(pb) and SectionKind.practice not in kinds(pa)
    assert SectionKind.prerequisite in kinds(pa) or ma.definition_count >= mb.definition_count


@needs_mqtt
def test_three_spec_cases_are_not_nearly_identical(mqtt):
    _, a = mqtt
    plans = {n: plan(a, **c) for n, c in {"A": CASE_A, "B": CASE_B, "C": CASE_C}.items()}
    for p in plans.values():
        assert_valid(p)
    dims = {
        "sections": lambda p: p.metrics.section_count,
        "slides": lambda p: p.metrics.slide_count,
        "definitions": lambda p: p.metrics.definition_count,
        "examples": lambda p: p.metrics.example_count,
        "practice": lambda p: p.metrics.practice_activity_count,
        "explanation_min": lambda p: p.metrics.explanation_minutes,
        "kinds": lambda p: tuple(k.value for k in kinds(p)),
        "durations": lambda p: tuple(s.duration_minutes for s in p.sections),
    }
    for x, y in itertools.combinations(plans, 2):
        differing = [d for d, f in dims.items() if f(plans[x]) != f(plans[y])]
        assert len(differing) >= 6, (x, y, differing)
        assert comparable(plans[x]) != comparable(plans[y])
    c = plans["C"]  # professional, advanced, 30 min, concise
    assert c.metrics.definition_count == 0
    assert SectionKind.caution in kinds(c)  # Rule_Difficulty_02: limits / trade-offs
    assert c.duration_minutes == 30 and c.estimated_slide_count < plans["A"].estimated_slide_count


# ===================================== Rule_Options_01: every option has an effect
OPTION_CASES = {
    "audience_level": ({"audience_level": "university_beginner"}, {"audience_level": "professional"}),
    "duration_minutes": ({"duration_minutes": 30}, {"duration_minutes": 90}),
    "difficulty": ({"difficulty": "introductory"}, {"difficulty": "advanced"}),
    "lecture_type": ({"lecture_type": "theory"}, {"lecture_type": "practice"}),
    "explanation_depth": ({"explanation_depth": "concise"}, {"explanation_depth": "detailed"}),
    "slide_density": ({"slide_density": "concise"}, {"slide_density": "detailed"}),
    "visual_level": ({"visual_level": "low"}, {"visual_level": "high"}),
    "example_level": ({"example_level": "none"}, {"example_level": "high"}),
    "practice_level": ({"practice_level": "none"}, {"practice_level": "full"}),
    "quiz_mode": ({"quiz_mode": "none"}, {"quiz_mode": "both"}),
    "lecture_tone": ({"lecture_tone": "academic"}, {"lecture_tone": "conversational"}),
    "speaker_notes": ({"speaker_notes": "none"}, {"speaker_notes": "full"}),
}


@needs_mqtt
@pytest.mark.parametrize("option", list(OPTION_CASES))
def test_every_option_changes_the_plan(mqtt, option):
    _, a = mqtt
    lo, hi = OPTION_CASES[option]
    p1, p2 = plan(a, **lo), plan(a, **hi)
    assert_valid(p1), assert_valid(p2)
    assert comparable(p1) != comparable(p2), f"{option} only changed metadata"
    if option in ("lecture_tone", "speaker_notes"):
        # these shape the slides: carried to the SlidePlanner (STAGE 4) via presentation_hints
        assert p1.presentation_hints != p2.presentation_hints
        assert p1.metrics == p2.metrics
    else:
        # ...and it is a structural change, not just a hint
        s1 = (p1.metrics, [(s.kind, s.duration_minutes, s.estimated_slides) for s in p1.sections])
        s2 = (p2.metrics, [(s.kind, s.duration_minutes, s.estimated_slides) for s in p2.sections])
        assert s1 != s2


def test_code_level_changes_the_plan_and_uses_only_source_code(tmp_path):
    _, a = analyze_text(tmp_path, CODE_DOC)
    none = plan(a, code_level="none")
    snip = plan(a, code_level="snippet")
    execu = plan(a, lecture_type="practice", code_level="executable")
    assert none.metrics.code_count == 0
    assert snip.metrics.code_count == 1 and comparable(none) != comparable(snip)
    code = next(c for s in snip.sections for c in s.code)
    assert code.origin == Origin.source and code.language == "python" and code.mode == "snippet"
    # a code block is taught with the topic it sits in (section "Publish 실습"), not elsewhere
    holder = next(s for s in snip.sections if s.code)
    assert "Publish" in holder.concepts or "Topic" in holder.concepts
    assert code.source_reference.section_title == "Publish 실습"
    # executable code makes the practice activity a "run the code" one
    modes = {x.mode for s in execu.sections for x in s.practice}
    assert "code" in modes
    act = next(x for s in execu.sections for x in s.practice if x.mode == "code")
    assert act.basis == "source_code" and any("코드" in step for step in act.steps)
    assert all(x.mode == "exercise" for s in snip.sections for x in s.practice)  # snippets are only shown


@needs_mqtt
def test_source_without_code_omits_code_with_a_warning(mqtt):
    _, a = mqtt
    p = plan(a, code_level="snippet")
    assert p.metrics.code_count == 0 and any("코드" in w for w in p.warnings)


# ============================================================ lecture_type rules
@needs_mqtt
def test_lecture_types_shape_the_sections(mqtt):
    _, a = mqtt
    theory = plan(a, lecture_type="theory")
    practice = plan(a, lecture_type="practice")
    exam = plan(a, lecture_type="exam_preparation")
    assert SectionKind.practice not in kinds(theory)
    assert SectionKind.practice in kinds(practice)
    for k in (SectionKind.comparison, SectionKind.caution, SectionKind.quiz):  # Rule_LectureType_03
        assert k in kinds(exam)
        assert k not in kinds(theory)
    assert exam.metrics.quiz_question_count > theory.metrics.quiz_question_count
    assert exam.sections[-1].title.startswith("핵심 정리")
    # comparison table slide(s) for exam preparation
    cmp = next(s for s in exam.sections if s.kind == SectionKind.comparison)
    assert len(cmp.concepts) >= 2 and cmp.estimated_slides >= 2
    # practice: step-by-step activities with a real number of steps (Rule_LectureType_02)
    acts = [x for s in practice.sections for x in s.practice]
    assert acts and all(len(x.steps) >= 4 for x in acts)


@needs_mqtt
def test_practice_level_controls_steps_blocks_and_time(mqtt):
    _, a = mqtt
    res = {lv: plan(a, lecture_type="practice", practice_level=lv) for lv in ("simple", "guided", "full")}
    steps = [res[lv].metrics.practice_step_count for lv in ("simple", "guided", "full")]
    mins = [res[lv].metrics.practice_minutes for lv in ("simple", "guided", "full")]
    assert steps == sorted(steps) and len(set(steps)) == 3
    assert mins == sorted(mins) and len(set(mins)) == 3
    assert len({len(x.steps) for lv in res.values() for s in lv.sections for x in s.practice}) >= 3


@needs_mqtt
def test_quiz_modes(mqtt):
    _, a = mqtt
    none = plan(a, quiz_mode="none")
    cp = plan(a, quiz_mode="checkpoint")
    fin = plan(a, quiz_mode="final")
    both = plan(a, quiz_mode="both")
    assert none.metrics.quiz_question_count == 0
    assert any(s.quiz and s.quiz.kind == "checkpoint" for s in cp.sections)
    assert SectionKind.quiz not in kinds(cp)
    assert SectionKind.quiz in kinds(fin) and not any(s.quiz and s.quiz.kind == "checkpoint" for s in fin.sections)
    assert SectionKind.quiz in kinds(both) and any(s.quiz and s.quiz.kind == "checkpoint" for s in both.sections)
    assert both.metrics.quiz_question_count > cp.metrics.quiz_question_count


# =========================================================== audience / difficulty
@needs_mqtt
def test_beginner_vs_professional(mqtt):
    _, a = mqtt
    beg = plan(a, audience_level="university_beginner", lecture_type="theory")
    pro = plan(a, audience_level="professional", lecture_type="theory")
    # Rule_Audience_01: every taught term defined, examples first for hard concepts
    assert SectionKind.prerequisite not in kinds(pro)
    assert beg.metrics.definition_count >= beg.metrics.concept_count - len(
        [s for s in beg.sections if s.kind == SectionKind.prerequisite])  # (background terms are defined too)
    assert any(e.example_first for s in beg.sections for e in s.examples)
    assert not any(e.example_first for s in pro.sections for e in s.examples)
    # Rule_Audience_02: basics reduced, industry cases preferred (as suggested slots when the source has none)
    assert pro.metrics.definition_count < beg.metrics.definition_count
    titles = [e.title for s in pro.sections for e in s.examples if e.origin == Origin.suggested]
    assert not titles or all(t.startswith("산업 사례") for t in titles)


@needs_mqtt
def test_beginner_gets_a_prerequisite_recap_for_dropped_prerequisites(mqtt):
    """A selected concept whose prerequisite was cut for time gets a short background section
    (Rule_Audience_01) for beginners only."""
    _, a = mqtt
    concepts = [
        c.model_copy(update={"importance": 5 if c.name == "Wildcard" else 1}) for c in a.concepts
    ]
    skewed = a.model_copy(update={"concepts": concepts})
    beg = plan(skewed, audience_level="university_beginner", lecture_type="theory", duration_minutes=20)
    pro = plan(skewed, audience_level="professional", lecture_type="theory", duration_minutes=20)
    assert_valid(beg), assert_valid(pro)
    assert "Wildcard" in {n for s in beg.sections for n in s.concepts}
    assert SectionKind.prerequisite in kinds(beg)
    bg = next(s for s in beg.sections if s.kind == SectionKind.prerequisite)
    assert "Topic" in bg.concepts and bg.duration_minutes < 5
    assert SectionKind.prerequisite not in kinds(pro)


@needs_mqtt
def test_introductory_has_more_examples_and_advanced_has_tradeoffs(mqtt):
    _, a = mqtt
    intro = plan(a, difficulty="introductory")
    adv = plan(a, difficulty="advanced")
    assert intro.metrics.example_count >= adv.metrics.example_count
    assert SectionKind.caution in kinds(adv) and SectionKind.caution not in kinds(intro)
    assert "trade-off" in next(s for s in adv.sections if s.kind == SectionKind.caution).title


@needs_mqtt
def test_explanation_depth_and_duration_scale_the_content(mqtt):
    _, a = mqtt
    concise, detailed = plan(a, explanation_depth="concise"), plan(a, explanation_depth="detailed")
    assert concise.metrics.concept_count > detailed.metrics.concept_count  # concise fits more concepts
    assert detailed.metrics.slide_count > concise.metrics.slide_count / 2
    short, long = plan(a, duration_minutes=20), plan(a, duration_minutes=90)
    assert short.metrics.concept_count < long.metrics.concept_count
    assert short.metrics.slide_count < long.metrics.slide_count
    for p in (short, long):
        assert_valid(p)
    # duration does not simply scale slides linearly (spec): 90 min is not 4.5x the 20 min slides
    assert long.metrics.slide_count < short.metrics.slide_count * 4.5


# ================================================================== source policy
@needs_mqtt
def test_source_only_never_adds_suggested_slots_and_source_first_may(mqtt):
    _, a = mqtt
    only = plan(a, source_policy="source_only", example_level="high")
    first = plan(a, source_policy="source_first", example_level="high")
    exp_only = [e for s in only.sections for e in s.examples]
    exp_first = [e for s in first.sections for e in s.examples]
    assert all(e.origin == Origin.source and e.text for e in exp_only)
    assert any(e.origin == Origin.suggested and e.text is None for e in exp_first)  # empty slot, no invented text
    assert len(exp_first) > len(exp_only)
    assert any("생략" in w for w in only.warnings)
    assert comparable(only) != comparable(first)


@needs_mqtt
@pytest.mark.parametrize("opts", [CASE_A, CASE_B, CASE_C, {**BASE, "lecture_type": "exam_preparation"}])
def test_every_text_in_the_plan_is_a_source_excerpt(mqtt, opts):
    material, a = mqtt
    p = plan(a, **opts)
    src = normalize(material.raw_text)

    def excerpt_in_source(t: str) -> bool:
        return normalize(t).rstrip("…").strip() in src

    for s in p.sections:
        for t in s.terms:
            assert t.origin == Origin.source and t.text and excerpt_in_source(t.text), t
            assert t.source_reference is not None
        for e in s.examples:
            if e.origin == Origin.source:
                assert e.text and excerpt_in_source(e.text.split(": ", 1)[-1]) or excerpt_in_source(e.text), e
            else:
                assert e.text is None  # a slot, never invented text
        for pt in s.source_points:
            assert excerpt_in_source(pt), pt
    for c in p.source_constraints:
        assert excerpt_in_source(c.text)


@needs_mqtt
def test_source_scope_notes_are_carried_into_the_plan(mqtt):
    _, a = mqtt
    assert a.scope_notes
    p = plan(a)
    assert [c.text for c in p.source_constraints] == [n.text for n in a.scope_notes]
    assert any("범위 제한" in n for n in p.sections[0].teaching_notes)  # intro warns the presenter
    assert any("범위 제한" in n for s in p.sections[1:] for n in s.teaching_notes)


# ============================================================ structural invariants
@needs_mqtt
def test_invariants_over_a_grid_of_profiles(mqtt):
    _, a = mqtt
    n = 0
    for aud, dur, typ, dep, dif in itertools.product(
        ("general", "university_intermediate", "professional"),
        (5, 10, 20, 45, 90, 240),
        ("theory", "practice", "mixed", "exam_preparation", "example_based"),
        ("concise", "detailed"),
        ("introductory", "advanced"),
    ):
        p = plan(a, audience_level=aud, duration_minutes=dur, lecture_type=typ, explanation_depth=dep, difficulty=dif)
        assert_valid(p)
        # a slide never gets less than 30 seconds
        assert all(s.estimated_slides * 30 <= s.duration_minutes * 60 or s.estimated_slides == 1 for s in p.sections)
        n += 1
    assert n == 3 * 6 * 5 * 2 * 2


@needs_mqtt
def test_very_short_lecture_drops_sections_but_stays_valid(mqtt):
    _, a = mqtt
    p = plan(a, duration_minutes=5, lecture_type="exam_preparation", practice_level="full")
    assert_valid(p)
    assert len(p.sections) <= 5
    assert any("제외" in w for w in p.warnings)


def test_small_source_with_a_long_lecture_warns(tmp_path):
    _, a = analyze_text(tmp_path, CODE_DOC)
    p = plan(a, duration_minutes=240)
    assert_valid(p)
    assert any("분량" in w for w in p.warnings)


@needs_mqtt
def test_planning_is_deterministic(mqtt):
    _, a = mqtt
    assert comparable(plan(a, **CASE_B)) == comparable(plan(a, **CASE_B))


@needs_mqtt
def test_prerequisites_come_before_dependents_inside_a_section(mqtt):
    _, a = mqtt
    prereq = {c.name: set(c.prerequisite) for c in a.concepts}
    p = plan(a, explanation_depth="concise", duration_minutes=90)
    for s in p.sections:
        if s.kind != SectionKind.concept:
            continue
        for i, n in enumerate(s.concepts):
            assert not (prereq.get(n, set()) & set(s.concepts[i + 1:])), (s.title, s.concepts)


def test_metrics_match_the_sections(tmp_path):
    _, a = analyze_text(tmp_path, CODE_DOC)
    p = plan(a, lecture_type="practice", code_level="executable", quiz_mode="both")
    m = p.metrics
    assert m.section_count == len(p.sections) and m.slide_count == p.estimated_slide_count
    assert m.definition_count == sum(len(s.terms) for s in p.sections)
    assert m.example_count == sum(len(s.examples) for s in p.sections)
    assert m.practice_step_count == sum(len(x.steps) for s in p.sections for x in s.practice)
    assert sum(m.minutes_by_kind.values()) == p.duration_minutes
    assert m.practice_minutes == m.minutes_by_kind.get("practice", 0)


# ================================================================ fallbacks / errors
def test_korean_only_document_uses_section_units_without_concepts(tmp_path):
    _, a = analyze_text(tmp_path, KOREAN_NARRATIVE)
    assert a.concepts == []
    p = plan(a)
    assert_valid(p)
    assert any("섹션 제목을 학습 단위" in w for w in p.warnings)
    assert p.metrics.concept_count >= 3


def test_korean_document_analysed_with_llm_gets_concept_based_plan(tmp_path):
    m = parse(tmp_path, KOREAN_DOC)
    a = HybridAnalyzer(FakeLLM(extract=GOOD_EXTRACT)).analyze(m)
    p = plan(a, audience_level="university_beginner", lecture_type="theory")
    assert_valid(p)
    names = {n for s in p.sections for n in s.concepts}
    assert {"광합성", "엽록체", "명반응"} <= names
    assert not any("섹션 제목을 학습 단위" in w for w in p.warnings)


def test_nothing_to_plan_raises_a_readable_error(tmp_path):
    _, a = analyze_text(tmp_path, KOREAN_NARRATIVE)
    empty = a.model_copy(update={"concepts": [], "sections": [], "main_topics": []})
    with pytest.raises(LecturePlanningError) as e:
        LecturePlanner().plan(empty, profile())
    assert "Traceback" not in e.value.message and "강의 계획" in e.value.message


def test_unexpected_planner_bug_is_reported_without_internals(tmp_path, monkeypatch):
    _, a = analyze_text(tmp_path, CODE_DOC)
    monkeypatch.setattr(LecturePlanner, "_select", lambda *x, **k: (_ for _ in ()).throw(RuntimeError("secret")))
    with pytest.raises(LecturePlanningError) as e:
        LecturePlanner().plan(a, profile())
    assert "secret" not in e.value.message


# ==================================================================== model rules
def test_allocate_minutes_sums_exactly_and_gives_every_section_a_minute():
    for total, weights in [(60, [1, 2, 3]), (7, [0.01, 5, 0.01, 0.01]), (5, [1] * 5), (240, [3, 1, 4, 1, 5, 9, 2])]:
        m = allocate_minutes(weights, total)
        assert sum(m) == total and all(x >= 1 for x in m)
    with pytest.raises(InvalidDurationDistribution):
        allocate_minutes([1, 1, 1], 2)
    with pytest.raises(InvalidDurationDistribution):
        allocate_minutes([], 10)


def test_lecture_plan_model_rejects_bad_time_sums_and_slide_counts(tmp_path):
    _, a = analyze_text(tmp_path, CODE_DOC)
    p = plan(a)
    d = json.loads(p.model_dump_json())
    d["duration_minutes"] += 1
    with pytest.raises(ValidationError):
        LecturePlan.model_validate(d)
    d = json.loads(p.model_dump_json())
    d["estimated_slide_count"] += 1
    with pytest.raises(ValidationError):
        LecturePlan.model_validate(d)
    d = json.loads(p.model_dump_json())
    d["sections"][1]["estimated_slides"] = 0
    with pytest.raises(ValidationError):
        LecturePlan.model_validate(d)
    assert LecturePlan.model_validate_json(p.model_dump_json()).model_dump() == p.model_dump()


def test_only_the_six_basic_options_are_enough(tmp_path):
    """SuccessMetric_01"""
    _, a = analyze_text(tmp_path, CODE_DOC)
    p = plan(a)  # BASE has only the 6 basic options
    assert_valid(p) and p.sections
