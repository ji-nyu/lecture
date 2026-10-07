"""GroundingValidator (STAGE 6A): what may stay of an LLM answer, and how honestly it is labelled."""

import pytest

from app.llm.base import LLMSlideOutput
from app.llm.prompts import PROMPT_VERSION
from app.models.enriched_slide_spec import FieldProvenance as P
from app.services.grounding_validator import SYSTEM_TAG, GroundingValidator
from app.services.source_context import build_requests
from app.services.text_utils import violates_scope

from enrich_helpers import make_ctx
from fake_llm import KOREAN_DOC

DEF = "광합성은 식물이 빛에너지를 이용하여 이산화탄소와 물로 포도당을 만드는 과정이다."
_REQS: dict = {}


def request(ctx, slide_type, concept=None):
    key = id(ctx)
    if key not in _REQS:
        _REQS[key] = build_requests(ctx.spec, ctx.plan, ctx.profile, ctx.analysis, PROMPT_VERSION)
    for r in _REQS[key]:
        if r.slide.slide_type == slide_type and (concept is None or concept in r.slide.concepts):
            return r
    raise LookupError(slide_type)


def answer(req, provenance=None, **fields):
    return LLMSlideOutput(slide_number=req.slide.slide_number, provenance=provenance or {}, **fields)


def check(ctx, req, out, extra_source=""):
    return GroundingValidator(ctx.text + extra_source).validate(req, out)


@pytest.fixture(scope="module")
def first():  # source_first, medium examples, concise notes
    return make_ctx(KOREAN_DOC, source_policy="source_first", example_level="medium", speaker_notes="concise")


@pytest.fixture(scope="module")
def only():  # source_only
    return make_ctx(KOREAN_DOC, source_policy="source_only", example_level="medium", speaker_notes="concise")


@pytest.fixture(scope="module")
def full():
    return make_ctx(KOREAN_DOC, source_policy="source_first", speaker_notes="full")


@pytest.fixture(scope="module")
def silent():
    return make_ctx(KOREAN_DOC, source_policy="source_first", speaker_notes="none")


# ------------------------------------------------------------------ fields
def test_a_field_the_slide_does_not_need_is_dropped(first):
    req = request(first, "concept", "광합성")
    assert "practice_instruction" not in req.allowed_fields and "quiz_content" not in req.allowed_fields
    out = answer(
        req, {"key_message": P.paraphrased_source},
        key_message=DEF, quiz_content={"question": "무엇인가?", "answer": "광합성", "choices": ["광합성", "명반응"]},
    )
    v = check(first, req, out)
    assert v.content.quiz_content is None and "quiz_content" not in v.provenance
    assert any("quiz_content" in w for w in v.warnings)


def test_presenter_notes_are_removed_when_speaker_notes_is_none(silent):
    req = request(silent, "concept", "광합성")
    assert req.notes.mode == "none" and "presenter_notes" not in req.allowed_fields
    out = answer(req, {"key_message": P.source_grounded}, key_message=DEF, presenter_notes="이 슬라이드에서는 광합성을 설명합니다.")
    v = check(silent, req, out)
    assert not v.content.presenter_notes and "presenter_notes" not in v.provenance


def test_missing_key_message_fails_only_this_slide(first):
    req = request(first, "concept", "광합성")
    v = check(first, req, answer(req, body_points=["a"]))
    assert v.failure_reason == "required_field_missing"


def test_display_title_falls_back_to_the_planned_title(first):
    req = request(first, "concept", "광합성")
    v = check(first, req, answer(req, {"key_message": P.source_grounded}, key_message=DEF))
    assert v.content.display_title == req.slide.title


# ------------------------------------------------------------------ safety
def test_destructive_commands_are_removed_from_practice(first):
    req = request(first, "practice")
    out = answer(
        req, {"practice_instruction": P.llm_explanation, "key_message": P.llm_explanation},
        key_message="지시에 따라 직접 수행한다.",
        practice_instruction={"steps": ["`rm -rf /` 로 작업 폴더를 정리한다", "sudo 권한으로 파일을 삭제한다",
                                        "DROP TABLE users; 를 실행한다", "결과를 확인하고 기록한다"]},
    )
    v = check(first, req, out)
    steps = v.content.practice_instruction.steps
    assert steps == ["결과를 확인하고 기록한다"]
    assert any("위험" in w for w in v.warnings)


