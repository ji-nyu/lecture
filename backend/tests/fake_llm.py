"""Test doubles for the LLM. NO real LLM/network call is ever made in pytest."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.services.document_parser import DocumentParser
from app.services.llm_client import LLMClient

# A Korean-only lecture note: almost no Latin technical terms, so the
# rule-based analyzer has little to hold on to.
KOREAN_DOC = """# 광합성의 이해

## 광합성이란
광합성은 식물이 빛에너지를 이용하여 이산화탄소와 물로 포도당을 만드는 과정이다.
이 강의에서는 광합성의 전체 흐름을 다룬다.
이 경우 방법에 따라 내용이 달라질 수 있다.

## 엽록체의 구조
엽록체는 광합성이 일어나는 세포 소기관이다.
엽록체 안에는 틸라코이드와 스트로마가 있다.
엽록체의 틸라코이드 막에서 빛을 흡수한다.

## 명반응
명반응은 틸라코이드 막에서 빛에너지를 화학에너지로 바꾸는 반응이다.
명반응에서 물이 분해되어 산소가 발생한다.
명반응의 결과로 화학에너지가 저장된다.

## 암반응
암반응은 스트로마에서 이산화탄소를 고정하여 포도당을 합성하는 반응이다.
암반응은 명반응에서 만들어진 화학에너지를 사용한다.

## 학습 범위
캘빈 회로의 세부 화학식은 이 강의에서 다루지 않는다.
세포 호흡은 이후에 설명한다.
중학교 과학 내용을 선수 지식으로 가정한다.
"""


# Narrative Korean: no "X는 … 이다" definitions, generic headings, so the
# heuristics have nothing to anchor on. The terms are only recognisable semantically.
KOREAN_NARRATIVE = """# 식물의 에너지

## 개요
잎의 초록색 부분에서는 광합성 과정이 진행된다.
광합성 덕분에 식물은 스스로 양분을 얻는다.

## 첫째 이야기
엽록체 내부의 틸라코이드 막이 빛을 받아들인다.
엽록체가 많은 잎일수록 초록색이 짙다.

## 둘째 이야기
명반응 단계에서 물이 분해되고 산소가 나온다.
명반응 이후에는 암반응 단계가 이어진다.

## 마무리
암반응 단계에서는 포도당이 만들어진다.
세포 호흡은 이후에 설명한다.
"""


def parse_text(tmp_path: Path, text: str, name: str = "doc.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return DocumentParser().parse(p, name, source_id="a" * 32)


class FakeLLM(LLMClient):
    """Returns canned JSON per task ("extract" / "topics"); can also raise."""

    model = "fake-model"

    def __init__(self, extract: dict | None = None, topics: dict | None = None, error: Exception | None = None):
        self.responses = {"extract": extract or {}, "topics": topics or {"topics": []}}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, *, task: str, system: str, user: str, timeout: float | None = None):
        self.calls.append({"task": task, "system": system, "user": user})
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.responses[task])


# What a well-behaved LLM would answer for KOREAN_DOC (every quote is verbatim).
GOOD_EXTRACT = {
    "concepts": [
        {"name": "광합성", "aliases": [], "category": "definition",
         "quote": "광합성은 식물이 빛에너지를 이용하여 이산화탄소와 물로 포도당을 만드는 과정이다."},
        {"name": "엽록체", "aliases": [], "category": "architecture",
         "quote": "엽록체는 광합성이 일어나는 세포 소기관이다."},
        {"name": "명반응", "aliases": [], "category": "process",
         "quote": "명반응은 틸라코이드 막에서 빛에너지를 화학에너지로 바꾸는 반응이다."},
        {"name": "암반응", "aliases": [], "category": "process",
         "quote": "암반응은 스트로마에서 이산화탄소를 고정하여 포도당을 합성하는 반응이다."},
    ],
    "definitions": [
        {"term": "명반응",
         "quote": "명반응은 틸라코이드 막에서 빛에너지를 화학에너지로 바꾸는 반응이다."},
    ],
    "prerequisites": [
        {"concept": "암반응", "requires": "명반응",
         "quote": "암반응은 명반응에서 만들어진 화학에너지를 사용한다."},
        {"concept": "명반응", "requires": "엽록체", "quote": None},
    ],
    "scope_notes": [
        {"quote": "캘빈 회로의 세부 화학식은 이 강의에서 다루지 않는다."},
        {"quote": "중학교 과학 내용을 선수 지식으로 가정한다."},
    ],
}
