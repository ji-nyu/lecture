"""STAGE 6B: GensparkPromptBuilder (structure, content, options, fallbacks, determinism)."""

import re
from pathlib import Path

import pytest

import app.services.prompt_builder as pb
from app.models import lecture_profile as lp
from app.models.enriched_slide_spec import QuizContent
from app.models.presentation_prompt import PromptContentSource
from app.models.slide_spec import ContentOrigin, SlideType
from app.services.prompt_builder import SECTION_NAMES, GensparkPromptBuilder
from app.services.slide_planner import SlidePlanner

from enrich_helpers import CASE_A, CASE_B, CASE_C, ScriptedLLM, make_ctx, only, run
from plan_helpers import BASE, CODE_DOC, analyze_text, plan, profile
from app.services.llm_client import LLMTimeout


def build(ctx, enriched=None, *, analysis=True, material=None, **kw):
    return GensparkPromptBuilder().build(
        project_id="p" * 32, profile=ctx.profile, plan=ctx.plan, spec=ctx.spec,
        analysis=ctx.analysis if analysis else None, material=material, enriched=enriched, **kw,
    )


def slide_blocks(text):
    part = text.split("\n## SLIDE SPECIFICATION\n", 1)[1].split("\n## VISUAL REQUIREMENTS\n", 1)[0]
    return re.split(r"(?m)^(?=### 슬라이드 )", part)[1:]


def section(text, name):
    after = text.split(f"\n## {name}\n", 1)[1]
    m = re.search(r"(?m)^## ", after)
    return after[: m.start()] if m else after


# ------------------------------------------------------------------ structure
def test_the_prompt_has_the_13_sections_of_the_spec_in_order():
    r = build(make_ctx("mqtt", **CASE_B))
    assert re.findall(r"(?m)^## (.+)$", r.final_prompt) == list(SECTION_NAMES)
    assert r.section_names == list(SECTION_NAMES) and [s.name for s in r.sections] == list(SECTION_NAMES)
    assert all(s.char_count > 0 for s in r.sections)
    assert len(SECTION_NAMES) == 13


def test_every_slide_appears_exactly_once_in_order_with_its_title_and_time():
    ctx = make_ctx("mqtt", **CASE_B)
    r = build(ctx, run(ctx))
    blocks = slide_blocks(r.final_prompt)
    assert len(blocks) == ctx.spec.slide_count == r.slide_count
    for slide, block in zip(ctx.spec.slides, blocks):
        assert block.startswith(f"### 슬라이드 {slide.slide_number}/{ctx.spec.slide_count} · {slide.title}\n")
        assert slide.section_title in block and slide.learning_purpose in block
        assert "예상 설명 시간:" in block and "시각 지시:" in block
    assert [i.slide_number for i in r.slides] == list(range(1, ctx.spec.slide_count + 1))
    assert r.validation.passed is True and r.validation.errors == []


def test_the_prompt_alone_says_what_lecture_to_make():
    ctx = make_ctx("mqtt", **CASE_B)
    r = build(ctx, run(ctx))
    t = r.final_prompt
    assert ctx.plan.title in t and f"{ctx.spec.slide_count}장" in t and f"{ctx.plan.duration_minutes}분" in t
    for obj in ctx.plan.learning_objectives:
        assert obj in t
    for sec in ctx.plan.sections:
        assert sec.title in t
    assert len(t) > 5000  # not "MQTT 60분 PPT 만들어줘"
    assert "정확히" in section(t, "ROLE") and "추가, 삭제, 병합, 분할하지 않는다" in section(t, "ROLE")


def test_the_prompt_is_deterministic_and_has_no_time_stamp():
    ctx = make_ctx("mqtt", **CASE_A)
    e = run(ctx)
    a, b = build(ctx, e), build(ctx, e)
    assert a.final_prompt == b.final_prompt and a.prompt_hash == b.prompt_hash
    assert a.char_count == len(a.final_prompt) and a.line_count == a.final_prompt.count("\n") + 1
    assert str(a.version.generated_at.year) not in section(a.final_prompt, "ROLE")


def test_no_template_leftovers_and_no_provider_name_in_the_text():
    ctx = make_ctx("mqtt", **CASE_B)
    t = build(ctx, run(ctx)).final_prompt
    assert "None" not in t and "{" not in t and "}" not in t and "null" not in t
    assert "genspark" not in t.lower()  # provider independent


