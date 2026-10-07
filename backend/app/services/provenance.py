"""Provenance / confidence / evidence for rule-based (heuristic) results.

The heuristic analyzer only *extracts* sentences from the source, so its
evidence is simply the extracted text plus its location. Nothing here changes
what the analyzer found; it adds traceability metadata after the fact.
"""

from __future__ import annotations

from ..models.source import (
    Evidence,
    PrerequisiteLink,
    Provenance,
    SourceAnalysis,
    SourceLocation,
)


def evidence_from(text: str, loc: SourceLocation) -> Evidence:
    quote = text[:-1].rstrip() if text.endswith("…") else text  # still a verbatim prefix
    return Evidence(
        quote=quote,
        page=loc.page,
        line_start=loc.line,
        line_end=loc.line,
        section_id=loc.section_id,
        match="exact",
        match_score=1.0,
    )


def heuristic_concept_confidence(importance: int, has_definition: bool) -> float:
    """0.58 (importance 1) ... 0.95 (importance 5, defined). Deterministic."""
    return round(min(0.95, 0.5 + 0.08 * importance + (0.05 if has_definition else 0.0)), 2)


def annotate_heuristic(a: SourceAnalysis) -> SourceAnalysis:
    defined = {d.term for d in a.definitions}
    for c in a.concepts:
        c.provenance = Provenance.heuristic
        c.inferred = False
        c.confidence = heuristic_concept_confidence(c.importance, c.name in defined)
        c.evidence = evidence_from(c.description, c.source_location)
        c.prerequisite_links = [
            PrerequisiteLink(
                name=p, provenance=Provenance.heuristic, inferred=True, confidence=0.5
            )
            for p in c.prerequisite
        ]
    for d in a.definitions:
        d.provenance, d.confidence = Provenance.heuristic, 0.9
        d.evidence = evidence_from(d.definition, d.source_location)
    for e in a.examples:
        e.provenance, e.confidence = Provenance.heuristic, 0.8
        quote = e.text
        if e.kind == "case_study" and ": " in quote:
            # the analyzer prepends the section title ("{title}: {body}"); the
            # evidence quote must be source text only, so drop that prefix
            quote = quote.split(": ", 1)[1]
        e.evidence = evidence_from(quote, e.source_location)
    for p in a.important_points:
        p.provenance, p.confidence = Provenance.heuristic, 0.7
        p.evidence = evidence_from(p.text, p.source_location)
    for n in a.scope_notes:
        n.provenance, n.confidence = Provenance.heuristic, 0.85
        n.evidence = evidence_from(n.text, n.source_location)
    for t in a.main_topics:
        # grouping is an interpretation of the structure, not stated in the source
        t.provenance, t.confidence, t.inferred = Provenance.heuristic, 0.5, True
    return a