def test_the_environment_is_not_guessed(first):
    req = request(first, "practice")
    out = answer(
        req, {"practice_instruction": P.llm_explanation, "key_message": P.llm_explanation},
        key_message="지시에 따라 직접 수행한다.",
        practice_instruction={"steps": ["Ubuntu 22.04 터미널을 연다", "Docker 컨테이너를 실행한다",
                                        "Python 3.11 가상환경을 만든다", "자료의 설명대로 수행한다"]},
    )
    v = check(first, req, out)
    assert v.content.practice_instruction.steps == ["자료의 설명대로 수행한다"]
    # ... but an environment the SOURCE names may be used
    v = check(first, req, out, extra_source="\n실습 환경은 Ubuntu 22.04 이다.")
    assert any("Ubuntu" in s for s in v.content.practice_instruction.steps)


def test_a_version_the_source_never_mentions_is_removed(first):
    req = request(first, "practice")
    out = answer(
        req, {"practice_instruction": P.llm_explanation, "key_message": P.llm_explanation},
        key_message="지시에 따라 직접 수행한다.",
        practice_instruction={"steps": ["도구 버전 2.4.1 을 준비한다", "결과를 확인한다"]},
    )
    assert check(first, req, out).content.practice_instruction.steps == ["결과를 확인한다"]


def test_steps_that_change_the_system_are_marked(first):
    req = request(first, "practice")
    out = answer(
        req, {"practice_instruction": P.llm_explanation, "key_message": P.llm_explanation},
        key_message="지시에 따라 직접 수행한다.",
        practice_instruction={"steps": ["필요한 패키지를 설치한다", "결과를 확인한다"]},
    )
    steps = check(first, req, out).content.practice_instruction.steps
    assert steps[0].startswith(SYSTEM_TAG) and not steps[1].startswith(SYSTEM_TAG)


# ------------------------------------------------------------------ source_only
def test_source_only_removes_an_example_the_llm_made_up(only):
    req = request(only, "concept", "광합성")
    out = answer(
        req, {"key_message": P.source_grounded, "example": P.llm_example},
        key_message=DEF, example="예를 들어 토마토 온실에서 LED 조명을 켜면 광합성이 빨라진다.",
    )
    v = check(only, req, out)
    assert v.content.example is None
    assert v.provenance.get("example") == P.suggested
    assert v.content.key_message == DEF


def test_source_only_removes_an_analogy_and_a_new_term(only):
    req = request(only, "concept", "광합성")
    out = answer(
        req, {"key_message": P.source_grounded, "body_points": P.paraphrased_source, "analogy": P.llm_explanation},
        key_message=DEF,
        body_points=[DEF, "광합성은 ATP와 NADPH를 만들고 효율은 6% 정도이다."],
        analogy="공장의 조립 라인과 같다.",
    )
    v = check(only, req, out)
    assert v.content.analogy is None
    assert v.content.body_points == [DEF]  # the point with terms/numbers that are not in the source is gone


def test_source_only_requires_the_key_message_to_restate_the_source(only):
    req = request(only, "concept", "광합성")
    out = answer(req, {"key_message": P.llm_explanation}, key_message="식물은 정말 놀라운 생물이다.")
    assert check(only, req, out).failure_reason == "required_field_missing"


def test_source_only_command_must_exist_in_the_source():
    ctx = make_ctx(
        "# MQTT 실습\n\n## 실행\nBroker를 시작하는 명령은 `mosquitto -v` 이다.\nBroker는 메시지를 중계하는 서버이다.\n",
        source_policy="source_only", practice_level="guided",
    )
    req = request(ctx, "practice")
    out = answer(
        req, {"practice_instruction": P.source_grounded, "key_message": P.source_grounded},
        key_message="Broker는 메시지를 중계하는 서버이다.",
        practice_instruction={"steps": ["`mosquitto -v` 로 Broker를 시작한다", "`docker run eclipse-mosquitto` 를 실행한다"]},
    )
    steps = check(ctx, req, out).content.practice_instruction.steps
    assert [s for s in steps if "mosquitto -v" in s] and not [s for s in steps if "docker" in s]


