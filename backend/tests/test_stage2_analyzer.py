"""LectureAnalyzer tests, mainly against the MQTT lecture note in /testdocument."""

from pathlib import Path

import pytest

from app.services.document_parser import DocumentParser
from app.services.lecture_analyzer import LectureAnalyzer, is_definitional

DOC_DIR = Path(__file__).resolve().parents[2] / "testdocument"
MQTT_DOCS = sorted(DOC_DIR.glob("*.txt")) if DOC_DIR.exists() else []


def analyze_text(tmp_path, text: str):
    f = tmp_path / "doc.md"
    f.write_text(text, encoding="utf-8")
    m = DocumentParser().parse(f, f.name, "t")
    return m, LectureAnalyzer().analyze(m)


@pytest.fixture(scope="module")
def mqtt():
    if not MQTT_DOCS:
        pytest.skip("testdocument/*.txt not present")
    f = MQTT_DOCS[0]
    m = DocumentParser().parse(f, f.name, "mqtt")
    return m, LectureAnalyzer().analyze(m)


# --------------------------------------------------------- MQTT document
def test_main_concepts_found(mqtt):
    _, a = mqtt
    names = {c.name for c in a.concepts}
    expected = {
        "MQTT", "AIoT", "IoT", "Publisher", "Subscriber", "Broker", "Topic", "Client",
        "QoS", "Retained Message", "Last Will and Testament", "Keep Alive", "TLS",
        "HTTP", "Edge AI", "Cloud AI", "Wildcard", "Publish/Subscribe",
    }
    assert expected <= names, expected - names


def test_everyday_words_are_not_concepts(mqtt):
    """The source explicitly warns: '데이터, 온도, 장치, 분석, 메시지' are not concept names."""
    _, a = mqtt
    names = {c.name for c in a.concepts}
    for noise in ("데이터", "온도", "장치", "분석", "메시지", "문장", "경우", "이것", "점"):
        assert noise not in names
    # nothing that looks like a sentence fragment / number list
    assert all(len(n) <= 30 and "," not in n for n in names)


def test_abbreviations_are_merged_as_aliases(mqtt):
    _, a = mqtt
    by = {c.name: c for c in a.concepts}
    assert "LWT" in by["Last Will and Testament"].aliases
    assert "Artificial Intelligence of Things" in by["AIoT"].aliases
    assert "MQTT Client" in by["Client"].aliases
    assert "LWT" not in by and "MQTT Client" not in by
    assert by["Publish/Subscribe"].mention_count >= 2


def test_definitions_are_verbatim_source_sentences(mqtt):
    m, a = mqtt
    raw = " ".join(m.raw_text.split())
    defs = {d.term: d for d in a.definitions}
    assert defs["Topic"].definition.startswith("Topic은 메시지를 분류하는 이름이다")
    assert defs["Retained Message"].definition.endswith("기능이다.")
    assert defs["Keep Alive"].definition.endswith("주기이다.")
    for d in a.definitions:
        assert d.definition.rstrip("…") in raw, d.term  # extracted, never invented
        assert d.source_location.section_id and d.source_location.line


def test_definition_of_qos_is_not_the_contrast_sentence(mqtt):
    _, a = mqtt
    qos = next(c for c in a.concepts if c.name == "QoS")
    assert "优劣" not in qos.description and "아니라" not in qos.description


def test_every_concept_has_location_description_and_valid_prereqs(mqtt):
    m, a = mqtt
    ids = {s.id for s in m.sections}
    names = {c.name for c in a.concepts}
    for c in a.concepts:
        assert c.description
        assert c.source_location.section_id in ids
        assert 1 <= c.importance <= 5
        assert set(c.prerequisite) <= names and c.name not in c.prerequisite
    # main subjects of the title are top importance
    imp = {c.name: c.importance for c in a.concepts}
    assert imp["MQTT"] == 5 and imp["AIoT"] == 5
    # component list "핵심 구성 요소는 Publisher, Subscriber, Broker, Topic"
    cat = {c.name: c.category.value for c in a.concepts}
    for n in ("Publisher", "Subscriber", "Broker", "Topic"):
        assert cat[n] == "architecture"
    assert cat["MQTT"] == "definition"


def test_prerequisite_direction(mqtt):
    _, a = mqtt
    by = {c.name: c for c in a.concepts}
    # Retained Message's description names Broker and Topic, introduced earlier
    assert {"Broker", "Topic"} <= set(by["Retained Message"].prerequisite)


def test_main_topics_cover_content_sections_without_overlap(mqtt):
    m, a = mqtt
    assert 3 <= len(a.main_topics) <= 8
    ids = [sid for t in a.main_topics for sid in t.section_ids]
    assert len(ids) == len(set(ids))
    content = {s.id for s in m.sections if s.level > 1}
    assert content <= set(ids)
    assert all(t.title and t.key_concepts for t in a.main_topics)
    # topics follow the document order
    assert ids == sorted(ids)
    # topic titles are not all the same
    assert len({t.title for t in a.main_topics}) == len(a.main_topics)


