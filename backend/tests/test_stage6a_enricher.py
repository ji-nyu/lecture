"""STAGE 6A: SlideContentEnricher (LLM writes content, the rule engine keeps the structure).

TEST 1-14 of the STAGE 6A specification are marked in the test names / comments.
No test here calls a real LLM or the network.
"""

import copy
import json
import socket

import pytest

from app.llm.base import SlideResult
from app.llm.json_client import JsonContentClient
from app.llm.mock import MockLLMClient
from app.llm.prompts import PROMPT_VERSION
from app.models.enriched_slide_spec import RunStatus, SlideEnrichmentStatus
from app.services.enrichment_cache import InMemoryCache, JsonFileCache
from app.services.llm_client import LLMClient, LLMRateLimit, LLMResponseError, LLMTimeout, LLMUnavailable
from app.services.slide_content_enricher import SlideContentEnricher, hash_profile, hash_spec
from app.services.source_context import build_requests, notes_budget
from app.services.text_utils import split_sentences, violates_scope

from enrich_helpers import CASE_A, CASE_B, CASE_C, ScriptedLLM, first_of, make_ctx, only, run, spec_copy
from fake_llm import KOREAN_DOC

OK = SlideEnrichmentStatus.enriched
FAILED = SlideEnrichmentStatus.enrichment_failed


def skeleton(x):
    return [
        (s.slide_number, s.section_id, s.slide_type.value, s.estimated_explanation_time, s.title)
        for s in x.slides
    ]


def by_number(res):
    return {s.slide_number: s for s in res.slides}


def all_text(slide) -> str:
    return json.dumps(slide.enriched.model_dump(mode="json"), ensure_ascii=False) if slide.enriched else ""


@pytest.fixture(scope="module")
def b():
    return make_ctx(**CASE_B)


@pytest.fixture(scope="module")
def b_result(b):
    return run(b)


# =========================================================== TEST 1
def test_1_22_slides_in_22_slides_out(b, b_result):
    assert b.spec.slide_count == len(b.spec.slides) == 22
    r = b_result
    assert r.slide_count == len(r.slides) == 22
    assert r.status == RunStatus.enriched
    assert all(s.status == OK and s.enriched is not None for s in r.slides)
    assert skeleton(r) == skeleton(b.spec)
    assert sum(s.estimated_explanation_time for s in r.slides) == b.spec.duration_minutes * 60
    v = r.validation
    for name in ("slide_count_same", "slide_number_same", "section_id_same", "slide_type_same", "duration_same",
                 "lecture_profile_same", "source_policy_valid", "no_structure_mutation", "required_field_valid",
                 "schema_valid"):
        assert getattr(v, name) is True, name
    assert v.passed and v.errors == []
    assert r.stats.enriched_count == 22 and r.stats.failed_count == 0


def test_the_original_content_stays_next_to_the_enriched_content(b, b_result):
    for orig, es in zip(b.spec.slides, b_result.slides):
        assert es.original.key_message == orig.key_message
        assert es.original.key_points == orig.key_points
        assert es.original.visual_instruction == orig.visual_instruction
        assert es.original.presenter_instruction == orig.presenter_instruction
        assert es.original.source_reference == orig.source_reference
        assert es.learning_purpose == orig.learning_purpose and es.title == orig.title


def test_every_content_field_has_a_provenance_and_slides_carry_only_what_their_type_needs(b_result):
    for s in b_result.slides:
        c = s.enriched
        for f, p in s.provenance.items():
            value = getattr(c, f)
            assert p.value == "suggested" or value not in (None, "", []), (s.slide_number, f)
        assert c.key_message and c.display_title
        if s.slide_type.value in ("title", "agenda", "summary", "quiz"):
            assert c.practice_instruction is None and c.code_explanation is None
        if s.slide_type.value != "quiz":
            assert c.quiz_content is None
        if s.slide_type.value != "practice":
            assert c.practice_instruction is None
    practice = first_of(b_result, "practice").enriched.practice_instruction
    assert practice.steps and practice.expected_result


def test_quiz_and_summary_slides_get_their_own_content_fields():
    ctx = make_ctx(KOREAN_DOC, source_policy="source_first")
    r = run(ctx)
    quiz = first_of(r, "quiz").enriched
    q = quiz.quiz_content
    assert q and q.question and q.answer and (not q.choices or q.answer in q.choices)
    summary = first_of(r, "summary").enriched
    assert summary.summary_message and summary.body_points
    assert r.status == RunStatus.enriched and r.validation.passed


