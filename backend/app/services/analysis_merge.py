"""Merge validated LLM extraction into the heuristic SourceAnalysis.

Order: (heuristic result) + (validated LLM items) -> alias normalization ->
dedupe -> provenance -> confidence.

Policies
  * A concept found by both gets provenance `heuristic+llm` (confidence raised).
  * Heuristic results are never dropped because of the LLM (hybrid mode). In
    `llm` mode heuristic-ONLY concepts are dropped; everything else is kept.
  * Every text taken from the LLM path is a SOURCE sentence (see EvidenceValidator).
  * LLM-only concepts are capped (MAX_LLM_ONLY) so the LLM cannot flood the list.
  * Prerequisites found by semantic inference are `inferred=True`,
    `provenance=llm_inference`.
"""

from __future__ import annotations

import math
import re

from ..models.source import (
    Concept,
    ConceptCategory,
    Definition,
    Evidence,
    MainTopic,
    PrerequisiteLink,
    Provenance,
    ScopeNote,
    SourceAnalysis,
    SourceLocation,
)
from .evidence_validator import EvidenceValidator, normalize
from .lecture_analyzer import clean_title, truncate
from .llm_extractor import LLMConcept, LLMExtraction, TopicLabel, _key

MAX_LLM_ONLY = 20
MAX_PREREQ = 5


def llm_concept_confidence(c: LLMConcept) -> float:
    conf = 0.6
    conf += 0.15 if c.evidence.match in ("exact", "normalized") else 0.05
    conf += 0.1 if c.in_heading else 0.0
    conf += 0.1 if c.definitional else 0.0
    return round(min(conf, 0.95), 2)


def _importance(c: LLMConcept) -> int:
    score = math.log1p(c.count) + (1.5 if c.in_heading else 0) + (1.5 if c.definitional else 0)
    return 5 if score >= 4.5 else 4 if score >= 3.5 else 3 if score >= 2.5 else 2 if score >= 1.5 else 1


def _location(ev: Evidence, v: EvidenceValidator) -> SourceLocation:
    title = v.section_title(ev.section_id)
    return SourceLocation(
        section_id=ev.section_id,
        section_title=clean_title(title) if title else None,
        line=ev.line_start,
        page=ev.page,
    )


def _words(name: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9]+", name.lower()))


def _similar(a: str, b: str) -> bool:
    a, b = normalize(a).rstrip("….").lower(), normalize(b).rstrip("….").lower()
    return bool(a) and bool(b) and (a in b or b in a)


class _Index:
    """name/alias (normalized key) -> Concept."""

    def __init__(self, concepts: list[Concept]):
        self.map: dict[str, Concept] = {}
        for c in concepts:
            self.add(c)

    def add(self, c: Concept) -> None:
        for n in [c.name, *c.aliases]:
            self.map.setdefault(_key(n), c)

    def find(self, names: list[str]) -> Concept | None:
        for n in names:
            hit = self.map.get(_key(n))
            if hit is not None:
                return hit
        return None


