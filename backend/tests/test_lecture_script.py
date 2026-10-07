"""Spoken lecture scripts: readable words, not planner directions."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.lecture_script_builder import (
    LectureScriptBuilder,
    SCRIPT_CHARS_PER_SECOND,
    _spoken_form,
    _usable_line,
)
from app.services.lecture_script_writer import LectureScriptWriter
from app.services.llm_client import LLMClient, LLMError
from app.services.slide_planner import SlidePlanner

from plan_helpers import analyze_text, plan
from test_stage4_api import new_project, planned, put_profile


class ScriptLLM(LLMClient):
    """Offline stand-in: turns the allowed facts into a spoken paragraph."""

    model = "script-fake"

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, *, task: str, system: str, user: str, timeout: float | None = None, **kwargs):
        self.calls.append({"task": task, "system": system, "user": user})
        facts: list[str] = []
        if "<facts>" in user:
            block = user.split("<facts>", 1)[1].split("</facts>", 1)[0]
            facts = [ln[2:].strip() for ln in block.splitlines() if ln.startswith("- ")]
        body = " ".join(f for f in facts if f and not f.startswith("(")) or "오늘은 강의를 시작합니다."
        target = 200
        for line in user.splitlines():
            if line.startswith("목표 글자 수:"):
                digits = "".join(ch for ch in line if ch.isdigit())
                if digits:
                    target = int(digits)
                break
        text = body
        while len(text) < target:
            text += " " + body
        return {"script": text[: max(target, 80)]}

THIN_DOC = """# AIoT 입문

이 자료는 AIoT 환경에서 MQTT가 왜 쓰이는지를 설명하기 위한 강의노트이다.
문장 속에 데이터가 있다. 온도를 이용하여 장치를 분석해야 한다.
위와 같은 일상 문장은 개념명이 아니다. 개념은 아래에서 따로 정의한다.

## AIoT란 무엇인가