def test_metadata_and_version_are_recorded(b, b_result):
    r = b_result
    assert r.metadata.audience_level == b.profile.audience_level.value
    assert r.metadata.lecture_type == "practice"
    assert all(s.metadata == r.metadata for s in r.slides)
    v = r.version
    assert v.enricher_version and v.prompt_version == PROMPT_VERSION
    assert v.model_provider == "mock" and v.model_name == "mock-content-writer-v1"
    assert v.generation_timestamp is not None
    assert len(v.source_hash) == len(v.lecture_profile_hash) == len(v.slide_spec_hash) == 64
    assert v.lecture_profile_hash == hash_profile(b.profile) and v.slide_spec_hash == hash_spec(b.spec)


def test_the_input_is_not_modified(b):
    before = b.spec.model_dump_json()
    plan_before = b.plan.model_dump_json()
    run(b)
    assert b.spec.model_dump_json() == before and b.plan.model_dump_json() == plan_before


def test_same_input_gives_the_same_content(b, b_result):
    again = run(b)
    assert [s.model_dump(exclude={"from_cache"}) for s in again.slides] == [s.model_dump(exclude={"from_cache"}) for s in b_result.slides]


# =========================================================== TEST 2-3 (structure is never changed)
def test_2_an_extra_23rd_slide_is_rejected(b):
    def add_slide(reqs, results):
        last = results[-1]
        return results + [SlideResult(23, output=dict(last.output, slide_number=23))] if reqs[-1].slide.slide_number == 22 else results

    r = run(b, ScriptedLLM(batch=add_slide))
    assert r.slide_count == len(r.slides) == 22 and [s.slide_number for s in r.slides] == list(range(1, 23))
    assert 23 not in by_number(r)
    bad = [s for s in r.slides if s.status == FAILED]
    assert [s.slide_number for s in bad] == [21, 22]  # the batch that contained the extra slide
    assert all(s.failure_reason == "structure_mutation" and s.enriched is None for s in bad)
    assert all(s.status == OK for s in r.slides if s.slide_number <= 20)
    assert r.status == RunStatus.enrichment_partial
    assert r.validation.passed  # the result itself is consistent: the structure is untouched
    assert any(x.reason == "structure_mutation" for x in r.validation.rejections)
    # the failed slides fall back to the rule-based content
    assert bad[0].original.key_message == b.spec.slides[20].key_message


def test_2_a_slide_returned_twice_or_missing_is_handled(b):
    def twice(reqs, results):
        return results + [results[0]]

    r = run(b, ScriptedLLM(batch=twice))
    assert all(s.failure_reason == "structure_mutation" for s in r.slides) and r.slide_count == 22

    def drop_one(reqs, results):
        return [x for x in results if x.slide_number != 3]

    r = run(b, ScriptedLLM(batch=drop_one))
    assert by_number(r)[3].failure_reason == "missing_in_response"
    assert sum(s.status == FAILED for s in r.slides) == 1 and r.status == RunStatus.enrichment_partial


def test_2_the_llm_cannot_add_slides_through_the_json_adapter(b):
    class Extra(LLMClient):
        model = "fake"

        def __init__(self):
            self.mock = MockLLMClient()

        def complete_json(self, *, task, system, user, timeout=None):
            payload = json.loads(user)
            reqs = build_requests(b.spec, b.plan, b.profile, b.analysis, PROMPT_VERSION)
            wanted = [s["slide"]["slide_number"] for s in payload["slides"]]
            outs = [self.mock.enrich_slide(r) for r in reqs if r.slide.slide_number in wanted]
            outs.append(dict(outs[-1], slide_number=23))
            return {"slides": outs}

    r = run(b, JsonContentClient(Extra()))
    assert len(r.slides) == 22 and all(s.failure_reason == "structure_mutation" for s in r.slides)


def test_3_a_changed_slide_number_is_rejected(b):
    def renumber(req, out):
        return dict(out, slide_number=req.slide.slide_number + 100) if req.slide.slide_number == 7 else out

    r = run(b, ScriptedLLM(patch=renumber))
    s7 = by_number(r)[7]
    assert s7.status == FAILED and s7.failure_reason == "slide_number_mismatch" and s7.slide_number == 7
    assert sum(s.status == FAILED for s in r.slides) == 1
    assert skeleton(r) == skeleton(b.spec)  # the slide number the LLM wanted was never adopted


@pytest.mark.parametrize("key,value", [
    ("section_id", "sec99"), ("slide_type", "quiz"), ("estimated_explanation_time", 999),
    ("new_slides", [{"slide_number": 99}]), ("slides", []), ("order", [3, 2, 1]), ("insert_after", 4), ("merge_with", 5),
])
def test_3_every_attempt_to_change_the_structure_is_rejected(b, key, value):
    def mutate(req, out):
        return dict(out, **{key: value}) if req.slide.slide_number == 5 else out

    r = run(b, ScriptedLLM(patch=mutate))
    s5 = by_number(r)[5]
    assert s5.status == FAILED and s5.failure_reason == "structure_mutation"
    assert skeleton(r) == skeleton(b.spec) and sum(s.status == FAILED for s in r.slides) == 1