# ------------------------------------------------------------------ source_first
def test_source_first_keeps_a_paraphrase_and_labels_it(first):
    req = request(first, "concept", "광합성")
    text = "광합성은 식물이 빛에너지로 이산화탄소와 물을 이용해 포도당을 만드는 과정이다."
    v = check(first, req, answer(req, {"key_message": P.paraphrased_source}, key_message=text))
    assert v.content.key_message == text
    assert v.provenance["key_message"] in (P.paraphrased_source, P.source_grounded)
    assert v.grounded is True


def test_source_first_allows_an_llm_example_with_an_honest_label(first):
    req = request(first, "concept", "광합성")
    out = answer(
        req, {"key_message": P.source_grounded, "example": P.llm_example},
        key_message=DEF, example="예를 들어 창가에 둔 화분의 잎이 햇빛을 받아 양분을 만드는 모습을 볼 수 있다.",
    )
    v = check(first, req, out)
    assert v.content.example and v.provenance["example"] == P.llm_example


def test_a_source_claim_is_downgraded_when_the_text_is_not_in_the_source(first):
    req = request(first, "concept", "광합성")
    out = answer(
        req, {"key_message": P.source_grounded, "explanation": P.source_grounded},
        key_message=DEF, explanation="식물은 해가 뜨면 잎을 활짝 펴서 하루 종일 부지런히 일한다고 알려져 있다.",
    )
    v = check(first, req, out)
    assert v.provenance["key_message"] == P.source_grounded
    assert v.provenance["explanation"] == P.llm_explanation
    assert any("explanation" in w for w in v.warnings)


def test_a_missing_label_becomes_llm_inference(first):
    req = request(first, "concept", "광합성")
    v = check(first, req, answer(req, {"key_message": P.source_grounded}, key_message=DEF, explanation="잎에서 일어나는 일이다."))
    assert v.provenance["explanation"] == P.llm_inference


def test_a_sentence_that_contradicts_the_source_is_removed(first):
    req = request(first, "concept", "광합성")
    wrong = "광합성은 식물이 빛에너지를 이용하여 이산화탄소와 물로 포도당을 만들지 않는 과정이다."
    v = check(first, req, answer(req, {"key_message": P.source_grounded, "body_points": P.paraphrased_source},
                                 key_message=DEF, body_points=[DEF, wrong]))
    assert v.content.body_points == [DEF]
    assert any("반대" in w for w in v.warnings)


def test_content_marked_suggested_is_left_empty(first):
    req = request(first, "concept", "광합성")
    out = answer(req, {"key_message": P.source_grounded, "example": P.suggested}, key_message=DEF, example="이건 무시된다.")
    v = check(first, req, out)
    assert v.content.example is None and v.provenance["example"] == P.suggested


def test_an_empty_core_field_becomes_a_suggested_placeholder(first):
    req = request(first, "quiz")
    v = check(first, req, answer(req, {"key_message": P.llm_explanation}, key_message="방금 배운 내용을 확인한다."))
    assert v.content.quiz_content is None
    assert v.provenance["quiz_content"] == P.suggested


# ------------------------------------------------------------------ scope
def test_scope_notes_are_enforced(first):
    req = request(first, "concept", "광합성")
    assert any("캘빈" in n for n in req.scope_notes)
    out = answer(
        req, {"key_message": P.source_grounded, "body_points": P.llm_explanation},
        key_message=DEF,
        body_points=[DEF, "캘빈 회로의 세부 화학식은 여러 단계의 반응식으로 이루어진다.", "광합성은 잎에서 일어난다."],
    )
    v = check(first, req, out)
    assert not any("화학식" in p for p in v.content.body_points)
    assert len(v.content.body_points) == 2
    assert any("다루지 않는다고" in w for w in v.warnings)


