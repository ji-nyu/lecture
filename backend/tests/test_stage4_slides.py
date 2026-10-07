"""STAGE 4: SlidePlanner. LecturePlan -> SlideSpecification."""

import itertools
import json

import pytest
from pydantic import ValidationError

from app.errors import SlidePlanningError
from app.models.slide_spec import ContentOrigin, Slide, SlideSpecification, SlideType
from app.services.evidence_validator import normalize
from app.services.hybrid_analyzer import HybridAnalyzer
from app.services.lecture_planner import LecturePlanner
from app.services.slide_planner import SlidePlanner, allocate_seconds, clip

from fake_llm import GOOD_EXTRACT, KOREAN_DOC, FakeLLM
from plan_helpers import (
    BASE, CASE_A, CASE_B, CASE_C, CODE_DOC, MQTT_DOCS, analyze_mqtt, analyze_text, parse, plan, profile, slides,
    spec_comparable,
)
from test_stage3_planner import OPTION_CASES

needs_mqtt = pytest.mark.skipif(not MQTT_DOCS, reason="testdocument/*.txt not present")

SPEC_FIELDS = (
    "slide_number", "section_id", "title", "slide_type", "learning_purpose", "key_message", "key_points",
    "source_reference", "visual_instruction", "presenter_instruction", "estimated_explanation_time",
)
STRUCTURAL_PREFIXES = ("선행 개념:", "연관 개념:", "강의 구성:", "언어:")


@pytest.fixture()
def mqtt(tmp_path):
    return analyze_mqtt(tmp_path)  # (material, analysis)


def assert_valid(p, s: SlideSpecification):
    """The hard rules of the specification."""
    assert [x.slide_number for x in s.slides] == list(range(1, len(s.slides) + 1))
    assert s.slide_count == len(s.slides) and s.metrics.slide_count == len(s.slides)
    assert sum(x.estimated_explanation_time for x in s.slides) == p.duration_minutes * 60  # duration kept
    by_sec: dict[str, list[Slide]] = {}
    for x in s.slides:
        by_sec.setdefault(x.section_id, []).append(x)
    assert [sec.id for sec in p.sections] == list(by_sec)  # every section, in plan order, has slides
    for sec in p.sections:
        assert by_sec[sec.id], sec.title  # >= 1 slide per section
        assert sum(x.estimated_explanation_time for x in by_sec[sec.id]) == sec.duration_minutes * 60
    for x in s.slides:  # every spec field is really filled
        for f in ("title", "learning_purpose", "key_message", "visual_instruction", "presenter_instruction"):
            assert getattr(x, f).strip(), (x.slide_number, f)
        assert x.estimated_explanation_time >= 1
        assert isinstance(x.slide_type, SlideType)
    assert s.lecture_id == p.id and s.project_id == p.project_id


def in_source(material, text: str) -> bool:
    return normalize(text.rstrip("…").strip()) in normalize(material.raw_text)


# ==================================================================== structure
@needs_mqtt
def test_every_spec_field_exists_on_every_slide(mqtt):
    _, a = mqtt
    _, s = slides(a, **CASE_A)
    d = s.slides[0].model_dump()
    for f in SPEC_FIELDS:
        assert f in d
    assert isinstance(d["estimated_explanation_time"], int)


@needs_mqtt
@pytest.mark.parametrize("opts", [CASE_A, CASE_B, CASE_C, {**BASE, "lecture_type": "exam_preparation"}])
def test_every_slide_has_a_learning_purpose_and_the_spec_matches_the_plan(mqtt, opts):
    """SuccessMetric_04 + the STAGE 4 completion criterion for a 60 minute lecture."""
    _, a = mqtt
    p, s = slides(a, **opts)
    assert_valid(p, s)
    assert s.slide_count == p.estimated_slide_count  # the preview number is the final number
    assert s.plan_estimated_slide_count == p.estimated_slide_count
    for x in s.slides:
        assert len(x.learning_purpose) >= 10 and x.learning_purpose.endswith("다.")
    assert not any("다릅니다" in w for w in s.warnings)