# =========================================================== TEST 4-5 (source policy)
def test_4_source_only_removes_an_external_example():
    ctx = make_ctx(**{**CASE_B, "source_policy": "source_only", "example_level": "high"})

    def add_example(req, out):
        if "example" in req.allowed_fields:
            out["example"] = "예를 들어 Kafka 클러스터에서 초당 100만 건을 처리하는 회사가 있다."
            out["provenance"] = dict(out.get("provenance", {}), example="llm_example")
        return out

    r = run(ctx, ScriptedLLM(patch=add_example))
    assert r.status == RunStatus.enriched
    wanted = {q.slide.slide_number for q in build_requests(ctx.spec, ctx.plan, ctx.profile, ctx.analysis, "v") if "example" in q.allowed_fields}
    assert wanted
    for s in r.slides:
        assert "Kafka" not in all_text(s)
        assert s.provenance.get("example") is None or s.provenance["example"].value != "llm_example"
        if s.slide_number in wanted:  # the external example was asked for, refused, and left as a placeholder
            assert s.enriched.example is None or "Kafka" not in s.enriched.example
            assert s.provenance["example"].value in ("suggested", "source_grounded", "paraphrased_source")
            assert any("example" in w for w in s.grounding.warnings)


def test_4_the_offline_writer_obeys_source_only():
    ctx = make_ctx(**{**CASE_B, "source_policy": "source_only"})
    r = run(ctx)
    assert r.status == RunStatus.enriched and r.validation.passed
    used = {p.value for s in r.slides for p in s.provenance.values()}
    assert "llm_example" not in used
    assert all(s.enriched.analogy is None for s in r.slides)
    for s in r.slides:
        if s.enriched.example:
            assert s.provenance["example"].value in ("source_grounded", "paraphrased_source")


def test_5_source_first_allows_a_paraphrase_and_labels_it():
    ctx = make_ctx(KOREAN_DOC, source_policy="source_first", example_level="medium")
    text = "광합성은 식물이 빛에너지로 이산화탄소와 물을 이용해 포도당을 만드는 과정이다."

    def paraphrase(req, out):
        if req.slide.slide_type == "concept" and "광합성" in req.slide.concepts:
            out["key_message"] = text
            out["provenance"] = dict(out.get("provenance", {}), key_message="paraphrased_source")
        return out

    r = run(ctx, ScriptedLLM(patch=paraphrase))
    s = first_of(r, "concept", "광합성")
    assert s.status == OK and s.enriched.key_message == text
    assert s.provenance["key_message"].value in ("paraphrased_source", "source_grounded")
    assert s.grounding.grounded is True and s.grounding.source_references
    # and it may add plain explanations / simple examples, labelled as LLM content
    used = {p.value for x in r.slides for p in x.provenance.values()}
    assert used & {"llm_explanation", "llm_example"}


def test_a_lying_provenance_label_is_corrected(b):
    def lie(req, out):
        out["provenance"] = {**out.get("provenance", {}), "explanation": "source_grounded"}
        out["explanation"] = "이 개념은 실무에서 매우 흔히 쓰이며 대부분의 대규모 서비스가 채택하고 있습니다."
        return out

    r = run(b, ScriptedLLM(patch=lie))
    s = first_of(r, "concept", "MQTT")
    assert s.provenance["explanation"].value != "source_grounded"
    assert any("explanation" in w for w in s.grounding.warnings)


# =========================================================== TEST 6-8 (profile dependence, same structure)
@pytest.fixture(scope="module")
def abc(b):
    profiles = {"A": make_ctx(**CASE_A).profile, "B": b.profile, "C": make_ctx(**CASE_C).profile}
    return {k: run(b, profile_override=p) for k, p in profiles.items()}


def test_6_the_structure_is_identical_for_beginner_intermediate_and_professional(b, abc):
    assert skeleton(abc["A"]) == skeleton(abc["B"]) == skeleton(abc["C"]) == skeleton(b.spec)
    for r in abc.values():
        assert r.status == RunStatus.enriched and r.validation.passed and len(r.slides) == 22
    assert abc["A"].metadata.audience_level == "university_beginner"
    assert abc["C"].metadata.audience_level == "professional" and abc["C"].metadata.difficulty == "advanced"