def merge_analysis(
    base: SourceAnalysis,
    ex: LLMExtraction,
    v: EvidenceValidator,
    *,
    keep_heuristic_only: bool = True,
) -> SourceAnalysis:
    a = base.model_copy(deep=True)
    idx = _Index(a.concepts)
    matched: set[int] = set()  # id() of heuristic concepts confirmed by the LLM
    heuristic_ids = {id(c) for c in a.concepts}
    discarded = ex.discarded

    # ---- concepts -------------------------------------------------------
    defs_by_key = {}
    for d in ex.definitions:
        defs_by_key.setdefault(_key(d.term), d)

    added: list[Concept] = []
    for lc in ex.concepts:
        conf = llm_concept_confidence(lc)
        hit = idx.find([lc.name, *lc.aliases])
        if hit is not None:
            if id(hit) in heuristic_ids:
                matched.add(id(hit))
                hit.provenance = Provenance.heuristic_llm
                hit.confidence = round(min(0.98, max(hit.confidence, conf) + 0.05), 2)
                if hit.category == ConceptCategory.principle and lc.category:
                    hit.category = lc.category
            for n in [lc.name, *lc.aliases]:
                if _key(n) != _key(hit.name) and n not in hit.aliases:
                    hit.aliases.append(n)
            idx.add(hit)
            continue
        # a single Latin word that is part of a longer known term is that term's fragment
        if any(
            len(_words(c.name)) > 1 and _words(lc.name) and _words(lc.name) < _words(c.name)
            for c in [*a.concepts, *added]
        ) and not lc.in_heading:
            discarded["concept.subsumed"] += 1
            continue
        if len(added) >= MAX_LLM_ONLY:
            discarded["concept.over_limit"] += 1
            continue
        d = defs_by_key.get(_key(lc.name))
        desc_sentence = d.sentence if d else lc.sentence
        ev = d.evidence if d else lc.evidence
        c = Concept(
            name=lc.name,
            aliases=lc.aliases,
            description=truncate(desc_sentence),
            importance=_importance(lc),
            source_location=_location(lc.evidence, v),
            category=lc.category
            or (ConceptCategory.definition if (d or lc.definitional) else ConceptCategory.principle),
            mention_count=lc.count,
            provenance=Provenance.llm,
            confidence=conf,
            inferred=False,
            evidence=ev,
        )
        added.append(c)
        idx.add(c)
    a.concepts.extend(added)

    if not keep_heuristic_only:
        dropped = {c.name for c in a.concepts if id(c) in heuristic_ids and id(c) not in matched}
        if dropped:
            discarded["concept.heuristic_only_dropped"] += len(dropped)
            _prune(a, dropped)

    final_ids = {id(c) for c in a.concepts}

    # ---- definitions ----------------------------------------------------
    have = {_key(d.term): d for d in a.definitions}
    for lc_def in ex.definitions:
        concept = idx.find([lc_def.term])
        if concept is None or id(concept) not in final_ids:
            discarded["definition.unknown_concept"] += 1
            continue
        k = _key(concept.name)
        if k in have:
            old = have[k]
            if old.provenance == Provenance.heuristic and _similar(old.definition, lc_def.sentence):
                old.provenance = Provenance.heuristic_llm
                old.confidence = round(min(0.98, old.confidence + 0.05), 2)
            continue
        nd = Definition(
            term=concept.name,
            definition=truncate(lc_def.sentence, 300),
            source_location=_location(lc_def.evidence, v),
            provenance=Provenance.llm,
            confidence=0.8 if lc_def.evidence.match != "fuzzy" else 0.65,
            evidence=lc_def.evidence,
        )
        a.definitions.append(nd)
        have[k] = nd
        for sec in a.sections:
            if sec.id == lc_def.evidence.section_id:
                sec.has_definition = True

    # ---- section concept lists ------------------------------------------
    for c in added:
        for sec in a.sections:
            if sec.id == (c.source_location.section_id) and len(sec.concepts) < 8:
                if c.name not in sec.concepts:
                    sec.concepts.append(c.name)

    # ---- prerequisites --------------------------------------------------
    for pr in ex.prerequisites:
        child, parent = idx.find([pr.concept]), idx.find([pr.requires])
        if (
            child is None
            or parent is None
            or id(child) not in final_ids
            or id(parent) not in final_ids
        ):
            discarded["prerequisite.unknown_concept"] += 1
            continue
        if child is parent:
            discarded["prerequisite.self"] += 1
            continue
        if child.name in parent.prerequisite:
            discarded["prerequisite.cycle"] += 1
            continue
        link = next((l for l in child.prerequisite_links if l.name == parent.name), None)
        if link is not None:
            if link.provenance == Provenance.heuristic:
                link.provenance = Provenance.heuristic_llm
                link.confidence = 0.7
                link.evidence = pr.evidence
            continue
        if len(child.prerequisite) >= MAX_PREREQ:
            discarded["prerequisite.over_limit"] += 1
            continue
        child.prerequisite.append(parent.name)
        child.prerequisite_links.append(
            PrerequisiteLink(
                name=parent.name,
                provenance=Provenance.llm_inference,
                inferred=True,
                confidence=0.6 if pr.evidence else 0.4,
                evidence=pr.evidence,
            )
        )

    # ---- scope notes ----------------------------------------------------
    for ls in ex.scope_notes:
        dup = next((n for n in a.scope_notes if _similar(n.text, ls.sentence)), None)
        if dup is not None:
            if dup.provenance == Provenance.heuristic:
                dup.provenance = Provenance.heuristic_llm
                dup.confidence = round(min(0.98, dup.confidence + 0.05), 2)
            continue
        a.scope_notes.append(
            ScopeNote(
                text=truncate(ls.sentence, 300),
                source_location=_location(ls.evidence, v),
                provenance=Provenance.llm,
                confidence=0.75 if ls.evidence.match != "fuzzy" else 0.6,
                evidence=ls.evidence,
            )
        )

    a.concepts.sort(key=lambda c: (-c.importance, -c.mention_count, c.name))
    n_bad = sum(n for k, n in discarded.items() if k.endswith("evidence_not_found"))
    if n_bad:
        a.warnings.append(
            f"LLM이 제시한 항목 {n_bad}개는 원문에서 근거를 찾지 못해 제외했습니다."
        )
    return a


def _prune(a: SourceAnalysis, names: set[str]) -> None:
    a.concepts = [c for c in a.concepts if c.name not in names]
    a.definitions = [d for d in a.definitions if d.term not in names]
    for c in a.concepts:
        keep = [p for p in c.prerequisite if p not in names]
        c.prerequisite = keep
        c.prerequisite_links = [l for l in c.prerequisite_links if l.name in keep]
    for e in a.examples:
        e.related_concepts = [r for r in e.related_concepts if r not in names]
    for s in a.sections:
        s.concepts = [x for x in s.concepts if x not in names]
    for t in a.main_topics:
        t.key_concepts = [x for x in t.key_concepts if x not in names]


def apply_topic_labels(a: SourceAnalysis, labels: dict[int, TopicLabel]) -> None:
    """Replace topic titles by the (validated) LLM titles. Grouping is unchanged."""
    for i, topic in enumerate(a.main_topics):
        lab = labels.get(i)
        if lab is None:
            continue
        topic: MainTopic
        topic.heuristic_title = topic.title
        topic.title = lab.title
        if lab.concepts:
            topic.key_concepts = lab.concepts[:5]
        topic.provenance = Provenance.heuristic_llm
        topic.inferred = True  # a label is an interpretation, not quoted text
        topic.confidence = 0.6