def test_scope_notes_capture_teaching_constraints(mqtt):
    _, a = mqtt
    texts = " ".join(n.text for n in a.scope_notes)
    assert "이 장의 주제가 아니다" in texts
    assert "함께 섞지 않는다" in texts
    assert any(p.text.startswith("MQTT의 핵심 구성 요소") for p in a.important_points)


def test_examples_extracted_with_locations(mqtt):
    _, a = mqtt
    joined = " ".join(e.text for e in a.examples)
    assert "factory/line1/robot3/temperature" in joined
    assert "home/livingroom/light/state" in joined
    assert any(e.kind == "case_study" and "스마트 팩토리" in e.text for e in a.examples)
    assert all(e.source_location.section_id for e in a.examples)


def test_structure_summary_hierarchy_complexity(mqtt):
    m, a = mqtt
    assert a.title == "AIoT와 MQTT 통신 기초"
    assert a.summary.startswith("이 자료는 AIoT 환경에서 MQTT")
    assert len(a.sections) == len(m.sections) == 29
    assert len(a.section_hierarchy) == 1 and len(a.section_hierarchy[0].children) == 28
    assert a.section_hierarchy[0].children[0].title == "AIoT란 무엇인가"  # ordinal stripped
    assert a.estimated_complexity.level.value in ("introductory", "beginner", "intermediate")
    assert 0 <= a.estimated_complexity.score <= 1
    assert a.code_examples == [] and a.formulas == []
    assert a.analyzer == "heuristic-v1"


def test_analysis_is_deterministic(mqtt):
    m, a = mqtt
    b = LectureAnalyzer().analyze(m)
    assert [c.model_dump() for c in a.concepts] == [c.model_dump() for c in b.concepts]
    assert [t.model_dump() for t in a.main_topics] == [t.model_dump() for t in b.main_topics]


# ------------------------------------------------------------ small units
@pytest.mark.parametrize(
    "sentence,expected",
    [
        ("Topic은 메시지를 분류하는 이름이다.", True),
        ("MQTT는 Message Queuing Telemetry Transport의 약자로, 프로토콜이다.", True),
        ("Last Will and Testament는 유언 메시지다.", True),
        ("Publisher는 Broker에게 메시지를 전송한다.", False),
        ("Topic은 IP 주소가 아니다.", False),
        ("QoS 0, 1, 2는 优劣이 아니라 상황에 따른 선택이다.", False),
        ("그래서 구성이 필요하다.", False),
    ],
)
def test_is_definitional(sentence, expected):
    assert is_definitional(sentence) is expected


# ---------------------------------------------------- other document kinds
def test_other_topic_and_code_formula_are_reported(tmp_path):
    text = (
        "# 정렬 알고리즘\n\n이 자료는 정렬을 설명한다.\n\n"
        "## 1. Bubble Sort란 무엇인가\n\n"
        "Bubble Sort는 인접한 두 원소를 비교해 교환하는 정렬 방식이다.\n"
        "Bubble Sort의 시간 복잡도는 $O(n^2)$ 이다.\n\n"
        "```python\ndef bubble(a):\n    pass\n```\n\n"
        "## 2. Quick Sort란 무엇인가\n\n"
        "Quick Sort는 피벗을 기준으로 분할하는 정렬 방식이다.\n"
        "예: [3, 1, 2] 를 정렬한다.\n\n"
        "## 3. 비교\n\nBubble Sort와 Quick Sort는 시간 복잡도가 다르다.\n"
    )
    m, a = analyze_text(tmp_path, text)
    names = {c.name for c in a.concepts}
    assert {"Bubble Sort", "Quick Sort"} <= names
    assert a.code_examples and "def bubble" in a.code_examples[0].code
    assert a.formulas and a.formulas[0].expression == "O(n^2)"
    assert {d.term for d in a.definitions} >= {"Bubble Sort", "Quick Sort"}
    assert any(e.text.startswith("예:") for e in a.examples)


def test_korean_only_document_falls_back_to_heading_concepts_with_warning(tmp_path):
    text = (
        "# 광합성\n\n## 1. 광합성이란 무엇인가\n\n식물이 빛 에너지를 화학 에너지로 바꾸는 과정이다.\n\n"
        "## 2. 엽록체의 역할\n\n엽록체는 광합성이 일어나는 장소이다.\n\n"
        "## 3. 명반응의 특징\n\n명반응은 빛이 필요하다.\n"
    )
    _, a = analyze_text(tmp_path, text)
    assert a.warnings, "fallback must be reported"
    names = {c.name for c in a.concepts}
    assert {"광합성", "엽록체", "명반응"} & names