def test_6_explanation_level_examples_notes_and_visuals_differ_for_the_same_concept_slide(abc):
    slides = {k: first_of(r, "concept", "MQTT") for k, r in abc.items()}
    a, bb, c = (slides[k].enriched for k in "ABC")
    for f in ("explanation", "presenter_notes", "visual_instruction", "display_title", "key_message"):
        assert len({getattr(a, f), getattr(bb, f), getattr(c, f)}) == 3, f
    assert a.analogy and not bb.analogy and not c.analogy  # only the beginner gets an analogy
    assert len(a.presenter_notes) > len(c.presenter_notes)  # the beginner gets the longer walk-through
    assert "trade-off" in c.key_message or "한계" in c.display_title  # the professional sees design limits
    assert a.body_points != bb.body_points != c.body_points
    # the source sentence is still the basis of every version
    assert all("Message Queuing Telemetry Transport" in x.key_message for x in (a, bb, c))


def test_6_over_the_whole_lecture_the_levels_differ(abc):
    def total(r, f):
        vals = [getattr(s.enriched, f) for s in r.slides if getattr(s.enriched, f)]
        return len(vals)

    assert total(abc["A"], "analogy") > 0 and total(abc["B"], "analogy") == 0 == total(abc["C"], "analogy")
    notes = {k: sum(len(s.enriched.presenter_notes or "") for s in r.slides) for k, r in abc.items()}
    assert notes["A"] > notes["B"] and notes["A"] > notes["C"]
    for f in ("example", "visual_instruction", "explanation"):
        texts = {k: [getattr(s.enriched, f) for s in r.slides] for k, r in abc.items()}
        assert texts["A"] != texts["B"] != texts["C"] and texts["A"] != texts["C"], f


def test_7_theory_and_practice_lectures_produce_different_content_forms(b):
    theory = run(b, profile_override=make_ctx(**{**CASE_B, "lecture_type": "theory"}).profile)
    practice = run(b, profile_override=make_ctx(**CASE_B).profile)
    assert skeleton(theory) == skeleton(practice)
    t, p = first_of(theory, "concept", "MQTT").enriched, first_of(practice, "concept", "MQTT").enriched
    assert t.body_points != p.body_points and t.explanation != p.explanation
    assert any("실습" in x for x in p.body_points + [p.explanation]) and not any("실습" in x for x in t.body_points)
    tp, pp = first_of(theory, "practice").enriched.practice_instruction, first_of(practice, "practice").enriched.practice_instruction
    assert tp.steps != pp.steps and len(pp.steps) > len(tp.steps)


def test_7_an_example_based_lecture_gets_examples_where_a_theory_lecture_does_not():
    theory = make_ctx(**{**CASE_B, "lecture_type": "theory", "example_level": "none"})
    example = make_ctx(**{**CASE_B, "lecture_type": "example_based", "example_level": "none"})
    n = lambda ctx: sum(1 for s in run(ctx).slides if s.enriched.example)
    assert n(example) > n(theory)


@pytest.mark.parametrize("mode", ["none", "concise", "full"])
def test_8_presenter_notes_follow_speaker_notes(mode):
    ctx = make_ctx(**{**CASE_B, "speaker_notes": mode})
    r = run(ctx)
    assert r.status == RunStatus.enriched
    if mode == "none":
        assert all(not s.enriched.presenter_notes and "presenter_notes" not in s.provenance for s in r.slides)
    elif mode == "concise":
        for s in r.slides:
            n = len(split_sentences(s.enriched.presenter_notes))
            assert 1 <= n <= 4, (s.slide_number, n)
    else:
        assert all(s.enriched.presenter_notes for s in r.slides)


def test_8_full_notes_are_much_longer_than_concise_notes_and_none_has_no_notes():
    total = {}
    for mode in ("none", "concise", "full"):
        r = run(make_ctx(**{**CASE_B, "speaker_notes": mode}))
        total[mode] = sum(len(s.enriched.presenter_notes or "") for s in r.slides)
    assert total["none"] == 0 < total["concise"] < total["full"]
    assert total["full"] > 1.5 * total["concise"]