AIoT는 Artificial Intelligence of Things의 약자로, 사물인터넷(IoT)에 인공지능(AI)을 결합한 체계를 말한다.
IoT가 센서와 장치를 연결해 데이터를 모은다면, AIoT는 그 데이터를 학습·추론에 활용해 자동으로 판단한다.
AIoT는 IoT를 대체하는 다른 물건이 아니라 IoT를 확장한 개념이다.
현장에 가까운 추론은 Edge AI가 맡고, 대량 학습과 장기 분석은 Cloud AI가 맡는 경우가 많다.
"""


def _opts(**extra):
    return dict(
        audience_level="general",
        duration_minutes=20,
        difficulty="introductory",
        lecture_type="theory",
        explanation_depth="concise",
        source_policy="source_first",
        **extra,
    )


def test_usable_line_drops_truncated_slide_fragments():
    assert _usable_line("MQTT는 제한된 네트워크에서 센서 값을 전달하기 위해 만들어진…") is None
    assert _usable_line("전달 한 번, 확인 없음 — 빠르지만 유실 가능") is None
    assert _usable_line("전달 한 번, 확인 없음 빠르지만입니다") is None
    assert _usable_line("그대로 끝나지 않고, 학습과 추론을 거쳐") is None
    assert _usable_line("MQTT는 Message Queuing Telemetry Transport의 약자로, 제한된 네트워크에서 센서 값을 전달하기 위해 만들어진 경량 메시징 프로토콜이다.")


def test_spoken_form_drops_clock_and_unreadable_marks():
    text = _spoken_form("오늘은 20분짜리 강의입니다. AIoT → MQTT → QoS. 발행/구독 모델을 쓴다.")
    assert "20분" not in text
    assert "분짜리" not in text
    for mark in ("—", "→", "/", "─", "·"):
        assert mark not in text
    assert "발행과 구독" in text
    assert "씁니다" in text


def test_composed_scripts_are_words_to_read_not_directions(tmp_path: Path):
    material, analysis = analyze_text(tmp_path, THIN_DOC)
    lecture = plan(analysis, **_opts())
    spec = SlidePlanner().plan(lecture, analysis)
    result = LectureScriptBuilder().build(
        project_id="p" * 32, plan=lecture, spec=spec, analysis=analysis, material=material,
    )
    assert result.slide_count == spec.slide_count == len(result.slides)
    title = result.slides[0]
    assert lecture.title in title.text and "살펴보겠습니다" in title.text
    assert "학습 동기를 만든다" not in title.text
    assert "분 동안" not in title.text

    spoken = " ".join(s.text for s in result.slides)
    assert "Artificial Intelligence of Things" in spoken
    assert "학습" in spoken or "확장" in spoken
    assert "(IoT)" not in spoken and "(AI)" not in spoken
    assert "사물인터넷" in spoken
    assert "역할과 동작 방식을 설명한다" not in spoken
    assert "정의 문장을 그대로 읽은 뒤" not in spoken

    definition = next(s for s in result.slides if s.slide_type == "definition")
    assert "정의를 함께 읽" in definition.text
    assert "Artificial Intelligence" in definition.text or "약자로" in definition.text
    concept = next(s for s in result.slides if s.slide_type == "concept")
    assert concept.text != "‘AIoT’의 역할과 동작 방식을 설명한다."
    for s in result.slides:
        assert "동안 차근차근" not in s.text
        assert "동안 살펴봅니다" not in s.text
        assert "동안 사례를" not in s.text
        assert "동안 줄을 따라" not in s.text
        assert "눈에 담아" not in s.text
        assert "되짚어 보세요" not in s.text
        assert "선행 개념:" not in s.text
        assert "연관 개념:" not in s.text
        assert "자료에는 이렇게 적혀" not in s.text
        assert "핵심을 다시 정리합니다" not in s.text
    summary = next(s for s in result.slides if s.slide_type == "summary")
    assert "문장 속에 데이터가 있다" not in summary.text
    assert "개념명이 아니다" not in summary.text
    assert "AIoT" in summary.text or "Artificial Intelligence" in summary.text


def test_script_length_matches_the_slide_time(tmp_path: Path):
    material, analysis = analyze_text(tmp_path, THIN_DOC)
    lecture = plan(analysis, **_opts())
    spec = SlidePlanner().plan(lecture, analysis)
    result = LectureScriptBuilder().build(
        project_id="p" * 32, plan=lecture, spec=spec, analysis=analysis, material=material,
    )
    for s in result.slides:
        share = 0.4 if s.slide_type == "practice" else 1.0
        target = max(80, int(s.estimated_seconds * SCRIPT_CHARS_PER_SECOND * share))
        n = len(s.text)
        if s.slide_type in ("title", "agenda"):
            assert n >= 60
            continue
        assert 0.70 * target <= n <= 1.30 * target, (s.title, s.estimated_seconds, n, target)
        assert abs(s.spoken_seconds - s.estimated_seconds) <= max(12, int(s.estimated_seconds * 0.35))
        assert "말한다" not in s.text
        assert "판단한다" not in s.text
        assert "설명할 수 있다." not in s.text
        assert "눈에 담아" not in s.text
        assert "선행 개념:" not in s.text
        assert "(" not in s.text and ")" not in s.text
        assert "같다." not in s.text
        assert "쉽다." not in s.text
        assert "쓰인다." not in s.text
        assert "분짜리" not in s.text
        for mark in ("—", "→", "─"):
            assert mark not in s.text


def test_scripts_are_deterministic(tmp_path: Path):
    material, analysis = analyze_text(tmp_path, THIN_DOC)
    lecture = plan(analysis, **_opts())
    spec = SlidePlanner().plan(lecture, analysis)
    a = LectureScriptBuilder().build(project_id="p" * 32, plan=lecture, spec=spec, analysis=analysis, material=material)
    b = LectureScriptBuilder().build(project_id="p" * 32, plan=lecture, spec=spec, analysis=analysis, material=material)
    assert [s.text for s in a.slides] == [s.text for s in b.slides]


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(Settings(data_dir=tmp_path, max_upload_mb=5, cors_origins=())), raise_server_exceptions=False)


def test_scripts_endpoint_returns_a_script_for_every_slide(client):
    pid = planned(client)
    assert client.post(f"/projects/{pid}/slides").status_code == 200
    r = client.get(f"/projects/{pid}/scripts")
    assert r.status_code == 200, r.text
    body = r.json()
    spec = client.get(f"/projects/{pid}/slides").json()
    assert body["slide_count"] == spec["slide_count"] == len(body["slides"])
    assert [s["slide_number"] for s in body["slides"]] == [s["slide_number"] for s in spec["slides"]]
    assert all(s["text"].strip() for s in body["slides"])
    assert "살펴보겠습니다" in body["slides"][0]["text"]


def test_scripts_need_slides(client):
    pid = new_project(client)
    put_profile(client, pid)
    assert client.get(f"/projects/{pid}/scripts").status_code == 409


def test_llm_writer_uses_model_words_and_skips_preamble(tmp_path: Path):
    material, analysis = analyze_text(tmp_path, THIN_DOC)
    lecture = plan(analysis, **_opts())
    spec = SlidePlanner().plan(lecture, analysis)
    llm = ScriptLLM()
    result = LectureScriptWriter().build(
        project_id="p" * 32, plan=lecture, spec=spec, analysis=analysis, material=material, llm=llm,
    )
    assert result.writer == "llm"
    assert llm.calls and all(s.source == "llm" for s in result.slides)
    summary = next(s for s in result.slides if s.slide_type == "summary")
    assert "문장 속에 데이터가 있다" not in summary.text
    assert "개념명이 아니다" not in summary.text
    assert "AIoT" in summary.text or "Artificial Intelligence" in summary.text
    for call in llm.calls:
        if "유형: summary" not in call["user"]:
            continue
        facts = call["user"].split("<facts>", 1)[1].split("</facts>", 1)[0]
        assert "문장 속에 데이터가 있다" not in facts
        assert "개념명이 아니다" not in facts


def test_llm_writer_falls_back_when_the_model_fails(tmp_path: Path):
    material, analysis = analyze_text(tmp_path, THIN_DOC)
    lecture = plan(analysis, **_opts())
    spec = SlidePlanner().plan(lecture, analysis)

    class Boom(LLMClient):
        model = "boom"

        def complete_json(self, *, task, system, user, timeout=None, **kwargs):
            raise LLMError("unavailable")

    result = LectureScriptWriter().build(
        project_id="p" * 32, plan=lecture, spec=spec, analysis=analysis, material=material, llm=Boom(),
    )
    assert result.writer == "composed"
    assert all(s.source == "composed" for s in result.slides)
    assert "살펴보겠습니다" in result.slides[0].text


def test_scripts_endpoint_uses_llm_and_caches(tmp_path: Path):
    llm = ScriptLLM()
    c = TestClient(
        create_app(Settings(data_dir=tmp_path, max_upload_mb=5, cors_origins=()), llm_client=llm),
        raise_server_exceptions=False,
    )
    pid = planned(c)
    assert c.post(f"/projects/{pid}/slides").status_code == 200
    first = c.get(f"/projects/{pid}/scripts")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["writer"] == "llm"
    assert all(s["source"] == "llm" for s in body["slides"])
    n = len(llm.calls)
    assert n >= body["slide_count"]
    again = c.get(f"/projects/{pid}/scripts")
    assert again.status_code == 200 and len(llm.calls) == n
    assert c.post(f"/projects/{pid}/slides").status_code == 200
    rebuilt = c.get(f"/projects/{pid}/scripts")
    assert rebuilt.status_code == 200
    assert len(llm.calls) > n
