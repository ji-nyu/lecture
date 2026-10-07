"""LectureAnalyzer: SourceMaterial -> SourceAnalysis (rule based, no LLM).

Design notes
------------
* Deterministic and offline. No text is invented: every concept description,
  definition, example and important point is an *extract* of the source, and
  carries a `source_location`.
* Concept extraction is deliberately NOT frequency based on Korean words
  (that would yield "데이터", "온도", "장치" ...). A term becomes a concept only
  through structural evidence:
    - Latin technical terms (acronyms / Capitalised phrases) that are repeated,
      appear in a heading, start a sentence, are defined, or are packet-like
      ALL-CAPS items in a list;
    - subjects of definitional sentences ("X는 ... 이다", "X는 ... 약자");
    - abbreviations linked by "Full Name(ABBR)" or "ABBR는 ... 의 약자".
  If a document has very few Latin terms, heading cores are used as a fallback
  (a warning is recorded).
* `estimated_complexity`, `importance` and the topic grouping are heuristics.
  They are documented in code and are meant to be reviewed by the instructor
  in later stages, not treated as ground truth.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from ..models.lecture_profile import Difficulty
from ..models.source import (
    AnalyzerInfo,
    Complexity,
    Concept,
    ConceptCategory,
    Definition,
    ExampleItem,
    HierarchyNode,
    ImportantPoint,
    MainTopic,
    ScopeNote,
    SectionInfo,
    SourceAnalysis,
    SourceLocation,
    SourceMaterial,
    SourceSection,
)
from .analyzer_strategy import AnalyzerStrategy
from .provenance import annotate_heuristic

# ---------------------------------------------------------------------------
# Patterns / word lists
# ---------------------------------------------------------------------------
_LATIN_WORD = r"[A-Z][A-Za-z0-9]*(?:/[A-Z][A-Za-z0-9]*)?"
LATIN_TERM = re.compile(
    rf"(?<![A-Za-z0-9_]){_LATIN_WORD}(?:(?:\s+(?:of|and|to))?\s+{_LATIN_WORD})*(?![A-Za-z0-9])"
)
PAREN_ABBR = re.compile(r"(?P<outer>[^\s()]+)\((?P<inner>[A-Z][A-Za-z0-9/ ]{1,30}?)\)")
ABBR_OF = re.compile(
    r"(?P<abbr>[A-Z][A-Za-z0-9]{1,10})(?:은|는)\s+"
    r"(?P<full>[A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*|\s+(?:of|and))*)의?\s*약자"
)
TITLE_ORDINAL = re.compile(
    r"^\s*(?:제\s*\d+\s*[장절편]|Chapter\s+\d+|\d+(?:\.\d+)*[.)]?)\s+", re.IGNORECASE
)
HEADING_CORE = re.compile(
    r"^(?P<core>.+?)(?:이?란 무엇인가|의 (?:역할|구조|특징|구성 요소|구성|차이|기본 구성|계층 구조|결합)"
    r"|(?:이|가) 필요한 이유)$"
)

STOP_SINGLE = {
    "The", "A", "An", "In", "On", "It", "This", "That", "For", "And", "Or", "To", "Of",
    "I", "OK", "No", "Yes", "We", "You", "Is", "As", "At", "By", "If", "Not",
}
# Very generic technical tokens that are not teachable concepts on their own.
GENERIC_TERMS = {"IP", "ID", "PC", "TV", "UI", "OS"}
DEMONSTRATIVE_START = (
    "이", "그", "저", "각", "모든", "위", "아래", "다음", "여기서", "그래서", "따라서",
    "즉", "예를", "또한", "하지만", "그러나", "이것", "그것",
)
PARTICLE_END = ("에서", "에게", "으로", "로서", "로써", "에", "로", "와", "과", "의", "도", "만")

IMPORTANT_MARKERS = ("중요", "핵심", "반드시", "주의", "유의", "명심", "위험", "목표")
SCOPE_MARKERS = (
    "주제가 아니다", "섞지 않는다", "범위는", "가르친다", "외우는 일이 아니라",
    "구분해서", "따로 구분", "설명해야 할", "다루지", "제외",
)
EXAMPLE_START = re.compile(r"^(?:예\s*[:：)]|예시\s*[:：)]?|예를 들어|Example|e\.g\.)", re.IGNORECASE)
EXAMPLE_HEADING = ("활용", "사례", "예제", "예시", "적용")
CATEGORY_HEADING_KEYWORDS = [
    (ConceptCategory.summary, ("요약", "정리", "마무리")),
    (ConceptCategory.practice, ("실습", "따라하기", "실행")),
    (ConceptCategory.example, ("활용", "사례", "예제", "예시")),
    (ConceptCategory.caution, ("주의", "고려사항", "함정", "오류")),
    (ConceptCategory.process, ("과정", "절차", "순서", "흐름", "단계")),
    (ConceptCategory.architecture, ("구조", "구성", "아키텍처")),
]
MAX_CONCEPTS = 40


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def clean(text: str) -> str:
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def clean_title(title: str) -> str:
    return clean(TITLE_ORDINAL.sub("", title))


def truncate(text: str, n: int = 220) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def split_sentences(line: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。])\s+", line.strip())
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def is_definitional(text: str) -> bool:
    """True for 'X는 ... 이다/말한다/약자' style sentences (not negations)."""
    s = text.strip().rstrip(".。 ")
    # negations and contrasts ("A가 아니라 B이다") are not definitions
    if s.endswith("아니다") or "아니라" in s or "아니며" in s:
        return False
    if re.search(r"(약자|말한다|의미한다|뜻한다)", s):
        return True
    if s.endswith("이다"):
        return True
    if s.endswith("다") and len(s) >= 2:
        prev = s[-2]
        if prev in "하되":
            return False
        # A noun ending in a vowel + 다 (e.g. 메시지다). Verbs end in 받침+다.
        if "가" <= prev <= "힣" and (ord(prev) - 0xAC00) % 28 == 0:
            return True
    return False


def _has_latin(s: str) -> bool:
    return re.search(r"[A-Za-z]", s) is not None


def contains_term(text: str, name: str) -> bool:
    """Whole-word match for Latin terms.

    'IoT' must not match inside 'AIoT', and 'AI' / 'Message' must not match
    when they are only the tail/head of a longer Capitalised phrase such as
    'Edge AI' / 'Retained Message'.
    """
    if not re.match(r"^[A-Za-z0-9]", name):
        return name in text
    for mt in re.finditer(rf"(?<![A-Za-z0-9/]){re.escape(name)}(?![A-Za-z0-9/])", text):
        if name[0].isupper():
            if re.search(r"(?<![A-Za-z0-9/])[A-Z][A-Za-z0-9]*\s$", text[: mt.start()]):
                continue
            if re.match(r"\s[A-Z][A-Za-z0-9]", text[mt.end():]):
                continue
        return True
    return False


@dataclass
class Sent:
    idx: int
    text: str
    section: SourceSection
    kind: str
    line: int | None
    page: int | None
    block_first: bool

    def location(self) -> SourceLocation:
        return SourceLocation(
            section_id=self.section.id,
            section_title=clean_title(self.section.title),
            line=self.line,
            page=self.page,
        )


@dataclass
class Occ:
    term: str
    section_id: str
    sent_idx: int  # -1 for headings
    in_heading: bool
    initial: bool
    in_list: bool


@dataclass
class _Stat:
    count: int = 0
    in_heading: int = 0
    initial: bool = False
    list_allcaps: bool = False
    sections: set = None  # type: ignore[assignment]

    def __post_init__(self):
        self.sections = set()


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
class HeuristicAnalyzer(AnalyzerStrategy):
    """The rule-based analyzer (STAGE 2). Unchanged logic; `analyze` only adds
    provenance metadata to its result afterwards."""

    name = "heuristic-v1"

    def analyze(self, m: SourceMaterial) -> SourceAnalysis:
        result = self._analyze(m)
        result.analyzer_info = AnalyzerInfo(mode_requested="heuristic", mode_used="heuristic")
        return annotate_heuristic(result)

    def _analyze(self, m: SourceMaterial) -> SourceAnalysis:
        warnings = list(m.warnings)
        secs = m.sections
        sents = self._sentences(secs)
        by_sec: dict[str, list[Sent]] = defaultdict(list)
        for s in sents:
            by_sec[s.section.id].append(s)

        alias_of, aliases = self._detect_aliases(sents)
        raw_occ = self._scan(secs, sents)
        main_subject = self._main_subject(m.title, raw_occ)
        canon = self._build_canon(raw_occ, alias_of, aliases, main_subject)

        # ---- canonical occurrences / statistics
        stats: dict[str, _Stat] = defaultdict(_Stat)
        occ_by_term: dict[str, list[Occ]] = defaultdict(list)
        for o in raw_occ:
            t = canon(o.term)
            if not t:
                continue
            st = stats[t]
            st.count += 1
            st.sections.add(o.section_id)
            if o.in_heading:
                st.in_heading += 1
            st.initial = st.initial or (o.initial and not o.in_heading)
            if o.in_list and o.term.isupper() and len(o.term) >= 4:
                st.list_allcaps = True
            occ_by_term[t].append(o)

        # ---- definitions
        defs = self._find_definitions(sents, canon)
        for term in defs:
            stats.setdefault(term, _Stat()).sections.add(defs[term][0].section.id)

        # ---- accept candidates
        accepted: list[str] = []
        for term, st in stats.items():
            if term in GENERIC_TERMS or (" " not in term and term in STOP_SINGLE):
                continue
            if (
                st.count >= 2
                or st.in_heading
                or st.initial
                or st.list_allcaps
                or term in defs
                or term in aliases
            ):
                accepted.append(term)

        if sum(1 for t in accepted if _has_latin(t)) < 5:
            warnings.append(
                "자료에 영문 전문용어가 적어 제목 기반으로 개념을 보완했습니다. 개념 목록을 검토해 주세요."
            )
            for t in self._heading_cores(secs, sents):
                if t not in accepted:
                    accepted.append(t)
                    stats.setdefault(t, _Stat()).count += 1

        # ---- section text index for description search
        sec_by_id = {s.id: s for s in secs}
        first_pos = {}
        for s in sents:
            for t in accepted:
                if t in first_pos:
                    continue
                if self._mentions(s.text, t, aliases):
                    first_pos[t] = s.idx

        concepts = self._build_concepts(
            m, accepted, stats, defs, sents, by_sec, sec_by_id, aliases, first_pos, main_subject
        )
        concepts = concepts[:MAX_CONCEPTS]
        concept_names = [c.name for c in concepts]

        # ---- per-section concept counts
        # (Latin terms reuse the maximal-match occurrences collected above, so
        # "Message" is not counted inside "Retained Message".)
        name_set = set(concept_names)
        sec_counts: dict[str, Counter] = {sec.id: Counter() for sec in secs}
        for o in raw_occ:
            t = canon(o.term)
            if t in name_set:
                sec_counts[o.section_id][t] += 1
        for name in concept_names:
            if _has_latin(name):
                continue
            for sec in secs:
                texts = [clean_title(sec.title)] + [s.text for s in by_sec[sec.id]]
                n = sum(text.count(name) for text in texts)
                if n:
                    sec_counts[sec.id][name] += n

        definitions = [
            Definition(
                term=c.name,
                definition=truncate(clean(defs[c.name][1]), 300),
                source_location=defs[c.name][0].location(),
            )
            for c in concepts
            if c.name in defs
        ]
        examples = self._examples(secs, sents, by_sec, concept_names, aliases)
        important = self._important_points(sents)
        scope_notes = self._scope_notes(sents)

        example_secs = {e.source_location.section_id for e in examples}
        def_secs = {d.source_location.section_id for d in definitions}
        section_infos = []
        for sec in secs:
            first = next((s.text for s in by_sec[sec.id]), "")
            section_infos.append(
                SectionInfo(
                    id=sec.id,
                    order=sec.order,
                    title=clean_title(sec.title),
                    level=sec.level,
                    summary=truncate(clean(first), 160),
                    concepts=[n for n, _ in sec_counts[sec.id].most_common(8)],
                    char_count=sec.char_count,
                    has_definition=sec.id in def_secs,
                    has_example=sec.id in example_secs,
                )
            )

        main_topics = self._main_topics(secs, sec_counts, concepts)
        complexity = self._complexity(m, sents, concepts)
        summary = self._summary(m, secs, by_sec, main_topics)

        return SourceAnalysis(
            source_id=m.id,
            title=m.title,
            summary=summary,
            main_topics=main_topics,
            concepts=concepts,
            definitions=definitions,
            examples=examples,
            formulas=m.formulas,
            code_examples=m.code_blocks,
            sections=section_infos,
            section_hierarchy=self._hierarchy(secs),
            important_points=important,
            scope_notes=scope_notes,
            estimated_complexity=complexity,
            analyzer=self.name,
            generated_at=datetime.now(timezone.utc),
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # sentences
    # ------------------------------------------------------------------
    def _sentences(self, secs: list[SourceSection]) -> list[Sent]:
        out: list[Sent] = []
        for sec in secs:
            for b in sec.blocks:
                first = True
                for li, line in enumerate(b.text.split("\n")):
                    for sent in split_sentences(line):
                        out.append(
                            Sent(
                                idx=len(out),
                                text=sent,
                                section=sec,
                                kind=b.kind,
                                line=(b.line + li) if b.line is not None else None,
                                page=b.page,
                                block_first=first,
                            )
                        )
                        first = False
        return out

    # ------------------------------------------------------------------
    # term scanning
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_term(raw: str) -> str | None:
        toks = raw.split()
        while toks and toks[0] in STOP_SINGLE | {"of", "and", "to"}:
            toks.pop(0)
        while toks and toks[-1] in STOP_SINGLE | {"of", "and", "to"}:
            toks.pop()
        if not toks:
            return None
        term = " ".join(toks)
        if len(toks) == 1 and (len(term) < 2 or term in STOP_SINGLE):
            return None
        return term

    def _scan(self, secs: list[SourceSection], sents: list[Sent]) -> list[Occ]:
        occ: list[Occ] = []
        for sec in secs:
            title = clean_title(sec.title)
            for mt in LATIN_TERM.finditer(title):
                t = self._norm_term(mt.group())
                if t:
                    occ.append(Occ(t, sec.id, -1, True, False, False))
        for s in sents:
            text = clean(s.text)
            for mt in LATIN_TERM.finditer(text):
                t = self._norm_term(mt.group())
                if not t:
                    continue
                prefix = text[: mt.start()].strip(" \"'“‘(*_-")
                occ.append(Occ(t, s.section.id, s.idx, False, prefix == "", s.kind == "list_item"))
        return occ

    def _detect_aliases(self, sents: list[Sent]):
        """alias_of: alias -> primary. aliases: primary -> [aliases]."""
        alias_of: dict[str, str] = {}
        aliases: dict[str, list[str]] = defaultdict(list)

        def link(alias: str, primary: str):
            if alias == primary or alias in alias_of:
                return
            alias_of[alias] = primary
            if alias not in aliases[primary]:
                aliases[primary].append(alias)

        for s in sents:
            text = clean(s.text)
            for mt in PAREN_ABBR.finditer(text):
                outer, inner = mt.group("outer"), mt.group("inner").strip()
                if re.match(r"^[A-Z]", outer):
                    toks = text[: mt.start()].split()
                    phrase = [outer]
                    j = len(toks) - 1
                    while j >= 0 and re.fullmatch(r"[A-Z][A-Za-z0-9]*|of|and|to", toks[j]):
                        phrase.insert(0, toks[j])
                        j -= 1
                    while phrase and phrase[0] in ("of", "and", "to"):
                        phrase.pop(0)
                    primary = " ".join(phrase)
                    link(inner, primary)
                elif re.match(r"^[가-힣]", outer):
                    link(outer, inner)
            for mt in ABBR_OF.finditer(text):
                link(mt.group("full").strip(), mt.group("abbr"))
        return alias_of, aliases

    def _main_subject(self, title: str, occ: list[Occ]) -> str | None:
        counts = Counter(o.term for o in occ)
        cands = [
            self._norm_term(mt.group()) for mt in LATIN_TERM.finditer(clean_title(title))
        ]
        cands = [c for c in cands if c and " " not in c]
        return max(cands, key=lambda c: counts.get(c, 0)) if cands else None

    def _build_canon(self, occ, alias_of, aliases, main_subject):
        raw_counts = Counter(o.term for o in occ)
        merge: dict[str, str] = {}

        # "MQTT Client" -> "Client" when "Client" also occurs on its own
        if main_subject:
            for term in list(raw_counts):
                toks = term.split()
                if len(toks) > 1 and toks[0] == main_subject:
                    rest = " ".join(toks[1:])
                    if raw_counts.get(rest, 0) > 0 and rest != main_subject:
                        merge[term] = rest
                        if term not in aliases[rest]:
                            aliases[rest].append(term)
        # "Publish/Subscribe": its components are the same concept
        for term in list(raw_counts):
            if "/" in term:
                for comp in term.split("/"):
                    if comp in raw_counts and "/" not in comp:
                        merge.setdefault(comp, term)

        def canon(term: str) -> str | None:
            term = alias_of.get(term, term)
            term = merge.get(term, term)
            term = alias_of.get(term, term)
            return term if term else None

        return canon

    # ------------------------------------------------------------------
    # definitions
    # ------------------------------------------------------------------
    def _definition_subject(self, text: str) -> str | None:
        toks = clean(text).lstrip("\"'“‘ ").split()
        base = None
        j = None
        for i in range(min(4, len(toks))):
            t = toks[i]
            if len(t) >= 2 and t.endswith(("은", "는")):
                base, j = t[:-1], i
                break
        if base is None or j is None:
            return None
        if base.endswith(PARTICLE_END) and not re.search(r"[A-Za-z]$", base):
            return None
        head = toks[:j] + [base]
        term = " ".join(head)
        term = re.sub(r"\([^)]*\)$", "", term).strip()
        term = re.sub(r"(?:\s+[\d,]+)+$", "", term).strip()  # "QoS 0, 1, 2" -> "QoS"
        parts = term.split()
        if not parts or len(parts) > 4 or len(term) > 40:
            return None
        if parts[0].startswith(DEMONSTRATIVE_START) and not re.match(r"[A-Za-z]", parts[0]):
            return None
        if any(p.endswith("의") and len(p) > 1 for p in parts):
            return None
        if _has_latin(term):
            return term
        if len(parts) == 1 and 2 <= len(term) <= 8:
            return term
        return None

    def _find_definitions(self, sents: list[Sent], canon) -> dict[str, tuple[Sent, str]]:
        best: dict[str, tuple[float, Sent]] = {}
        for s in sents:
            if not is_definitional(s.text):
                continue
            subject = self._definition_subject(s.text)
            if not subject:
                continue
            term = self._norm_term(subject) if _has_latin(subject) else subject
            term = canon(term) if term else None
            if not term or term in GENERIC_TERMS:
                continue
            score = 0.0
            if contains_term(clean_title(s.section.title), term):
                score += 3
            if s.block_first:
                score += 1
            score -= s.idx * 0.001
            if term not in best or score > best[term][0]:
                best[term] = (score, s)
        return {t: (s, clean(s.text)) for t, (_, s) in best.items()}

    # ------------------------------------------------------------------
    # concepts
    # ------------------------------------------------------------------
    @staticmethod
    def _names(term: str, aliases) -> list[str]:
        return [term] + list(aliases.get(term, []))

    def _mentions(self, text: str, term: str, aliases) -> bool:
        return any(contains_term(text, n) for n in self._names(term, aliases))

    def _build_concepts(
        self, m, accepted, stats, defs, sents, by_sec, sec_by_id, aliases, first_pos, main_subject
    ) -> list[Concept]:
        secs = m.sections
        titles = {s.id: clean_title(s.title) for s in secs}
        title_terms = {
            self._norm_term(mt.group()) for mt in LATIN_TERM.finditer(clean_title(m.title))
        }

        # component lists: "핵심 구성 요소는 A, B, C, D이다"
        components: set[str] = set()
        for s in sents:
            mt = re.search(r"구성 요소(?:는|들은|로는)", s.text)
            if mt:
                rest = s.text[mt.end():]  # only what is listed after the phrase
                for t in accepted:
                    if self._mentions(rest, t, aliases):
                        components.add(t)

        # ---- source location + description
        loc_of: dict[str, tuple[SourceLocation, str]] = {}
        for t in accepted:
            names = self._names(t, aliases)
            if t in defs:
                s, text = defs[t]
                loc_of[t] = (s.location(), text)
                continue
            intro = next(
                (
                    s
                    for s in sents
                    if any(re.search(rf"{re.escape(n)}(?:이?라는)", s.text) for n in names)
                ),
                None,
            )
            if intro:
                loc_of[t] = (intro.location(), clean(intro.text))
                continue
            head_sec = next(
                (
                    sec
                    for sec in secs
                    if any(contains_term(titles[sec.id], n) for n in names)
                    and by_sec[sec.id]
                ),
                None,
            )
            if head_sec:
                cand = next(
                    (s for s in by_sec[head_sec.id] if self._mentions(s.text, t, aliases)),
                    by_sec[head_sec.id][0],
                )
                loc_of[t] = (cand.location(), clean(cand.text))
                continue
            idx = first_pos.get(t)
            if idx is not None:
                s = sents[idx]
                loc_of[t] = (s.location(), clean(s.text))
                continue
            sec = next((x for x in secs if names[0] in x.title), secs[0])
            loc_of[t] = (SourceLocation(section_id=sec.id, section_title=titles[sec.id]), titles[sec.id])

        order_of = {sec.id: sec.order for sec in secs}

        # ---- importance score
        scored = {}
        for t in accepted:
            st = stats[t]
            score = (
                math.log1p(st.count)
                + 1.5 * min(st.in_heading, 2)
                + (1.5 if t in defs else 0)
                + 0.4 * len(st.sections)
            )
            scored[t] = score
        ranking = sorted(accepted, key=lambda t: -scored[t])
        n = max(len(ranking), 1)
        importance: dict[str, int] = {}
        for i, t in enumerate(ranking):
            r = i / n
            importance[t] = 5 if r < 0.12 else 4 if r < 0.35 else 3 if r < 0.65 else 2 if r < 0.85 else 1
            if t in title_terms or t == main_subject:
                importance[t] = 5
            elif t in defs and stats[t].in_heading and importance[t] < 3:
                importance[t] = 3
        # Drop one-off, lowest-importance terms (e.g. a packet name mentioned once).
        accepted = [t for t in accepted if not (importance[t] == 1 and stats[t].count <= 1)]

        # ---- category
        def category(t: str) -> ConceptCategory:
            loc, _ = loc_of[t]
            heading = (loc.section_title or "")
            if any(t in blk.code for blk in m.code_blocks):
                return ConceptCategory.code
            if t in components:
                return ConceptCategory.architecture
            # A heading keyword only classifies terms that the heading itself
            # names (otherwise "Wildcard" would become "architecture" just
            # because it sits under "Topic의 계층 구조").
            if any(contains_term(heading, n) for n in self._names(t, aliases)):
                for cat, keys in CATEGORY_HEADING_KEYWORDS:
                    if not any(k in heading for k in keys):
                        continue
                    if cat == ConceptCategory.process and not t.isupper():
                        continue  # only packet-like ALL-CAPS steps are 'process'
                    if cat == ConceptCategory.architecture and t in defs:
                        break
                    return cat
            if t.isupper() and stats[t].list_allcaps:
                return ConceptCategory.process
            if t in defs:
                return ConceptCategory.definition
            return ConceptCategory.principle

        concepts: list[Concept] = []
        for t in accepted:
            loc, desc = loc_of[t]
            # prerequisite: concepts named in the description that are introduced earlier
            prereq = []
            for u in accepted:
                if u == t or not self._mentions(desc, u, aliases):
                    continue
                uloc = loc_of[u][0]
                if order_of.get(uloc.section_id or "", 0) < order_of.get(loc.section_id or "", 0):
                    prereq.append(u)
            prereq.sort(key=lambda u: -stats[u].count)
            concepts.append(
                Concept(
                    name=t,
                    aliases=list(aliases.get(t, [])),
                    description=truncate(desc),
                    importance=importance[t],
                    prerequisite=prereq[:3],
                    source_location=loc,
                    category=category(t),
                    mention_count=stats[t].count,
                )
            )
        concepts.sort(key=lambda c: (-c.importance, -c.mention_count, c.name))
        return concepts

    def _heading_cores(self, secs, sents) -> list[str]:
        out = []
        for sec in secs:
            title = clean_title(sec.title)
            mt = HEADING_CORE.match(title)
            core = mt.group("core").strip() if mt else None
            if core and len(core.split()) <= 3 and core not in out:
                out.append(core)
        return out

    # ------------------------------------------------------------------
    # examples / important points / scope notes
    # ------------------------------------------------------------------
    def _examples(self, secs, sents, by_sec, concept_names, aliases) -> list[ExampleItem]:
        def related(text: str) -> list[str]:
            return [c for c in concept_names if self._mentions(text, c, aliases)][:5]

        out: list[ExampleItem] = []
        seen = set()
        for s in sents:
            if EXAMPLE_START.match(clean(s.text)):
                text = clean(s.text)
                if text not in seen:
                    seen.add(text)
                    out.append(
                        ExampleItem(
                            kind="inline_example",
                            text=truncate(text, 300),
                            related_concepts=related(text),
                            source_location=s.location(),
                        )
                    )
        for sec in secs:
            title = clean_title(sec.title)
            if any(k in title for k in EXAMPLE_HEADING) and by_sec[sec.id]:
                first = by_sec[sec.id][0]
                body = " ".join(clean(x.text) for x in by_sec[sec.id][:2])
                out.append(
                    ExampleItem(
                        kind="case_study",
                        text=truncate(f"{title}: {body}", 300),
                        related_concepts=related(body),
                        source_location=first.location(),
                    )
                )
        return out

    def _important_points(self, sents: list[Sent]) -> list[ImportantPoint]:
        out = []
        for s in sents:
            text = clean(s.text)
            marker = next((k for k in IMPORTANT_MARKERS if k in text), None)
            if marker and len(text) >= 12:
                out.append(
                    ImportantPoint(
                        text=truncate(text, 260),
                        reason=f"'{marker}' 표현이 포함된 문장",
                        source_location=s.location(),
                    )
                )
        return out[:12]

    def _scope_notes(self, sents: list[Sent]) -> list[ScopeNote]:
        out = []
        for s in sents:
            text = clean(s.text)
            if any(k in text for k in SCOPE_MARKERS):
                out.append(ScopeNote(text=truncate(text, 300), source_location=s.location()))
        return out[:15]

    # ------------------------------------------------------------------
    # structure: hierarchy, topics, summary, complexity
    # ------------------------------------------------------------------
    def _hierarchy(self, secs: list[SourceSection]) -> list[HierarchyNode]:
        nodes = {
            s.id: HierarchyNode(id=s.id, title=clean_title(s.title), level=s.level) for s in secs
        }
        roots: list[HierarchyNode] = []
        for s in secs:
            if s.parent_id and s.parent_id in nodes:
                nodes[s.parent_id].children.append(nodes[s.id])
            else:
                roots.append(nodes[s.id])
        return roots

    def _main_topics(self, secs, sec_counts, concepts) -> list[MainTopic]:
        # The title section (a lone level-1 heading that owns the others) is
        # not a topic of its own.
        parents = {s.parent_id for s in secs if s.parent_id}
        content = [s for s in secs if s.id not in parents or s.parent_id]
        if not content:
            content = secs
        n = len(content)
        importance = {c.name: c.importance for c in concepts}

        if n <= 3:
            groups = [[s] for s in content]
        else:
            df = Counter(c for s in content for c in set(sec_counts[s.id]))
            idf = {c: math.log((n + 1) / (df[c] + 0.5)) for c in df}

            def vec(group):
                v: Counter = Counter()
                for s in group:
                    for c, k in sec_counts[s.id].items():
                        v[c] += k * idf[c]
                return v

            def cos(a, b):
                num = sum(a[c] * b[c] for c in a if c in b)
                den = math.sqrt(sum(x * x for x in a.values())) * math.sqrt(
                    sum(x * x for x in b.values())
                )
                return num / den if den else 0.0

            def strength(i: int) -> float:
                # how much the vocabulary changes across the boundary before section i
                return 1 - cos(vec(content[max(0, i - 2): i]), vec(content[i: i + 2]))

            # Roughly balanced topics: around each ideal cut position, pick the
            # boundary where the concept vocabulary changes the most.
            k = max(3, min(8, round(n / 5)))
            k = min(k, n // 2)
            w = max(1, round(n / k / 2))
            cuts: list[int] = []
            prev = 0
            for j in range(1, k):
                p = round(j * n / k)
                lo, hi = max(prev + 2, p - w), min(n - 2, p + w)
                if lo > hi:
                    continue
                best = max(range(lo, hi + 1), key=lambda i: (strength(i), -abs(i - p)))
                cuts.append(best)
                prev = best
            bounds = [0] + cuts + [n]
            groups = [content[bounds[i]: bounds[i + 1]] for i in range(len(bounds) - 1)]

        topics = []
        for g in groups:
            weights: Counter = Counter()
            total: Counter = Counter()
            for s in g:
                for c, cnt in sec_counts[s.id].items():
                    total[c] += cnt
            df_all = Counter(c for s in content for c in set(sec_counts[s.id]))
            for c, cnt in total.items():
                idf_c = math.log((len(content) + 1) / (df_all[c] + 0.5))
                weights[c] = cnt * idf_c * (1 + 0.2 * importance.get(c, 1))
            # Title: important concepts named in the group's own headings (what
            # the author chose to headline); fall back to distinctive concepts.
            heading_terms: Counter = Counter()
            for s in g:
                for c in concepts:
                    if c.importance >= 3 and any(
                        contains_term(clean_title(s.title), n) for n in [c.name, *c.aliases]
                    ):
                        heading_terms[c.name] += 1
            # Rank by importance x distinctiveness so a term that is in every
            # section's vocabulary (e.g. the document's main subject) does not
            # headline every topic.
            def distinct(c: str) -> float:
                return importance.get(c, 1) * math.log((len(content) + 1) / (df_all[c] + 0.5))

            top = sorted(heading_terms, key=lambda c: -distinct(c))[:3]
            if len(top) < 2:
                for c, _ in weights.most_common(6):
                    if c not in top and importance.get(c, 1) >= 3:
                        top.append(c)
                    if len(top) >= 3:
                        break
            first, last = clean_title(g[0].title), clean_title(g[-1].title)
            key = [c for c, _ in weights.most_common(5)]
            topics.append(
                MainTopic(
                    title=" · ".join(top) if top else first,
                    summary=(
                        f"‘{first}’" + (f" ~ ‘{last}’" if len(g) > 1 else "") + f" ({len(g)}개 섹션)"
                    ),
                    section_ids=[s.id for s in g],
                    key_concepts=key,
                )
            )
        return topics

    def _summary(self, m, secs, by_sec, topics) -> str:
        first_sentence = ""
        for sec in secs:
            if by_sec[sec.id]:
                first_sentence = clean(by_sec[sec.id][0].text)
                break
        parts = []
        if first_sentence:
            parts.append(first_sentence if first_sentence.endswith((".", "다")) else first_sentence + ".")
        if topics:
            parts.append("주요 주제: " + "; ".join(t.title for t in topics[:6]) + ".")
        return " ".join(parts) or m.title

    def _complexity(self, m: SourceMaterial, sents: list[Sent], concepts: list[Concept]) -> Complexity:
        chars = max(len(m.raw_text), 1)
        avg_len = sum(len(s.text) for s in sents) / max(len(sents), 1)
        term_density = len(concepts) / (chars / 1000)
        has_code = 1.0 if m.code_blocks else 0.0
        has_formula = 1.0 if m.formulas else 0.0
        score = (
            0.4 * min(term_density / 10, 1)
            + 0.3 * min(avg_len / 70, 1)
            + 0.15 * has_code
            + 0.15 * has_formula
        )
        level = (
            Difficulty.introductory if score < 0.25
            else Difficulty.beginner if score < 0.45
            else Difficulty.intermediate if score < 0.7
            else Difficulty.advanced
        )
        return Complexity(
            level=level,
            score=round(score, 3),
            factors={
                "concepts_per_1k_chars": round(term_density, 2),
                "avg_sentence_chars": round(avg_len, 1),
                "has_code": has_code,
                "has_formula": has_formula,
            },
        )


# Backward-compatible name used by STAGE 2 code and tests.
LectureAnalyzer = HeuristicAnalyzer