# =========================================================== TEST 9-10 (fallback)
@pytest.mark.parametrize("exc,reason", [
    (LLMTimeout("timeout"), "timeout"),
    (LLMRateLimit("429"), "rate_limit"),
    (LLMUnavailable("down"), "provider_unavailable"),
    (LLMResponseError("bad"), "invalid_json"),
])
def test_9_an_llm_error_falls_back_to_the_original_slide(b, exc, reason):
    r = run(b, ScriptedLLM(error=only({4, 5}, exc)))
    assert [s.slide_number for s in r.slides if s.status == FAILED] == [4, 5]
    for n in (4, 5):
        s = by_number(r)[n]
        assert s.failure_reason == reason and s.failure_message and s.enriched is None
        assert s.original.key_message == b.spec.slides[n - 1].key_message
        assert s.slide_number == n and s.section_id == b.spec.slides[n - 1].section_id
    assert r.status == RunStatus.enrichment_partial and r.validation.passed
    assert skeleton(r) == skeleton(b.spec)
    assert r.stats.failed_count == 2 and r.stats.failures_by_reason == {reason: 2}
    assert r.warnings


def test_9_all_slides_time_out_and_the_project_can_still_continue(b):
    r = run(b, ScriptedLLM(error=lambda req: LLMTimeout("t")))
    assert r.status == RunStatus.enrichment_failed and r.slide_count == 22
    assert all(s.failure_reason == "timeout" for s in r.slides)
    assert r.validation.passed and skeleton(r) == skeleton(b.spec)


def test_9_no_llm_configured_is_a_fallback_not_an_error(b):
    r = SlideContentEnricher(None).enrich(b.spec, b.plan, b.profile, b.analysis, source_text=b.text)
    assert r.status == RunStatus.enrichment_failed and len(r.slides) == 22
    assert {s.failure_reason for s in r.slides} == {"not_configured"}
    assert r.version.model_provider == "none"


def test_9_an_unexpected_provider_error_is_contained_and_does_not_leak(b):
    r = run(b, ScriptedLLM(error=only({2}, RuntimeError("TESTSECRET-KEY connection string"))))
    s = by_number(r)[2]
    assert s.failure_reason == "unexpected"
    assert "TESTSECRET" not in r.model_dump_json() and "Traceback" not in r.model_dump_json()


def test_10_invalid_json_falls_back(b):
    for raw in ("this is not json", ["a", "list"], 42, {"slide_number": "seven"}, {"slide_number": 3, "body_points": "not-a-list"},
                {"slide_number": 3, "quiz_content": "wrong type"}, {"slide_number": 3, "provenance": "x"}):
        r = run(b, ScriptedLLM(raw=lambda req, raw=raw: raw if req.slide.slide_number == 3 else None))
        s = by_number(r)[3]
        assert s.status == FAILED and s.failure_reason in ("invalid_json", "invalid_schema"), raw
        assert sum(x.status == FAILED for x in r.slides) == 1
        assert r.validation.passed


def test_10_the_json_adapter_turns_bad_responses_into_fallbacks(b):
    class Broken(LLMClient):
        model = "fake"

        def __init__(self, mode):
            self.mode = mode

        def complete_json(self, *, task, system, user, timeout=None):
            if self.mode == "timeout":
                raise LLMTimeout("t")
            if self.mode == "no_slides":
                return {"answer": "hello"}
            if self.mode == "not_objects":
                return {"slides": ["x", "y"]}
            raise LLMResponseError("invalid")

    for mode, reason in (("timeout", "timeout"), ("no_slides", "invalid_json"), ("not_objects", "invalid_json"), ("invalid", "invalid_json")):
        r = run(b, JsonContentClient(Broken(mode)))
        assert {s.failure_reason for s in r.slides} == {reason}, mode
        assert r.status == RunStatus.enrichment_failed and r.validation.passed and len(r.slides) == 22


def test_a_slide_whose_only_content_gets_rejected_falls_back(b):
    def empty(req, out):
        if req.slide.slide_number == 6:
            return {"slide_number": 6, "key_message": "   ", "body_points": []}
        return out

    r = run(b, ScriptedLLM(patch=empty))
    assert by_number(r)[6].failure_reason == "required_field_missing"
    assert sum(s.status == FAILED for s in r.slides) == 1


def test_retry_asks_the_llm_only_for_the_failed_slides(b):
    bad = {3, 9, 17}
    llm = ScriptedLLM(error=lambda req: LLMTimeout("t") if req.slide.slide_number in bad else None)
    first = run(b, llm)
    assert {s.slide_number for s in first.slides if s.status == FAILED} == bad
    bad.clear()
    llm.slide_calls.clear()
    second = run(b, llm, previous=first)
    assert sorted(llm.slide_calls) == [3, 9, 17]
    assert second.status == RunStatus.enriched and second.stats.cache_hits == 19
    kept = by_number(first)
    for n in (1, 2, 4, 10):
        assert by_number(second)[n].enriched == kept[n].enriched
    assert skeleton(second) == skeleton(b.spec)