@needs_mqtt
def test_60_minute_lecture_has_a_reason_for_every_slide(mqtt):
    _, a = mqtt
    p, s = slides(a, **CASE_A)
    assert p.duration_minutes == 60 and s.slide_count >= 20
    assert all(x.learning_purpose for x in s.slides)
    # not one generic sentence pasted everywhere: purposes are specific to the slide
    assert len({x.learning_purpose for x in s.slides}) >= s.slide_count * 0.6


@needs_mqtt
def test_lecture_flow_starts_with_title_and_ends_with_summary(mqtt):
    _, a = mqtt
    for opts in (CASE_A, CASE_B, CASE_C):
        _, s = slides(a, **opts)
        assert s.slides[0].slide_type == SlideType.title
        assert s.slides[0].title == s.title
        assert s.slides[1].slide_type == SlideType.agenda
        assert s.slides[-1].slide_type == SlideType.summary


@needs_mqtt
def test_intro_with_two_slides_merges_objectives_and_agenda(mqtt):
    _, a = mqtt
    p, s = slides(a, **CASE_A)
    intro = [x for x in s.slides if x.section_id == p.sections[0].id]
    assert len(intro) == p.sections[0].estimated_slides == 2
    assert intro[1].title == "학습 목표와 강의 구성"
    assert any(pt.startswith("강의 구성:") for pt in intro[1].key_points)
    assert set(p.learning_objectives) <= set(intro[1].key_points)


@needs_mqtt
def test_example_first_puts_the_example_before_its_concept(mqtt):
    _, a = mqtt
    p, s = slides(a, **CASE_A)
    firsts = [e for sec in p.sections for e in sec.examples if e.example_first and e.origin.value == "source"]
    assert firsts
    for e in firsts:
        ex = next(i for i, x in enumerate(s.slides) if x.slide_type == SlideType.example and x.title == e.title)
        concept = e.concepts[0]
        cs = next(
            i for i, x in enumerate(s.slides)
            if x.slide_type in (SlideType.concept, SlideType.architecture, SlideType.workflow) and concept in x.concepts
        )
        assert ex < cs, e.title