def test_the_builder_knows_nothing_about_a_provider():
    src = Path(pb.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+(httpx|requests|urllib|openai|\.\.?providers)", src, re.M)
    assert "providers" not in src  # the prompt is provider independent


# ------------------------------------------------------------------ options change the text
OPTION_WORDING = [
    ("audience_level", "professional", "실무 전문가"),
    ("audience_level", "high_school", "고등학생"),
    ("difficulty", "advanced", "난이도: 고급"),
    ("difficulty", "introductory", "난이도: 입문"),
    ("explanation_depth", "concise", "설명 깊이: 간결"),
    ("explanation_depth", "detailed", "설명 깊이: 상세"),
    ("lecture_type", "exam_preparation", "시험 대비"),
    ("lecture_type", "practice", "실습 중심"),
    ("source_policy", "source_only", "원문 전용(source_only)"),
    ("source_policy", "expanded", "확장(expanded)"),
    ("slide_density", "concise", "슬라이드 밀도=간결"),
    ("slide_density", "detailed", "슬라이드 밀도=상세"),
    ("visual_level", "high", "시각 수준=높음"),
    ("visual_level", "low", "시각 수준=낮음"),
    ("example_level", "none", "예시 수준=없음"),
    ("example_level", "high", "예시 수준=높음"),
    ("practice_level", "guided", "실습 수준=따라 하기"),
    ("practice_level", "none", "실습 수준=없음"),
    ("code_level", "snippet", "코드 수준=발췌"),
    ("code_level", "executable", "코드 수준=실행"),
    ("quiz_mode", "both", "퀴즈 수준=중간+마무리"),
    ("quiz_mode", "final", "퀴즈 수준=마무리"),
    ("lecture_tone", "conversational", "어조/디자인: 대화체"),
    ("lecture_tone", "professional", "어조/디자인: 전문적"),
    ("speaker_notes", "full", "발표자 노트 수준=대본"),
    ("speaker_notes", "concise", "발표자 노트 수준=간단"),
    ("speaker_notes", "none", "발표자 노트 수준=없음"),
]


@pytest.mark.parametrize("option,value,wording", OPTION_WORDING)
def test_every_option_changes_the_wording_of_the_prompt(option, value, wording):
    ctx = make_ctx("mqtt", **{option: value})
    assert getattr(ctx.profile, option).value == value
    assert wording in build(ctx).final_prompt


def test_the_wording_tables_cover_every_value_of_every_option():
    enums = [
        (pb._AUDIENCE, lp.AudienceLevel), (pb._DIFFICULTY, lp.Difficulty), (pb._DEPTH, lp.ExplanationDepth),
        (pb._TYPE, lp.LectureType), (pb._POLICY, lp.SourcePolicy), (pb._DENSITY, lp.SlideDensity),
        (pb._VISUAL, lp.VisualLevel), (pb._EXAMPLE, lp.ExampleLevel), (pb._PRACTICE, lp.PracticeLevel),
        (pb._CODE, lp.CodeLevel), (pb._QUIZ, lp.QuizMode), (pb._TONE, lp.LectureTone), (pb._NOTES, lp.SpeakerNotes),
    ]
    for table, enum in enums:
        assert set(table) == {m.value for m in enum}, enum.__name__
    assert {t.value for t in SlideType} <= set(pb._LAYOUT) and {t.value for t in SlideType} <= set(pb._TYPE_LABEL)


def test_the_three_spec_profiles_give_clearly_different_prompts():
    texts = {}
    for name, case in (("A", CASE_A), ("B", CASE_B), ("C", CASE_C)):
        ctx = make_ctx("mqtt", **case)
        texts[name] = build(ctx, run(ctx))
    a, b, c = (texts[k] for k in "ABC")
    assert len({a.prompt_hash, b.prompt_hash, c.prompt_hash}) == 3
    assert "대학 초급" in section(a.final_prompt, "AUDIENCE") and "실무 전문가" in section(c.final_prompt, "AUDIENCE")
    assert section(a.final_prompt, "DIFFICULTY") != section(c.final_prompt, "DIFFICULTY")
    assert section(a.final_prompt, "LECTURE STYLE") != section(b.final_prompt, "LECTURE STYLE")
    assert "30분" in c.final_prompt and "60분" in a.final_prompt
    assert a.slide_count != c.slide_count
    assert a.options["audience_level"] == "university_beginner" and c.options["audience_level"] == "professional"


def test_speaker_notes_none_leaves_no_notes_anywhere():
    ctx = make_ctx("mqtt", speaker_notes="none")
    r = build(ctx, run(ctx))
    t = r.final_prompt
    assert t.count("- 발표자 노트: 작성하지 않는다.") == ctx.spec.slide_count
    slides = section(t, "SLIDE SPECIFICATION")
    assert "발표자 노트(" not in slides and "발표 방향" not in slides and "발표자 노트(" not in t
    assert "노트란은 비워 둔다" in section(t, "SPEAKER NOTES REQUIREMENTS")


@pytest.mark.parametrize("mode", ["concise", "full"])
def test_speaker_notes_are_carried_over_verbatim(mode):
    ctx = make_ctx("mqtt", speaker_notes=mode)
    e = run(ctx)
    t = build(ctx, e).final_prompt
    notes = [s.enriched.presenter_notes for s in e.slides if s.enriched and s.enriched.presenter_notes]
    assert notes and all(n.strip() in t for n in notes)
    assert "노트란에만 넣고" in section(t, "SPEAKER NOTES REQUIREMENTS")


def test_visual_requirements_list_the_slides_that_need_a_diagram():
    ctx = make_ctx("mqtt", visual_level="high")
    r = build(ctx)
    need = [s.slide_number for s in ctx.spec.slides if s.slide_type.value in ("diagram", "architecture", "workflow", "comparison")]
    assert need and f"총 {len(need)}장" in section(r.final_prompt, "VISUAL REQUIREMENTS")


# ------------------------------------------------------------------ content: enriched, rule-based, fallback
def test_enriched_content_is_used_and_marked():
    ctx = make_ctx("mqtt", **CASE_B)
    e = run(ctx)
    r = build(ctx, e)
    assert r.content_source == PromptContentSource.enriched and r.enrichment_status == "enriched"
    assert r.version.enrichment_model == e.version.model_name and r.version.enrichment_prompt_version
    assert all(i.content == "enriched" for i in r.slides) and r.fallback_slide_numbers == []
    first = next(s for s in e.slides if s.enriched and s.enriched.explanation)
    assert first.enriched.explanation.strip() in r.final_prompt
    assert r.version.slide_spec_hash == e.version.slide_spec_hash


def test_without_enrichment_the_rule_based_content_is_used_and_says_so():
    ctx = make_ctx("mqtt", **CASE_B)
    r = build(ctx)
    assert r.content_source == PromptContentSource.rule_based and r.enrichment_status is None
    assert any("콘텐츠 보강 없이" in w for w in r.warnings)
    assert r.validation.passed
    for s in ctx.spec.slides[:5]:
        assert s.key_message in r.final_prompt and s.visual_instruction in r.final_prompt
    assert "발표 방향" in r.final_prompt  # the planner's direction, not written notes


def test_a_slide_whose_enrichment_failed_uses_the_planners_text():
    ctx = make_ctx("mqtt", **CASE_B)
    e = run(ctx, ScriptedLLM(error=only({3, 9}, LLMTimeout("t"))))
    r = build(ctx, e)
    assert r.fallback_slide_numbers == [3, 9] and r.content_source == PromptContentSource.enriched
    assert r.validation.passed
    blocks = slide_blocks(r.final_prompt)
    for n in (3, 9):
        s = ctx.spec.slides[n - 1]
        assert s.key_message in blocks[n - 1] and "발표 방향" in blocks[n - 1]
    assert any("3, 9번" in w and "규칙 기반" in w for w in r.warnings)
    assert {i.content for i in r.slides} >= {"enriched", "rule_based_fallback"}


def test_when_every_enrichment_failed_the_prompt_is_rule_based():
    ctx = make_ctx("mqtt", **CASE_B)
    e = run(ctx, ScriptedLLM(error=lambda req: LLMTimeout("t")))
    assert e.status.value == "enrichment_failed"
    r = build(ctx, e)
    assert r.content_source == PromptContentSource.rule_based and r.validation.passed
    assert any("모든 슬라이드에서 실패" in w for w in r.warnings)


def test_enrichment_of_another_lecture_is_never_used():
    a, b = make_ctx("mqtt", **CASE_A), make_ctx("mqtt", **CASE_B)
    r = build(b, run(a))
    assert r.content_source == PromptContentSource.rule_based
    assert any("현재 슬라이드 구성과 맞지 않아" in w for w in r.warnings)


def test_slides_the_source_has_nothing_for_are_marked_not_invented():
    ctx = make_ctx("mqtt", **CASE_A)
    suggested = [s.slide_number for s in ctx.spec.slides if s.content_origin == ContentOrigin.suggested]
    assert suggested
    r = build(ctx)
    assert r.placeholder_slide_numbers == suggested
    blocks = slide_blocks(r.final_prompt)
    for n in suggested:
        assert "- 내용 지시:" in blocks[n - 1]
    assert "- 내용 지시:" not in "".join(b for i, b in enumerate(blocks, 1) if i not in suggested)


def test_source_only_never_asks_the_provider_to_fill_in_facts():
    ctx = make_ctx("mqtt", **{**CASE_B, "source_policy": "source_only"})
    r = build(ctx, run(ctx))
    assert "- 내용 지시:" not in r.final_prompt
    pol = section(r.final_prompt, "SOURCE POLICY")
    assert "추가하지 않는다" in pol and "비유와 외부 사례를 만들지 않는다" in pol


def test_llm_made_examples_are_labelled_as_not_from_the_source():
    ctx = make_ctx("mqtt", **CASE_A)
    e = run(ctx)
    made = [s for s in e.slides if s.provenance.get("example") == "llm_example" and s.enriched and s.enriched.example]
    assert made
    r = build(ctx, e)
    block = slide_blocks(r.final_prompt)[made[0].slide_number - 1]
    assert "교육용으로 보충한 예시, 원문에 없음" in block


def test_an_enriched_slide_that_shows_only_a_message_keeps_the_planners_points():
    ctx = make_ctx("mqtt", **CASE_B)
    e = run(ctx)
    victim = next(s for s in e.slides if s.enriched and s.original.key_points and s.slide_type.value == "concept")
    n = victim.slide_number
    for f in ("body_points", "explanation", "example", "analogy", "practice_instruction", "code_explanation",
              "quiz_content", "summary_message"):
        setattr(victim.enriched, f, [] if f == "body_points" else None)
    e.version.slide_spec_hash = e.version.slide_spec_hash  # unchanged: the structure is the same
    r = build(ctx, e)
    block = slide_blocks(r.final_prompt)[n - 1]
    assert "- 본문 항목:" in block and victim.original.key_points[0] in block


# ------------------------------------------------------------------ source: code, constraints, material
def test_a_code_slide_carries_the_source_code_verbatim(tmp_path):
    material, analysis = analyze_text(tmp_path, CODE_DOC)
    p = plan(analysis, code_level="snippet", lecture_type="practice", practice_level="guided")
    spec = SlidePlanner().plan(p, analysis)
    ctx = make_ctx(CODE_DOC, code_level="snippet", lecture_type="practice", practice_level="guided")
    assert any(s.slide_type == SlideType.code for s in spec.slides)
    r = GensparkPromptBuilder().build(
        project_id="p" * 32, profile=profile(code_level="snippet", lecture_type="practice", practice_level="guided"),
        plan=p, spec=spec, analysis=analysis, material=material,
    )
    assert 'client.publish("sensor/temp", "21")' in r.final_prompt
    assert "수정·추가·재작성 금지" in r.final_prompt and "~~~python" in r.final_prompt
    assert r.code_slide_numbers_without_code == [] and r.validation.passed
    assert ctx.spec.slide_count  # helper context builds too


def test_a_code_slide_without_its_source_is_reported_not_invented(tmp_path):
    material, analysis = analyze_text(tmp_path, CODE_DOC)
    p = plan(analysis, code_level="snippet", lecture_type="practice")
    spec = SlidePlanner().plan(p, analysis)
    r = GensparkPromptBuilder().build(
        project_id="p" * 32, profile=profile(code_level="snippet", lecture_type="practice"), plan=p, spec=spec,
    )
    code_slides = [s.slide_number for s in spec.slides if s.slide_type == SlideType.code]
    assert code_slides and r.code_slide_numbers_without_code == code_slides
    assert "코드를 새로 만들지 않고" in r.final_prompt and 'client.publish("sensor/temp"' not in r.final_prompt
    assert any("원문 코드를 찾지 못해" in w for w in r.warnings)


def test_a_very_long_code_block_is_cut_and_the_cut_is_announced(tmp_path):
    body = "\n".join(f'client.publish("sensor/temp", "{i}")' for i in range(150))
    doc = CODE_DOC.replace('client.publish("sensor/temp", "21")', body)
    material, analysis = analyze_text(tmp_path, doc)
    opts = dict(code_level="snippet", lecture_type="practice", practice_level="guided")
    p = plan(analysis, **opts)
    spec = SlidePlanner().plan(p, analysis)
    r = GensparkPromptBuilder().build(
        project_id="p" * 32, profile=profile(**opts), plan=p, spec=spec, analysis=analysis, material=material,
    )
    assert f"150줄 중 앞의 {pb.MAX_CODE_LINES}줄만 표시" in r.final_prompt
    assert '"119"' in r.final_prompt and '"120"' not in r.final_prompt
    assert any("앞의 120줄만" in w for w in r.warnings)


def test_the_scope_notes_of_the_source_become_constraints():
    ctx = make_ctx("mqtt", **CASE_B)
    assert ctx.plan.source_constraints
    cons = section(build(ctx).final_prompt, "SOURCE MATERIAL CONSTRAINTS")
    for c in ctx.plan.source_constraints:
        assert " ".join(c.text.split()) in cons
    assert "충돌하는 내용은 화면과 노트에 넣지 않는다" in cons
    assert "원문 구성" in cons


def test_the_source_material_adds_file_information(tmp_path):
    material, analysis = analyze_text(tmp_path, CODE_DOC)
    p = plan(analysis)
    spec = SlidePlanner().plan(p, analysis)
    r = GensparkPromptBuilder().build(
        project_id="p" * 32, profile=profile(), plan=p, spec=spec, analysis=analysis, material=material,
    )
    cons = section(r.final_prompt, "SOURCE MATERIAL CONSTRAINTS")
    assert "원문 파일: doc.md" in cons and "원문 제목:" in cons


def test_without_any_source_the_prompt_still_builds(tmp_path):
    _, analysis = analyze_text(tmp_path, CODE_DOC)
    p = plan(analysis)
    spec = SlidePlanner().plan(p, analysis)
    r = GensparkPromptBuilder().build(project_id="p" * 32, profile=profile(), plan=p, spec=spec)
    assert r.validation.passed and "SOURCE MATERIAL CONSTRAINTS" in r.final_prompt


# ------------------------------------------------------------------ pieces and the safety net
def test_quiz_answers_stay_out_of_the_slide_when_notes_exist():
    q = QuizContent(question="Q?", choices=["a", "b"], answer="a", explanation="because")
    with_notes = "\n".join(GensparkPromptBuilder._quiz_lines(q, "concise"))
    assert "정답 — 발표자 노트에만 넣는다" in with_notes and "해설 — 발표자 노트에만" in with_notes
    without = "\n".join(GensparkPromptBuilder._quiz_lines(q, "none"))
    assert "화면 아래쪽 ‘정답’ 영역" in without and "1. a" in without


def test_a_prompt_that_does_not_match_the_specification_fails_validation():
    ctx = make_ctx("mqtt", **CASE_B)
    r = build(ctx)
    text = r.final_prompt
    sections = [(n, "") for n in SECTION_NAMES]
    v = GensparkPromptBuilder._validate(text.replace("## VISUAL REQUIREMENTS", "## VISUAL"), sections, ctx.spec)
    assert v.passed is False and v.all_sections_present is False
    v = GensparkPromptBuilder._validate(text.replace(f"정확히 {ctx.spec.slide_count}장", "정확히 1장"), [
        ("SLIDE SPECIFICATION", section(text, "SLIDE SPECIFICATION"))], ctx.spec)
    assert v.slide_count_matches is False
    cut = section(text, "SLIDE SPECIFICATION")
    cut = cut[: cut.index("### 슬라이드 5/")]
    v = GensparkPromptBuilder._validate(text, [("SLIDE SPECIFICATION", cut)], ctx.spec)
    assert v.all_slides_present_once_in_order is False and v.no_empty_slide_block is False and v.passed is False


def test_a_long_prompt_only_gets_a_hint_no_provider_limit_is_guessed(monkeypatch):
    monkeypatch.setattr(pb, "LONG_PROMPT_CHARS", 1000)
    r = build(make_ctx("mqtt", **CASE_B))
    assert any("프롬프트가 깁니다" in w and "입력 한도를 확인" in w for w in r.warnings)
    assert r.validation.passed  # nothing is cut or refused because of length


def test_the_builder_changes_no_input():
    ctx = make_ctx("mqtt", **CASE_B)
    e = run(ctx)
    before = (ctx.spec.model_dump_json(), ctx.plan.model_dump_json(), ctx.profile.model_dump_json(), e.model_dump_json())
    build(ctx, e)
    assert before == (ctx.spec.model_dump_json(), ctx.plan.model_dump_json(), ctx.profile.model_dump_json(), e.model_dump_json())


THIN_DOC = """# AIoT 입문

## AIoT란 무엇인가

AIoT는 Artificial Intelligence of Things의 약자로, 사물인터넷(IoT)에 인공지능(AI)을 결합한 체계를 말한다.
IoT가 센서와 장치를 연결해 데이터를 모은다면, AIoT는 그 데이터를 학습·추론에 활용해 자동으로 판단한다.
AIoT는 IoT를 대체하는 다른 물건이 아니라 IoT를 확장한 개념이다.
현장에 가까운 추론은 Edge AI가 맡고, 대량 학습과 장기 분석은 Cloud AI가 맡는 경우가 많다.
"""


def test_a_name_only_concept_slide_gets_source_sentences_in_the_prompt(tmp_path):
    """Genspark was drawing an empty box labelled 'AIoT' because the planner stored the name as the only bullet."""
    from plan_helpers import plan as make_plan, profile as make_profile

    material, analysis = analyze_text(tmp_path, THIN_DOC)
    opts = dict(
        audience_level="general", duration_minutes=20, difficulty="introductory",
        lecture_type="theory", explanation_depth="concise", source_policy="source_first",
    )
    lecture = make_plan(analysis, **opts)
    spec = SlidePlanner().plan(lecture, analysis)
    concept = next(
        s for s in spec.slides
        if s.slide_type == SlideType.concept and "AIoT" in (s.title, *s.concepts)
    )
    r = GensparkPromptBuilder().build(
        project_id="p" * 32, profile=make_profile(**opts), plan=lecture, spec=spec,
        analysis=analysis, material=material,
    )
    block = slide_blocks(r.final_prompt)[concept.slide_number - 1]
    assert "- 본문 항목:" in block
    bullets = [ln[4:].strip() for ln in block.splitlines() if ln.startswith("  - ")]
    assert any(len(b) > 20 for b in bullets)
    assert any("학습" in b or "확장" in b or "Artificial Intelligence" in b for b in bullets)
    assert "빈 상자" in section(r.final_prompt, "ROLE")
    assert "빈 상자" in section(r.final_prompt, "VISUAL REQUIREMENTS")
    assert "그림만" in section(r.final_prompt, "ROLE")
    assert "그림만" in section(r.final_prompt, "VISUAL REQUIREMENTS")
    assert "화면 텍스트:" in block
    assert "완전한 문장" in section(r.final_prompt, "VISUAL REQUIREMENTS")
    assert "대본" in section(r.final_prompt, "ROLE")


def test_prompt_body_comes_from_the_spoken_script(tmp_path):
    from app.services.lecture_script_builder import LectureScriptBuilder
    from plan_helpers import plan as make_plan, profile as make_profile

    material, analysis = analyze_text(tmp_path, THIN_DOC)
    opts = dict(
        audience_level="general", duration_minutes=20, difficulty="introductory",
        lecture_type="theory", explanation_depth="concise", source_policy="source_first",
    )
    lecture = make_plan(analysis, **opts)
    spec = SlidePlanner().plan(lecture, analysis)
    script = LectureScriptBuilder().build(
        project_id="p" * 32, plan=lecture, spec=spec, analysis=analysis, material=material,
    )
    definition = next(s for s in script.slides if s.slide_type == "definition")
    r = GensparkPromptBuilder().build(
        project_id="p" * 32, profile=make_profile(**opts), plan=lecture, spec=spec,
        analysis=analysis, material=material, script=script,
    )
    block = slide_blocks(r.final_prompt)[definition.slide_number - 1]
    assert "사물인터넷" in block or "Artificial Intelligence" in block
    assert "이 슬라이드 대본" in block or "대본에서 고른" in r.final_prompt
