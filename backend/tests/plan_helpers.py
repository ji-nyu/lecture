"""Shared helpers for the STAGE 3 tests."""

from __future__ import annotations

import json
from pathlib import Path

from app.models.lecture_profile import LectureOptionsInput
from app.services.document_parser import DocumentParser
from app.services.lecture_analyzer import HeuristicAnalyzer
from app.services.lecture_planner import LecturePlanner
from app.services.lecture_profile_service import build_profile

DOC_DIR = Path(__file__).resolve().parents[2] / "testdocument"
MQTT_DOCS = sorted(DOC_DIR.glob("*.txt")) if DOC_DIR.exists() else []

BASE = dict(
    audience_level="university_intermediate",
    duration_minutes=60,
    difficulty="intermediate",
    lecture_type="mixed",
    explanation_depth="standard",
    source_policy="source_first",
)
CASE_A = dict(  # spec "Profile A" / CASE A
    audience_level="university_beginner", duration_minutes=60, difficulty="introductory",
    lecture_type="theory", explanation_depth="detailed", source_policy="source_first",
)
CASE_B = dict(  # spec "Profile B" / CASE B
    audience_level="university_intermediate", duration_minutes=60, difficulty="intermediate",
    lecture_type="practice", explanation_depth="standard", source_policy="source_first",
)
CASE_C = dict(  # spec CASE C
    audience_level="professional", duration_minutes=30, difficulty="advanced",
    lecture_type="theory", explanation_depth="concise", source_policy="source_first",
)

# Small Markdown lecture WITH a code block and inline examples.
CODE_DOC = """# MQTT 실습 입문

## Broker란 무엇인가
Broker는 메시지를 중계하는 서버이다.
Broker는 Client 연결을 관리한다.
예: Broker에 Client가 접속한다.

## Publish 실습
Publish는 Topic으로 메시지를 보내는 동작이다.
Topic은 메시지를 분류하는 이름이다.
Publish와 Topic은 함께 쓰인다.

```python
client.publish("sensor/temp", "21")
```

## Subscribe 실습
Subscribe는 Topic의 메시지를 받겠다고 등록하는 동작이다.
Subscribe와 Broker는 함께 쓰인다.
Client는 Subscribe 후 메시지를 받는다.
"""


def parse(tmp_path: Path, text: str, name: str = "doc.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return DocumentParser().parse(p, name, source_id="a" * 32)


def analyze_text(tmp_path: Path, text: str, name: str = "doc.md"):
    m = parse(tmp_path, text, name)
    return m, HeuristicAnalyzer().analyze(m)


def analyze_mqtt(tmp_path: Path):
    doc = MQTT_DOCS[0]
    return analyze_text(tmp_path, doc.read_text(encoding="utf-8"), "mqtt.txt")


def profile(**opts):
    return build_profile("p" * 32, LectureOptionsInput(**{**BASE, **opts}))


def plan(analysis, **opts):
    return LecturePlanner().plan(analysis, profile(**opts))


def comparable(p) -> str:
    """The plan without identity/time-stamp fields (for equality / difference checks)."""
    d = p.model_dump(mode="json")
    for k in ("id", "profile_id", "generated_at"):
        d.pop(k)
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def slides(analysis, **opts):
    """(LecturePlan, SlideSpecification) for the options."""
    from app.services.slide_planner import SlidePlanner

    p = plan(analysis, **opts)
    return p, SlidePlanner().plan(p, analysis)


def spec_comparable(s) -> str:
    d = s.model_dump(mode="json")
    for k in ("lecture_id", "generated_at"):
        d.pop(k)
    return json.dumps(d, ensure_ascii=False, sort_keys=True)
