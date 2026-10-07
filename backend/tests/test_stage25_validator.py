"""EvidenceValidator: a quote only counts if it is really in the source."""

import pytest

from app.services.evidence_validator import EvidenceValidator, normalize
from app.services.llm_extractor import defines

from fake_llm import KOREAN_DOC, parse_text

SENT = "엽록체는 광합성이 일어나는 세포 소기관이다."


@pytest.fixture()
def v(tmp_path):
    return EvidenceValidator(parse_text(tmp_path, KOREAN_DOC))


def test_exact_quote_is_found_with_location(v):
    ev = v.validate(SENT)
    assert ev is not None
    assert ev.match == "exact" and ev.match_score == 1.0
    assert ev.quote == SENT
    assert ev.line_start is not None and ev.line_start == ev.line_end
    assert ev.section_id


def test_whitespace_newline_quote_marks_and_markup_are_normalized(v):
    ev = v.validate("“엽록체는   광합성이\n일어나는 세포 소기관이다.”")
    assert ev is not None and ev.match == "normalized"
    # the stored quote is the SOURCE text, not the LLM's variant
    assert ev.quote == SENT
    ev2 = v.validate("- **명반응**은 틸라코이드 막에서 빛에너지를 화학에너지로 바꾸는 반응이다.")
    assert ev2 is not None and ev2.match == "normalized"


def test_unicode_normalization_nfkc(v):
    assert normalize("ＡＴＰ １２") == "ATP 12"
    # a full-width period in the LLM's quote still finds the source sentence
    ev = v.validate("엽록체는 광합성이 일어나는 세포 소기관이다．")
    assert ev is not None and ev.match == "normalized"


def test_trailing_ellipsis_from_the_llm_is_tolerated(v):
    ev = v.validate("명반응은 틸라코이드 막에서 빛에너지를 화학에너지로 바꾸는…")
    assert ev is not None and ev.match in ("exact", "normalized")


def test_fabricated_and_too_short_quotes_are_rejected(v):
    assert v.validate("미토콘드리아는 세포의 발전소 역할을 한다.") is None
    assert v.validate("") is None
    assert v.validate(None) is None  # type: ignore[arg-type]
    assert v.validate("명반응") is None  # shorter than the minimum quote length


def test_fuzzy_match_is_recorded_and_conservative(v, tmp_path):
    # one word slightly different -> fuzzy, with the method and score recorded
    ev = v.validate("엽록체는 광합성이 일어나는 세포의 소기관이다.")
    assert ev is not None
    assert ev.match == "fuzzy" and 0.92 <= ev.match_score < 1.0
    assert ev.quote == SENT  # source text, not the LLM's

    # a negation flip must never match, even though the text is 90%+ similar
    assert v.validate("엽록체는 광합성이 일어나지 않는 세포 소기관이다.") is None
    # changed number
    m2 = EvidenceValidator(
        parse_text(tmp_path, "# t\n\n## a\n\n브로커는 최대 3개의 연결을 동시에 허용한다고 설명한다.\n", "n.md")
    )
    assert m2.validate("브로커는 최대 3개의 연결을 동시에 허용한다고 설명한다.") is not None
    assert m2.validate("브로커는 최대 5개의 연결을 동시에 허용한다고 설명한다.") is None


def test_fuzzy_can_be_disabled(tmp_path):
    ev = EvidenceValidator(parse_text(tmp_path, KOREAN_DOC), allow_fuzzy=False)
    assert ev.validate("엽록체는 광합성이 일어나는 세포의 소기관이다.") is None


def test_sentence_of_returns_the_full_source_sentence(v):
    ev = v.validate("틸라코이드 막에서 빛에너지를 화학에너지로 바꾸는 반응이다.")
    assert ev is not None
    assert v.sentence_of(ev) == "명반응은 틸라코이드 막에서 빛에너지를 화학에너지로 바꾸는 반응이다."


def test_count_and_heading_lookup(v):
    assert v.count("명반응") >= 3
    assert v.in_heading("명반응") and not v.in_heading("포도당")
    assert v.contains("포도당") and not v.contains("미토콘드리아")


def test_defines_requires_the_term_to_be_the_subject():
    assert defines(SENT, "엽록체")
    assert not defines(SENT, "세포 소기관")  # the defined term is 엽록체, not 세포 소기관
    assert not defines("명반응에서 물이 분해되어 산소가 발생한다.", "명반응")