def test_retry_starts_over_when_the_inputs_changed(b):
    llm = ScriptedLLM(error=only({3}, LLMTimeout("t")))
    first = run(b, llm)
    other = make_ctx(**{**CASE_B, "speaker_notes": "none"})
    llm2 = ScriptedLLM()
    run(other, llm2, previous=first)
    assert len(llm2.slide_calls) == 22


# =========================================================== TEST 11 (mock, offline)
def test_11_the_mock_client_works_without_any_external_api(b, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    mock = MockLLMClient()
    r = run(b, mock)
    assert r.status == RunStatus.enriched and mock.call_count == 22
    assert r.version.model_provider == "mock"


def test_11_the_mock_is_deterministic():
    ctx = make_ctx(**CASE_C)
    req = build_requests(ctx.spec, ctx.plan, ctx.profile, ctx.analysis, PROMPT_VERSION)[8]
    assert MockLLMClient().enrich_slide(req) == MockLLMClient().enrich_slide(req)


# =========================================================== TEST 12 (scope notes)
def test_12_the_scope_note_reaches_the_llm_and_the_forbidden_content_does_not_come_back():
    ctx = make_ctx(KOREAN_DOC, source_policy="source_first", example_level="medium", speaker_notes="full")
    reqs = build_requests(ctx.spec, ctx.plan, ctx.profile, ctx.analysis, PROMPT_VERSION)
    assert all(any("캘빈 회로의 세부 화학식" in n for n in r.scope_notes) for r in reqs)
    forbidden = "캘빈 회로의 세부 화학식은 여러 단계의 효소 반응식으로 이루어진다."

    def leak(req, out):
        out["explanation"] = f"{out.get('explanation') or '광합성을 설명한다.'} {forbidden}"
        out["body_points"] = [*out.get("body_points", []), forbidden]
        out["presenter_notes"] = f"{forbidden} {out.get('presenter_notes') or ''}"
        return out

    r = run(ctx, ScriptedLLM(patch=leak))
    assert r.slide_count == len(ctx.spec.slides)
    for s in r.slides:
        assert "세부 화학식" not in all_text(s), s.slide_number
    assert any("다루지 않는다고" in w for s in r.slides for w in s.grounding.warnings)


def test_12_a_list_of_excluded_topics_in_the_real_document_is_respected(b):
    # 'QoS 2의 단계, TLS 핸드셰이크, Edge AI 배치, ... 은 이 장의 주제가 아니다.'
    bad = "TLS 핸드셰이크 과정에서 인증서와 키를 교환한다."
    assert any(violates_scope(bad, r.scope_notes) for r in build_requests(b.spec, b.plan, b.profile, b.analysis, "v")[:1])

    def leak(req, out):
        out["explanation"] = f"{out.get('explanation') or '설명한다.'} {bad}"
        return out

    r = run(b, ScriptedLLM(patch=leak))
    assert all("TLS 핸드셰이크" not in all_text(s) for s in r.slides)


def test_12_the_offline_writer_stays_inside_the_scope_of_the_document(b, b_result):
    notes = b_result and build_requests(b.spec, b.plan, b.profile, b.analysis, "v")[0].scope_notes
    assert notes
    for s in b_result.slides:
        for sentence in split_sentences(all_text(s).replace('"', " ").replace("\\n", " ")):
            assert not violates_scope(sentence, notes), (s.slide_number, sentence)


def test_only_the_relevant_source_context_is_sent_for_a_slide():
    ctx = make_ctx(KOREAN_DOC, source_policy="source_first")
    reqs = build_requests(ctx.spec, ctx.plan, ctx.profile, ctx.analysis, PROMPT_VERSION)
    calvin = next(r for r in reqs if r.slide.slide_type == "concept" and r.slide.concepts == ["암반응"])
    texts = " ".join(s.text for s in calvin.source_context)
    assert "암반응" in texts
    assert "산소가 발생한다" not in texts and "틸라코이드와 스트로마가 있다" not in texts
    assert calvin.slide.title and calvin.slide.key_message and calvin.slide.slide_type == "concept"
    assert calvin.profile.audience_level == ctx.profile.audience_level.value
    assert calvin.section.id == calvin.slide.section_id


def test_a_request_never_carries_the_whole_document(b):
    from app.services.source_context import MAX_CONTEXT_CHARS, MAX_SNIPPETS

    reqs = build_requests(b.spec, b.plan, b.profile, b.analysis, PROMPT_VERSION)
    assert len(reqs) == 22
    for r in reqs:
        assert len(r.source_context) <= MAX_SNIPPETS
        assert sum(len(s.text) for s in r.source_context) <= MAX_CONTEXT_CHARS
        assert sum(len(s.text) for s in r.source_context) < len(b.text)
    assert any(r.source_context for r in reqs) and reqs[0].source_context == []  # the title slide needs no source text


def test_the_json_adapter_prompt_carries_profile_policy_and_scope():
    ctx = make_ctx(KOREAN_DOC, source_policy="source_only")
    seen = {}

    class Spy(LLMClient):
        model = "spy"

        def complete_json(self, *, task, system, user, timeout=None):
            seen.update(task=task, system=system, user=user)
            raise LLMTimeout("stop")

    reqs = build_requests(ctx.spec, ctx.plan, ctx.profile, ctx.analysis, PROMPT_VERSION)
    try:
        JsonContentClient(Spy()).enrich_slide(reqs[3])
    except LLMTimeout:
        pass
    assert "source_only" in seen["system"] and "JSON" in seen["system"]
    assert "캘빈 회로의 세부 화학식" in seen["user"] and "university_intermediate" in seen["user"]
    assert "LLM_API_KEY" not in seen["system"] + seen["user"]


# =========================================================== TEST 13 (notes ~ time)
def test_13_the_notes_budget_grows_with_the_slide_time():
    for mode in ("concise", "full"):
        budgets = [notes_budget(mode, sec, "concept").max_chars for sec in (20, 60, 120, 240, 480)]
        assert budgets == sorted(budgets)
    full = [notes_budget("full", sec, "concept").max_chars for sec in (30, 120, 300)]
    assert full[0] < full[1] < full[2]
    assert notes_budget("none", 300, "concept").max_chars == 0
    assert notes_budget("full", 300, "practice").max_chars < notes_budget("full", 300, "concept").max_chars


def test_13_the_offline_writer_writes_more_for_longer_slides():
    ctx = make_ctx(**{**CASE_A, "speaker_notes": "full"})
    r = run(ctx)
    rows = sorted((s.estimated_explanation_time, len(s.enriched.presenter_notes)) for s in r.slides if s.slide_type.value == "concept")
    assert len({t for t, _ in rows}) > 1
    third = max(1, len(rows) // 3)
    short = sum(n for _, n in rows[:third]) / third
    long = sum(n for _, n in rows[-third:]) / third
    assert long > short
    for s in r.slides:
        budget = notes_budget("full", s.estimated_explanation_time, s.slide_type.value)
        assert len(s.enriched.presenter_notes) <= budget.max_chars


def test_13_over_long_notes_from_the_llm_are_cut_to_the_time():
    ctx = make_ctx(**{**CASE_B, "speaker_notes": "full"})
    long_notes = " ".join(f"{i}번째 안내 문장을 자세하게 말합니다." for i in range(1, 400))
    r = run(ctx, ScriptedLLM(patch=lambda req, out: dict(out, presenter_notes=long_notes)))
    for s in r.slides:
        n = len(s.enriched.presenter_notes)
        assert 0 < n <= notes_budget("full", s.estimated_explanation_time, s.slide_type.value).max_chars
    times = {s.estimated_explanation_time: len(s.enriched.presenter_notes) for s in r.slides}
    assert max(times) > min(times) and times[max(times)] > times[min(times)]


# =========================================================== TEST 14 (cache)
def test_14_identical_input_with_a_cache_does_not_call_the_llm_again(b):
    cache = InMemoryCache()
    first_llm = MockLLMClient()
    first = run(b, first_llm, cache=cache)
    assert first_llm.call_count == 22 and first.stats.llm_calls == 22 and first.stats.cache_hits == 0
    second_llm = MockLLMClient()
    second = run(b, second_llm, cache=cache)
    assert second_llm.call_count == 0
    assert second.stats.llm_calls == 0 and second.stats.cache_hits == 22
    assert all(s.from_cache for s in second.slides) and not any(s.from_cache for s in first.slides)
    assert [s.model_dump(exclude={"from_cache"}) for s in second.slides] == [s.model_dump(exclude={"from_cache"}) for s in first.slides]
    assert second.version.slide_spec_hash == first.version.slide_spec_hash


def test_14_the_cache_is_not_used_when_an_input_changes(b):
    cache = InMemoryCache()
    run(b, MockLLMClient(), cache=cache)

    llm = MockLLMClient()  # another profile option
    run(make_ctx(**{**CASE_B, "speaker_notes": "none"}), llm, cache=cache)
    assert llm.call_count == 22

    llm = MockLLMClient()  # another slide specification
    changed = spec_copy(b)
    changed.slides[4].key_message = "완전히 다른 핵심 메시지이다."
    run(b, llm, cache=cache, spec=changed)
    assert llm.call_count == 22

    llm = MockLLMClient()  # another source
    run(b, llm, cache=cache, source_hash="f" * 64)
    assert llm.call_count == 22

    llm = MockLLMClient()  # force
    run(b, llm, cache=cache, force=True)
    assert llm.call_count == 22

    llm = MockLLMClient()  # another prompt version / model
    SlideContentEnricher(llm, cache=cache, prompt_version="enrich-v999").enrich(b.spec, b.plan, b.profile, b.analysis, source_text=b.text)
    assert llm.call_count == 22
    other = ScriptedLLM()
    other.model = "another-model"
    run(b, other, cache=cache)
    assert len(other.slide_calls) == 22


def test_14_failed_slides_are_not_cached_so_a_second_run_only_asks_for_them(b):
    cache = InMemoryCache()
    run(b, ScriptedLLM(error=only({5, 6}, LLMTimeout("t"))), cache=cache)
    llm = ScriptedLLM()
    r = run(b, llm, cache=cache)
    assert sorted(llm.slide_calls) == [5, 6] and r.status == RunStatus.enriched and r.stats.cache_hits == 20


def test_14_the_file_cache_survives_a_restart(b, tmp_path):
    path = tmp_path / "cache.json"
    run(b, MockLLMClient(), cache=JsonFileCache(path))
    assert path.is_file()
    llm = MockLLMClient()
    r = run(b, llm, cache=JsonFileCache(path))
    assert llm.call_count == 0 and r.stats.cache_hits == 22
    path.write_text("{ this is broken", encoding="utf-8")  # a damaged cache file is just a miss
    llm = MockLLMClient()
    run(b, llm, cache=JsonFileCache(path))
    assert llm.call_count == 22


def test_a_cached_answer_is_validated_again(b):
    cache = InMemoryCache()
    run(b, ScriptedLLM(), cache=cache)
    for k in list(cache._data):  # poison every cached answer with a structure change
        cache._data[k] = dict(cache._data[k], new_slides=[{"x": 1}])
    llm = MockLLMClient()
    r = run(b, llm, cache=cache)
    assert llm.call_count == 22 and r.status == RunStatus.enriched


# =========================================================== batching
def test_slides_are_processed_in_batches_of_five(b):
    llm = ScriptedLLM()
    run(b, llm, batch_size=5)
    assert [len(c) for c in llm.batch_calls] == [5, 5, 5, 5, 2]
    assert sorted(n for c in llm.batch_calls for n in c) == list(range(1, 23))
    llm = ScriptedLLM()
    r = run(b, llm, batch_size=8)
    assert [len(c) for c in llm.batch_calls] == [8, 8, 6] and r.stats.batch_count == 3 and r.stats.batch_size == 8


def test_parallel_batches_keep_the_slide_order(b):
    slow_first = lambda n: 0.03 if n <= 5 else 0.0  # the first batch finishes last
    sequential = run(b, ScriptedLLM())
    parallel = run(b, ScriptedLLM(delay=slow_first), workers=4)
    assert [s.slide_number for s in parallel.slides] == list(range(1, 23))
    assert [s.model_dump() for s in parallel.slides] == [s.model_dump() for s in sequential.slides]


def test_batches_fail_independently(b):
    r = run(b, ScriptedLLM(error=only({7}, LLMTimeout("t"))), workers=3)
    assert [s.slide_number for s in r.slides if s.status == FAILED] == [7]


def test_hashes_are_stable_and_sensitive(b):
    assert hash_spec(b.spec) == hash_spec(spec_copy(b))
    other = spec_copy(b)
    other.generated_at = other.generated_at.replace(year=2001)
    assert hash_spec(other) == hash_spec(b.spec)  # time stamps do not count
    other.slides[0].title = "다른 제목"
    assert hash_spec(other) != hash_spec(b.spec)
    assert hash_profile(b.profile) != hash_profile(make_ctx(**CASE_A).profile)
    assert hash_profile(b.profile) == hash_profile(copy.deepcopy(b.profile))


def test_a_spec_that_does_not_belong_to_the_plan_is_refused(b):
    from app.errors import EnrichmentError

    other_plan = make_ctx(**CASE_A).plan
    with pytest.raises(EnrichmentError):
        SlideContentEnricher(MockLLMClient()).enrich(b.spec, other_plan, b.profile, b.analysis, source_text=b.text)


def test_the_code_slide_is_explained_from_the_source_code():
    from plan_helpers import CODE_DOC

    ctx = make_ctx(CODE_DOC, source_policy="source_first", code_level="snippet")
    r = run(ctx)
    code = first_of(r, "code")
    assert code.status == OK and code.enriched.code_explanation
    assert code.enriched.code_explanation.purpose or code.enriched.code_explanation.key_lines
    assert r.validation.passed