def test_a_topic_list_in_a_scope_note_is_understood():
    notes = [
        "QoS 2의 단계, TLS 핸드셰이크, Edge AI 배치, Retained Message의 저장 정책은 이 장의 주제가 아니다.",
        "TLS는 Broker의 메시지 중계 개념과 다른 계층의 이야기이므로, 중계 역할을 설명할 때 함께 섞지 않는다.",
    ]
    assert violates_scope("TLS 핸드셰이크 과정에서 인증서를 교환한다.", notes)
    assert violates_scope("Edge AI 배치 위치에 따라 지연이 달라진다.", notes)
    assert violates_scope("Retained Message의 저장 정책은 브로커마다 다르다.", notes)
    # the concepts themselves are still taught
    assert not violates_scope("QoS는 메시지 전달 보장 수준을 정한다.", notes)
    assert not violates_scope("Retained Message는 새 구독자에게 마지막 메시지를 전달한다.", notes)
    assert not violates_scope("Broker는 메시지를 중계하는 서버이다.", notes)


# ------------------------------------------------------------------ size
def test_body_points_and_explanation_are_cut_to_the_limits(first):
    req = request(first, "concept", "광합성")
    many = [f"광합성은 식물이 빛에너지를 이용하여 물질을 만드는 과정이다 {i}." for i in range(1, 10)]
    long_expl = " ".join(["광합성은 식물이 빛에너지를 이용하여 포도당을 만드는 과정이다."] * 30)
    v = check(first, req, answer(req, {"key_message": P.source_grounded, "body_points": P.paraphrased_source, "explanation": P.paraphrased_source},
                                 key_message=DEF, body_points=many, explanation=long_expl))
    assert len(v.content.body_points) == req.limits.body_points_max
    assert len(v.content.explanation) <= req.limits.explanation_chars * 1.3
    assert any("줄였" in w for w in v.warnings)


def test_concise_notes_are_cut_to_the_sentence_limit(first):
    req = request(first, "concept", "광합성")
    notes = " ".join(f"{i}번째 안내 문장입니다." for i in range(1, 9))
    v = check(first, req, answer(req, {"key_message": P.source_grounded, "presenter_notes": P.llm_explanation}, key_message=DEF, presenter_notes=notes))
    assert req.notes.mode == "concise" and v.content.presenter_notes.count(".") <= req.notes.max_sentences


def test_notes_may_not_be_longer_than_the_slide_time_allows(full):
    long_notes = " ".join(f"{i}번째 안내 문장을 자세하게 말합니다." for i in range(1, 200))
    lengths = {}
    for stype in ("concept", "practice", "quiz"):
        req = request(full, stype)
        v = check(full, req, answer(req, {"key_message": P.llm_explanation, "presenter_notes": P.llm_explanation},
                                    key_message="핵심을 말한다.", presenter_notes=long_notes))
        n = len(v.content.presenter_notes)
        assert 0 < n <= req.notes.max_chars
        lengths[stype] = (req.slide.estimated_explanation_time, n)
    ordered = sorted(lengths.values())
    assert ordered[0][1] <= ordered[-1][1]  # more time => at least as many characters allowed
    assert ordered[0][0] < ordered[-1][0] and ordered[0][1] < ordered[-1][1]


# ------------------------------------------------------------------ structured fields
def test_a_quiz_answer_must_be_one_of_the_choices(first):
    req = request(first, "quiz")
    bad = {"question": "광합성이 일어나는 곳은?", "choices": ["엽록체", "미토콘드리아"], "answer": "핵"}
    v = check(first, req, answer(req, {"key_message": P.llm_explanation, "quiz_content": P.llm_explanation},
                                 key_message="방금 배운 내용을 확인한다.", quiz_content=bad))
    assert v.content.quiz_content is None
    ok = dict(bad, answer="엽록체")
    v = check(first, req, answer(req, {"key_message": P.llm_explanation, "quiz_content": P.llm_explanation},
                                 key_message="방금 배운 내용을 확인한다.", quiz_content=ok))
    assert v.content.quiz_content.answer == "엽록체"