@needs_mqtt
def test_slides_follow_their_section_content(mqtt):
    """The slide structure is a faithful refinement of the plan, not a new structure."""
    _, a = mqtt
    p, s = slides(a, **CASE_B)
    for sec in p.sections:
        mine = [x for x in s.slides if x.section_id == sec.id]
        types = [x.slide_type for x in mine]
        if sec.kind.value == "practice":
            assert all(t == SlideType.practice for t in types)
            acts = [x for x in mine if x.title in {act.title for act in sec.practice}]
            assert len(acts) == len(sec.practice)
            for act in sec.practice:  # every step of the activity is on its slide, in order
                assert next(x for x in mine if x.title == act.title).key_points == act.steps
        if sec.kind.value == "concept":
            covered = {c for x in mine for c in x.concepts}
            assert set(sec.concepts) <= covered
            assert sum(1 for x in mine if x.slide_type == SlideType.definition) == -(-len(sec.terms) // 2)
            assert sum(1 for x in mine if x.slide_type == SlideType.example) == len(sec.examples)


# ===================================================== options change the slides
@needs_mqtt
def test_three_cases_give_clearly_different_slide_structures(mqtt):
    _, a = mqtt
    res = {n: slides(a, **o)[1] for n, o in {"A": CASE_A, "B": CASE_B, "C": CASE_C}.items()}
    for x, y in itertools.combinations(res, 2):
        assert spec_comparable(res[x]) != spec_comparable(res[y])
        assert res[x].metrics.slides_by_type != res[y].metrics.slides_by_type
        assert res[x].slide_count != res[y].slide_count
    assert SlideType.practice.value in res["B"].metrics.slides_by_type
    assert SlideType.practice.value not in res["A"].metrics.slides_by_type
    assert res["A"].metrics.slides_by_type[SlideType.definition.value] > res["C"].metrics.slides_by_type.get(
        SlideType.definition.value, 0)
    # theory/beginner: more definitions than the practice lecture (spec Rule_LectureType_01/02)
    assert res["A"].metrics.slides_by_type["definition"] > res["B"].metrics.slides_by_type.get("definition", 0)
    assert res["B"].metrics.slides_by_type["practice"] >= 4


@needs_mqtt
@pytest.mark.parametrize("option", list(OPTION_CASES))
def test_every_option_changes_the_slide_specification(mqtt, option):
    """Rule_Options_01 for the SlideSpecification: not metadata only."""
    _, a = mqtt
    lo, hi = OPTION_CASES[option]
    s1, s2 = slides(a, **lo)[1], slides(a, **hi)[1]
    assert spec_comparable(s1) != spec_comparable(s2)
    slides_only = lambda s: json.dumps(  # noqa: E731  (the slides themselves, without the style record)
        [x.model_dump(mode="json") for x in s.slides], ensure_ascii=False, sort_keys=True
    )
    assert slides_only(s1) != slides_only(s2), f"{option} only changed the recorded style"


def test_code_level_changes_the_slides_and_code_is_never_invented(tmp_path):
    _, a = analyze_text(tmp_path, CODE_DOC)
    none = slides(a, code_level="none")[1]
    snip = slides(a, code_level="snippet")[1]
    execu = slides(a, lecture_type="practice", code_level="executable")[1]
    assert SlideType.code not in [x.slide_type for x in none.slides]
    code = [x for x in snip.slides if x.slide_type == SlideType.code]
    assert len(code) == 1
    c = code[0]
    assert c.source_reference and c.source_reference.section_title == "Publish 실습"
    assert c.content_origin == ContentOrigin.source and "python" in " ".join(c.key_points)
    assert "그대로" in c.visual_instruction and "새로 쓰지 않는다" in c.visual_instruction
    assert "읽으며" in c.presenter_instruction
    ec = next(x for x in execu.slides if x.slide_type == SlideType.code)
    assert "실행" in ec.presenter_instruction and "실행 결과" in ec.visual_instruction


@needs_mqtt
def test_visual_level_changes_slide_types_and_visual_instructions(mqtt):
    _, a = mqtt
    visual_types = {SlideType.architecture, SlideType.workflow, SlideType.diagram}
    res = {lv: slides(a, visual_level=lv)[1] for lv in ("low", "medium", "high")}
    counts = {lv: sum(1 for x in s.slides if x.slide_type in visual_types) for lv, s in res.items()}
    assert counts["low"] == 0  # low: text only, no diagram slides
    assert counts["high"] > counts["medium"] > counts["low"] or counts["high"] > counts["low"]
    assert res["high"].metrics.visual_slide_count > res["low"].metrics.visual_slide_count
    ins = {lv: {x.visual_instruction for x in s.slides} for lv, s in res.items()}
    assert ins["low"] != ins["medium"] != ins["high"]
    assert any("텍스트만" in v or "텍스트 목록만" in v or "그림 없이" in v for v in ins["low"])
    assert any("전체 화면" in v or "대표 이미지" in v or "일러스트" in v for v in ins["high"])


@needs_mqtt
def test_slide_density_limits_key_points(mqtt):
    _, a = mqtt
    cap = {"concise": 3, "normal": 5, "detailed": 7}
    avg = {}
    for d, n in cap.items():
        _, s = slides(a, slide_density=d)
        assert s.style.max_key_points == n
        for x in s.slides:
            if x.slide_type in (SlideType.definition, SlideType.concept, SlideType.comparison, SlideType.example,
                                SlideType.architecture, SlideType.workflow):
                assert len(x.key_points) <= n, (d, x.title)
        avg[d] = s.metrics.avg_key_points
    assert avg["concise"] < avg["detailed"]


@needs_mqtt
def test_speaker_notes_control_how_much_the_presenter_is_told(mqtt):
    _, a = mqtt
    size = {}
    for lv in ("none", "concise", "full"):
        _, s = slides(a, speaker_notes=lv)
        size[lv] = sum(len(x.presenter_instruction) for x in s.slides) / s.slide_count
        assert all(x.presenter_instruction.strip() for x in s.slides)  # required field, even for "none"
    assert size["none"] < size["concise"] < size["full"]
    _, full = slides(a, speaker_notes="full")
    assert any("권장 시간" in x.presenter_instruction for x in full.slides)
    assert any("근거: 자료" in x.presenter_instruction for x in full.slides)


@needs_mqtt
def test_lecture_tone_changes_only_how_the_presenter_speaks(mqtt):
    _, a = mqtt
    res = {t: slides(a, lecture_tone=t)[1] for t in ("academic", "professional", "conversational")}
    assert len({tuple(x.presenter_instruction for x in s.slides) for s in res.values()}) == 3
    # structure is identical: same slides, same types, same times
    sig = lambda s: [(x.slide_type, x.title, x.estimated_explanation_time) for x in s.slides]  # noqa: E731
    assert sig(res["academic"]) == sig(res["conversational"])
    assert any("질문" in x.presenter_instruction for x in res["conversational"].slides)
    assert not any("질문을 던지며" in x.presenter_instruction for x in res["academic"].slides)


@needs_mqtt
def test_audience_and_difficulty_shape_purposes_and_presenter_notes(mqtt):
    _, a = mqtt
    beg = slides(a, audience_level="university_beginner", difficulty="introductory", speaker_notes="full")[1]
    pro = slides(a, audience_level="professional", difficulty="advanced", speaker_notes="full")[1]
    text = lambda s: " ".join(x.presenter_instruction for x in s.slides)  # noqa: E731
    assert "쉬운 예시" in text(beg) and "쉬운 예시" not in text(pro)
    assert "내부 동작" in text(pro) and "trade-off" in text(pro)
    purposes = lambda s: " ".join(x.learning_purpose for x in s.slides)  # noqa: E731
    assert "쉬운 말로" in purposes(beg) and "trade-off" in purposes(pro)


@needs_mqtt
def test_lecture_types_shape_the_slide_types(mqtt):
    _, a = mqtt
    exam = slides(a, lecture_type="exam_preparation")[1]
    theory = slides(a, lecture_type="theory")[1]
    practice = slides(a, lecture_type="practice", practice_level="full")[1]
    ex_types = exam.metrics.slides_by_type
    assert ex_types.get("comparison", 0) >= 1 and ex_types.get("quiz", 0) >= 1  # Rule_LectureType_03
    assert "comparison" not in theory.metrics.slides_by_type and "practice" not in theory.metrics.slides_by_type
    assert practice.metrics.slides_by_type["practice"] >= 5  # Rule_LectureType_02
    pr = [x for x in practice.slides if x.slide_type == SlideType.practice and x.title.endswith("적용 실습")]
    assert pr and all(len(x.key_points) >= 4 for x in pr)  # step-by-step
    assert all("직접 수행" in x.presenter_instruction for x in pr)


@needs_mqtt
def test_quiz_slides_follow_quiz_mode(mqtt):
    _, a = mqtt
    none = slides(a, quiz_mode="none")[1]
    both = slides(a, quiz_mode="both")[1]
    assert "quiz" not in none.metrics.slides_by_type
    assert both.metrics.slides_by_type["quiz"] >= 2
    q = next(x for x in both.slides if x.slide_type == SlideType.quiz)
    assert q.key_points and "먼저 답하게" in q.presenter_instruction


# ==================================================================== grounding
@needs_mqtt
@pytest.mark.parametrize("opts", [CASE_A, CASE_B, CASE_C, {**BASE, "lecture_type": "exam_preparation"}])
def test_slide_text_comes_from_the_source(mqtt, opts):
    material, a = mqtt
    p, s = slides(a, **opts)
    names = {c for sec in p.sections for c in sec.concepts}
    objectives = set(p.learning_objectives)
    for x in s.slides:
        if x.message_from_source:
            assert in_source(material, x.key_message), (x.title, x.key_message)
            assert x.content_origin == ContentOrigin.source
        for pt in x.key_points:
            if x.slide_type in (SlideType.practice, SlideType.quiz, SlideType.title) or pt in objectives:
                continue
            if pt in names or pt.startswith(STRUCTURAL_PREFIXES):
                continue
            body = pt.split(": ", 1)[-1]
            assert in_source(material, pt) or in_source(material, body) or body in names, (x.title, pt)
        if x.source_reference is not None:
            assert x.source_reference.section_title or x.source_reference.line or x.source_reference.page


@needs_mqtt
def test_source_only_has_no_suggested_slides_and_source_first_marks_them(mqtt):
    _, a = mqtt
    only = slides(a, source_policy="source_only", example_level="high")[1]
    first = slides(a, source_policy="source_first", example_level="high")[1]
    assert only.metrics.suggested_slide_count == 0
    assert first.metrics.suggested_slide_count > 0
    sug = [x for x in first.slides if x.content_origin == ContentOrigin.suggested]
    for x in sug:
        assert not x.message_from_source and x.source_reference is None
        assert "충돌하지 않게" in x.presenter_instruction or "충돌" in x.key_message or "보충" in x.key_message
    # Rule_Source_01 / 02 are visible in the presenter notes
    full = slides(a, source_policy="source_only", speaker_notes="full")[1]
    assert all("추가하지 않는다" in x.presenter_instruction for x in full.slides if x.slide_type == SlideType.concept)
    fs = slides(a, source_policy="source_first", speaker_notes="full")[1]
    assert all("충돌하는 내용은 말하지 않는다" in x.presenter_instruction for x in fs.slides
               if x.slide_type == SlideType.concept)
    assert spec_comparable(only) != spec_comparable(first)


@needs_mqtt
def test_range_limits_of_the_source_reach_the_presenter(mqtt):
    _, a = mqtt
    p, s = slides(a, speaker_notes="full")
    assert p.source_constraints
    assert "자료의 범위 제한" in s.slides[0].presenter_instruction


@needs_mqtt
def test_caution_slides_only_carry_source_caution_sentences(mqtt):
    material, a = mqtt
    p, s = slides(a, **CASE_C)
    cau = [x for x in s.slides if x.section_id == next(sec.id for sec in p.sections if sec.kind.value == "caution")]
    assert cau
    for x in cau:
        assert x.message_from_source and in_source(material, x.key_message)
        assert any(k in x.key_message for k in ("위험", "한계", "주의", "고려사항", "문제", "장애"))


# ============================================================ invariants / fits
@needs_mqtt
def test_invariants_over_a_grid_of_profiles(mqtt):
    _, a = mqtt
    n = mismatches = 0
    for aud, dur, typ, dep, dif, vis in itertools.product(
        ("general", "professional"),
        (5, 10, 20, 45, 90, 240),
        ("theory", "practice", "exam_preparation", "example_based"),
        ("concise", "detailed"),
        ("introductory", "advanced"),
        ("low", "high"),
    ):
        p, s = slides(a, audience_level=aud, duration_minutes=dur, lecture_type=typ, explanation_depth=dep,
                      difficulty=dif, visual_level=vis)
        assert_valid(p, s)
        assert all(x.estimated_explanation_time >= 1 for x in s.slides)
        mismatches += s.slide_count != p.estimated_slide_count
        n += 1
    assert n == 2 * 6 * 4 * 2 * 2 * 2
    assert mismatches == 0  # the slide count in the plan preview is the final slide count


@needs_mqtt
def test_time_budget_too_short_for_the_content_still_gives_a_valid_spec(mqtt):
    _, a = mqtt
    p, s = slides(a, duration_minutes=5, lecture_type="exam_preparation", practice_level="full", quiz_mode="both")
    assert_valid(p, s)
    assert all(x.estimated_explanation_time >= 15 or p.duration_minutes * 60 < 15 * s.slide_count for x in s.slides)


def test_small_source_and_long_lecture_stay_valid(tmp_path):
    _, a = analyze_text(tmp_path, CODE_DOC)
    for dur in (10, 60, 240):
        p, s = slides(a, duration_minutes=dur)
        assert_valid(p, s)


@needs_mqtt
def test_planning_is_deterministic(mqtt):
    _, a = mqtt
    assert spec_comparable(slides(a, **CASE_B)[1]) == spec_comparable(slides(a, **CASE_B)[1])


def test_without_the_analysis_the_slides_are_still_valid_but_carry_names_only(tmp_path):
    _, a = analyze_text(tmp_path, CODE_DOC)
    p = plan(a, **CASE_A)
    s = SlidePlanner().plan(p)  # no SourceAnalysis
    assert_valid(p, s)
    assert any("분석 정보 없이" in w for w in s.warnings)
    assert not any(x.message_from_source and x.slide_type == SlideType.concept for x in s.slides)


def test_korean_only_document_with_sections_as_units(tmp_path):
    from fake_llm import KOREAN_NARRATIVE

    _, a = analyze_text(tmp_path, KOREAN_NARRATIVE)
    p, s = slides(a)
    assert_valid(p, s)


def test_korean_document_with_llm_concepts(tmp_path):
    m = parse(tmp_path, KOREAN_DOC)
    a = HybridAnalyzer(FakeLLM(extract=GOOD_EXTRACT)).analyze(m)
    p = LecturePlanner().plan(a, profile(audience_level="university_beginner", lecture_type="theory"))
    s = SlidePlanner().plan(p, a)
    assert_valid(p, s)
    assert {"광합성", "엽록체"} <= {c for x in s.slides for c in x.concepts}


# ===================================================================== helpers
def test_allocate_seconds_is_exact_and_gives_every_slide_time():
    for total, w in [(60, [1, 2, 3]), (600, [1] * 7), (30, [0.01, 9, 0.01]), (3600, [3, 1, 4, 1, 5, 9, 2, 6])]:
        out = allocate_seconds(w, total)
        assert sum(out) == total and all(x >= 1 for x in out)
    with pytest.raises(SlidePlanningError):
        allocate_seconds([1, 1, 1], 2)


def test_clip_is_a_verbatim_prefix():
    src = "Broker는 MQTT의 중계 중심이며 모든 메시지는 Broker를 거쳐 전달된다. 그래서 단일 장애점이 된다."
    out = clip(src, 30)
    assert out.endswith("…") and src.startswith(out[:-1].rstrip()) and len(out) <= 31
    assert clip("짧은 문장이다.", 30) == "짧은 문장이다."
    assert clip("a   b\n c", 30) == "a b c"


# ================================================================== model rules
def test_slide_specification_rejects_bad_numbers_times_and_empty_purposes(tmp_path):
    _, a = analyze_text(tmp_path, CODE_DOC)
    p, s = slides(a)
    d = json.loads(s.model_dump_json())
    for mutate in (
        lambda d: d["slides"][1].update(slide_number=9),  # gap in numbering
        lambda d: d["slides"][0].update(estimated_explanation_time=d["slides"][0]["estimated_explanation_time"] + 1),
        lambda d: d.update(slide_count=d["slide_count"] + 1),
        lambda d: d["slides"][2].update(learning_purpose=""),  # a slide without a purpose
        lambda d: d["slides"][2].update(slide_type="banner"),
        lambda d: d.update(slides=[], slide_count=0),
    ):
        bad = json.loads(json.dumps(d))
        mutate(bad)
        with pytest.raises(ValidationError):
            SlideSpecification.model_validate(bad)
    assert SlideSpecification.model_validate_json(s.model_dump_json()).model_dump() == s.model_dump()


def test_all_spec_slide_types_are_declared():
    assert {t.value for t in SlideType} == {
        "title", "agenda", "concept", "definition", "comparison", "diagram", "architecture", "workflow",
        "example", "practice", "code", "formula", "quiz", "summary",
    }


def test_concept_and_definition_slides_carry_source_sentences_not_just_the_name(tmp_path):
    """A bullet that is only the concept name becomes an empty labelled box in the renderer."""
    thin = """# AIoT 입문

## AIoT란 무엇인가

AIoT는 Artificial Intelligence of Things의 약자로, 사물인터넷(IoT)에 인공지능(AI)을 결합한 체계를 말한다.
IoT가 센서와 장치를 연결해 데이터를 모은다면, AIoT는 그 데이터를 학습·추론에 활용해 자동으로 판단한다.
AIoT는 IoT를 대체하는 다른 물건이 아니라 IoT를 확장한 개념이다.
"""
    _, a = analyze_text(tmp_path, thin)
    _, s = slides(
        a, audience_level="general", duration_minutes=20, difficulty="introductory",
        lecture_type="theory", explanation_depth="concise",
    )
    defs = [x for x in s.slides if x.slide_type == SlideType.definition]
    concepts = [x for x in s.slides if x.slide_type == SlideType.concept]
    assert defs and concepts
    for x in defs + concepts:
        assert x.key_points
        assert not all(p in x.concepts or p == x.title for p in x.key_points), (x.title, x.key_points)


# ======================================================================= errors
def test_unexpected_planner_crash_is_reported_without_internals(tmp_path, monkeypatch):
    _, a = analyze_text(tmp_path, CODE_DOC)
    p = plan(a)
    monkeypatch.setattr(SlidePlanner, "_section_seeds", lambda *x, **k: 1 / 0)
    with pytest.raises(SlidePlanningError) as e:
        SlidePlanner().plan(p, a)
    assert "ZeroDivision" not in e.value.message and "Traceback" not in e.value.message
