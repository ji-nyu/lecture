"""EvidenceValidator: is an LLM-supplied quote really in the source?

Rule: an LLM output can only become a fact in SourceAnalysis if its evidence
quote is found in the SourceMaterial. The returned `Evidence.quote` is always
the SOURCE text that matched, never the LLM's wording.

Match methods (recorded in `Evidence.match`):
  exact       the quote occurs verbatim in `SourceMaterial.raw_text`
  normalized  it occurs after Unicode NFKC, whitespace, quote-mark, markdown
              emphasis and list-marker normalization
  fuzzy       last resort, deliberately conservative:
                * quote >= 20 chars, similarity ratio >= 0.92 with ONE source sentence
                * numbers must be identical and the count of negation words
                  (않/아니/없/못/불가) must be equal, so "…하지 않는다" can never
                  be matched to "…한다"
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..models.source import Evidence, SourceMaterial

MIN_QUOTE_CHARS = 8
FUZZY_MIN_CHARS = 20
FUZZY_THRESHOLD = 0.92

_QUOTE_MARKS = str.maketrans({"“": '"', "”": '"', "„": '"', "‟": '"', "‘": "'", "’": "'", "‚": "'", "‛": "'"})
_LIST_MARKER = re.compile(r"^\s*(?:[-*+•▪◦]|\d+[.)])\s+")
_NEGATIONS = ("않", "아니", "없", "못", "불가")
_SENTENCE = re.compile(r"[^.!?。]+[.!?。]?")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).translate(_QUOTE_MARKS)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", t).strip()


def _prepare_quote(quote: str) -> str:
    q = normalize(quote)  # first: curly quotes become plain quotes, so they can be stripped
    q = q.strip("\"'").strip()
    q = _LIST_MARKER.sub("", q)
    q = re.sub(r"(?:\.\.\.|…)\s*$", "", q)  # LLMs like to trail off with an ellipsis
    return normalize(q)


@dataclass
class _Seg:
    text: str
    start: int
    end: int
    section_id: str | None
    line: int | None
    page: int | None
    is_heading: bool


class EvidenceValidator:
    def __init__(self, material: SourceMaterial, *, allow_fuzzy: bool = True):
        self.material = material
        self.allow_fuzzy = allow_fuzzy
        self._raw = material.raw_text
        self._segs: list[_Seg] = []
        parts: list[str] = []
        pos = 0

        def add(text, section_id, line, page, heading=False):
            nonlocal pos
            t = normalize(text)
            if not t:
                return
            self._segs.append(_Seg(t, pos, pos + len(t), section_id, line, page, heading))
            parts.append(t)
            pos += len(t) + 1  # one space separator

        for sec in material.sections:
            add(sec.title, sec.id, sec.line, sec.page, heading=True)
            for b in sec.blocks:
                for i, ln in enumerate(b.text.split("\n")):
                    add(ln, sec.id, (b.line + i) if b.line is not None else None, b.page)
        for t in material.tables:
            for row in t.rows:
                add(" | ".join(row), t.section_id, t.location.line if t.location else None,
                    t.location.page if t.location else None)
        self.corpus = " ".join(parts)
        self._starts = [s.start for s in self._segs]
        self._sentences = [
            (s.start + m.start(), s.start + m.end())
            for s in self._segs
            for m in _SENTENCE.finditer(s.text)
            if m.group().strip()
        ]

    # ------------------------------------------------------------------ api
    def validate(self, quote: str) -> Evidence | None:
        """Evidence for `quote`, or None if it is not in the source."""
        if not quote or not isinstance(quote, str):
            return None
        q = _prepare_quote(quote)
        if len(q) < MIN_QUOTE_CHARS:
            return None
        idx = self.corpus.find(q)
        if idx >= 0:
            raw = quote.strip()
            method = "exact" if raw and raw in self._raw else "normalized"
            return self._evidence(idx, idx + len(q), method, 1.0)
        if self.allow_fuzzy and len(q) >= FUZZY_MIN_CHARS:
            hit = self._fuzzy(q)
            if hit:
                score, s, e = hit
                return self._evidence(s, e, "fuzzy", round(score, 3))
        return None

    def contains(self, term: str) -> bool:
        return bool(term) and normalize(term) in self.corpus

    def count(self, term: str) -> int:
        t = normalize(term)
        return self.corpus.count(t) if t else 0

    def in_heading(self, term: str) -> bool:
        t = normalize(term)
        return bool(t) and any(t in s.text for s in self._segs if s.is_heading)

    def section_title(self, section_id: str | None) -> str | None:
        for s in self.material.sections:
            if s.id == section_id:
                return s.title
        return None

    def sentence_of(self, ev: Evidence) -> str:
        """The complete source sentence containing the evidence (or the quote itself
        when it spans several lines)."""
        idx = self.corpus.find(ev.quote)
        if idx < 0:
            return ev.quote
        first, last = self._seg_index(idx), self._seg_index(idx + len(ev.quote) - 1)
        if first != last:
            return ev.quote
        for s, e in self._sentences:
            if s <= idx < e:
                return self.corpus[s:e].strip()
        return ev.quote

    # ------------------------------------------------------------- internals
    def _seg_index(self, pos: int) -> int:
        return max(0, bisect.bisect_right(self._starts, pos) - 1)

    def _evidence(self, start: int, end: int, method: str, score: float) -> Evidence:
        a, b = self._segs[self._seg_index(start)], self._segs[self._seg_index(max(start, end - 1))]
        return Evidence(
            quote=self.corpus[start:end],
            page=a.page,
            line_start=a.line,
            line_end=b.line,
            section_id=a.section_id,
            match=method,
            match_score=score,
        )

    @staticmethod
    def _same_facts(a: str, b: str) -> bool:
        if re.findall(r"\d+", a) != re.findall(r"\d+", b):
            return False
        return sum(a.count(n) for n in _NEGATIONS) == sum(b.count(n) for n in _NEGATIONS)

    def _fuzzy(self, q: str) -> tuple[float, int, int] | None:
        best: tuple[float, int, int] | None = None
        lo, hi = 0.7 * len(q), 1.4 * len(q)
        for s, e in self._sentences:
            if not (lo <= e - s <= hi):
                continue
            cand = self.corpus[s:e].strip()
            sm = SequenceMatcher(None, q, cand, autojunk=False)
            if sm.real_quick_ratio() < FUZZY_THRESHOLD or sm.quick_ratio() < FUZZY_THRESHOLD:
                continue
            ratio = sm.ratio()
            if ratio >= FUZZY_THRESHOLD and self._same_facts(q, cand):
                if best is None or ratio > best[0]:
                    best = (ratio, s, s + len(cand))
        return best
