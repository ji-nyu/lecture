"""Small, dependency-free text helpers shared by the enrichment stage.

Everything here is a HEURISTIC over surface text (terms, numbers, character overlap).
The GroundingValidator built on top of it can reliably catch new technical terms, numbers,
commands, out-of-scope topics and provenance claims that the source does not support; it
cannot judge whether a fluent Korean sentence is factually new. The README says so.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

_WS = re.compile(r"\s+")
_QUOTES = re.compile(r"[‘’“”'\"`]")
_SENT_SPLIT = re.compile(r"(?<=[.!?。…])\s+|\n+")

LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
HANGUL_RE = re.compile(r"[가-힣]+")
# Numbers that state a fact: decimals / versions (3.1.1), 2+ digit numbers, a digit + unit.
NUMBER_FACT_RE = re.compile(
    r"\d+(?:\.\d+)+|\d{2,}|\d(?=\s?(?:%|ms|초|분|시간|MB|KB|GB|Hz|V|배|바이트|byte|개월|년))"
)

_PARTICLES = (
    "에서", "으로", "이라", "에게", "까지", "부터", "처럼", "보다",
    "은", "는", "이", "가", "을", "를", "의", "에", "도", "와", "과", "로", "만",
)
_STOP_WORDS = {
    "다루지", "않", "이후", "후속", "강의", "수업", "세부", "자세", "내용", "별도", "범위", "생략",
    "제외", "설명", "언급", "참고", "다음", "시간", "이번", "다만", "또한", "경우", "대한", "관련",
    "위한", "통해", "때문", "선수", "지식", "가정", "주제", "섞지", "포함", "이야기", "다른",
}
_STOP_ENDINGS = ("한다", "된다", "는다", "지만", "합니다", "됩니다", "습니다", "하지", "되지", "이다", "에서는", "아니다")

_EXCLUSION_CUES = (
    "다루지", "생략", "제외", "범위 밖", "범위를 벗어", "이후에 설명", "이후 설명", "후속",
    "다음 강의", "다음 시간", "참고만", "언급만", "설명하지", "주제가 아니", "범위가 아니",
    "포함하지 않",
)


def normalize(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def norm_key(text: str) -> str:
    """Lower-case, quote-free, whitespace-free form for containment tests."""
    return _WS.sub("", _QUOTES.sub("", (text or "").lower()))


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s and s.strip()]


def bigrams(text: str) -> set[str]:
    chars = [c for c in norm_key(text) if c.isalnum()]
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def coverage(text: str, reference: set[str]) -> float:
    """Share of the text's character pairs that also occur in the reference."""
    grams = bigrams(text)
    if not grams:
        return 1.0
    return len(grams & reference) / len(grams)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def latin_tokens(text: str) -> set[str]:
    return {t.lower() for t in LATIN_RE.findall(text or "") if len(t) >= 2}


def fact_numbers(text: str) -> set[str]:
    return set(NUMBER_FACT_RE.findall(text or ""))


def known_latin(token: str, vocab: set[str]) -> bool:
    """`publishing` counts as known when the source has `publish` (shared stem of 4+ chars)."""
    if token in vocab:
        return True
    if len(token) < 4:
        return False
    return any(len(v) >= 4 and (token.startswith(v) or v.startswith(token)) for v in vocab)


def ko_stem(word: str) -> str:
    for p in _PARTICLES:
        if word.endswith(p) and len(word) - len(p) >= 2:
            return word[: -len(p)]
    return word


def keywords(text: str) -> set[str]:
    """Content words of a sentence: Latin words (lower-case) and particle-stripped Hangul words."""
    words: set[str] = set(t.lower() for t in LATIN_RE.findall(text or "") if len(t) >= 2)
    for w in HANGUL_RE.findall(text or ""):
        if any(w.endswith(e) for e in _STOP_ENDINGS):
            continue
        s = ko_stem(w)
        if len(s) >= 2 and s not in _STOP_WORDS:
            words.add(s)
    return words


# ---- scope notes ("this lecture does not cover ...") ------------------------
def is_exclusion_note(note: str) -> bool:
    return any(c in note for c in _EXCLUSION_CUES)


def scope_clauses(note: str) -> list[tuple[set[str], int]]:
    """An exclusion note often lists several topics ("A의 단계, B 핸드셰이크, C 배치는 이 장의 주제가 아니다").
    Each comma separated clause is one topic: (its content words, how many of them a sentence
    must contain to count as talking about it). Short topics need every word, longer ones ~70%."""
    clauses = []
    for part in re.split(r"[,，、;；]", note):
        keys = keywords(part)
        if not keys:
            continue
        need = len(keys) if len(keys) <= 3 else max(3, math.ceil(len(keys) * 0.7))
        clauses.append((keys, need))
    return clauses


def violates_scope(sentence: str, notes: list[str]) -> str | None:
    """The scope note the sentence talks about, or None."""
    words = keywords(sentence)
    for note in notes:
        if not is_exclusion_note(note):
            continue
        for keys, need in scope_clauses(note):
            if len(words & keys) >= need:
                return note
    return None


# ---- hashing / clipping -----------------------------------------------------
def stable_hash(obj: Any) -> str:
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def clip_chars(text: str, limit: int) -> str:
    text = normalize(text)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def truncate_sentences(text: str, max_chars: int, max_sentences: int | None = None) -> str:
    """Keep whole sentences from the start while they fit."""
    kept: list[str] = []
    total = 0
    for s in split_sentences(text):
        add = len(s) + (1 if kept else 0)
        if total + add > max_chars or (max_sentences is not None and len(kept) >= max_sentences):
            break
        kept.append(s)
        total += add
    return " ".join(kept)
