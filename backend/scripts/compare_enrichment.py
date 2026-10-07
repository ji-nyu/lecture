"""STAGE 6A: how does the SAME slide read after LLM enrichment for profile A / B / C?

    python scripts/compare_enrichment.py <file> [--mode fixed|own] [--base A|B|C]
                                         [--concept MQTT] [--slides 9,11,6] [--out result.txt]

fixed (default): ONE SlideSpecification (built for --base, default B) is enriched under the
                 three profiles. The structure must come out identical; only the wording,
                 examples, presenter notes and visual instructions may differ.
own            : every profile gets its own plan and slide specification (different slide
                 counts, as in STAGE 4) and the slide of the same concept is compared.

Uses the offline MockLLMClient (no key, no network). It is a deterministic template writer,
so this shows the pipeline and the profile adaptation, not the prose quality of a real LLM.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.mock import MockLLMClient  # noqa: E402
from app.models.lecture_profile import LectureOptionsInput  # noqa: E402
from app.services.document_parser import DocumentParser  # noqa: E402
from app.services.lecture_analyzer import HeuristicAnalyzer  # noqa: E402
from app.services.lecture_planner import LecturePlanner  # noqa: E402
from app.services.lecture_profile_service import build_profile  # noqa: E402
from app.services.slide_content_enricher import SlideContentEnricher  # noqa: E402
from app.services.slide_planner import SlidePlanner  # noqa: E402

CASES = {
    "A": dict(audience_level="university_beginner", duration_minutes=60, difficulty="introductory",
              lecture_type="theory", explanation_depth="detailed", source_policy="source_first"),
    "B": dict(audience_level="university_intermediate", duration_minutes=60, difficulty="intermediate",
              lecture_type="practice", explanation_depth="standard", source_policy="source_first"),
    "C": dict(audience_level="professional", duration_minutes=30, difficulty="advanced",
              lecture_type="theory", explanation_depth="concise", source_policy="source_first"),
}
LABEL = {"A": "A 대학 초급 / 이론 중심", "B": "B 대학 중급 / 실습 중심", "C": "C 전문가 / 고급"}


def build(material, analysis, name):
    profile = build_profile("p" * 32, LectureOptionsInput(**CASES[name]))
    plan = LecturePlanner().plan(analysis, profile)
    return profile, plan, SlidePlanner().plan(plan, analysis)


def enrich(spec, plan, profile, analysis, material):
    return SlideContentEnricher(MockLLMClient()).enrich(spec, plan, profile, analysis, source_text=material.raw_text)


def structure(spec):
    return [(s.slide_number, s.section_id, s.slide_type.value, s.estimated_explanation_time, s.title) for s in spec.slides]


def show_slide(es, lines):
    e = es.enriched
    lines.append(f"    slide {es.slide_number} [{es.slide_type.value}] '{es.title}' {es.estimated_explanation_time}s  status={es.status.value}")
    if e is None:
        lines.append(f"    (fallback) {es.failure_message}")
        return
    lines.append(f"    display_title : {e.display_title}")
    lines.append(f"    key_message   : {e.key_message}")
    for p in e.body_points:
        lines.append(f"    body_point    : {p}")
    if e.explanation:
        lines.append(f"    explanation   : {e.explanation}")
    if e.analogy:
        lines.append(f"    analogy       : {e.analogy}")
    if e.example:
        lines.append(f"    example       : {e.example}")
    if e.practice_instruction:
        p = e.practice_instruction
        lines.append(f"    practice goal : {p.goal}")
        for i, s in enumerate(p.steps, 1):
            lines.append(f"      step {i}      : {s}")
        lines.append(f"      expected    : {p.expected_result}")
    if e.code_explanation:
        lines.append(f"    code          : {e.code_explanation.purpose}")
    if e.quiz_content:
        lines.append(f"    quiz          : {e.quiz_content.question}")
    if e.summary_message:
        lines.append(f"    summary       : {e.summary_message}")
    lines.append(f"    visual        : {e.visual_instruction}")
    notes = e.presenter_notes
    lines.append(f"    presenter     : ({len(notes) if notes else 0} chars) {notes}")
    lines.append("    provenance    : " + ", ".join(f"{k}={v.value}" for k, v in es.provenance.items()))
    if es.grounding.warnings:
        lines.append(f"    validator     : {es.grounding.warnings}")


def pick(spec, concept, kind):
    for s in spec.slides:
        if s.slide_type.value == kind and concept in (s.concepts or []) and ":" not in s.title:
            return s.slide_number
    for s in spec.slides:
        if s.slide_type.value == kind:
            return s.slide_number
    return None


def summary(results, lines):
    lines.append(f"{'':28}" + "".join(f"{LABEL[n][:26]:>28}" for n in results))
    def avg(r, get):
        vals = [get(s.enriched) for s in r.slides if s.enriched and get(s.enriched)]
        return round(sum(len(v) for v in vals) / len(vals), 1) if vals else 0

    def count(r, get):
        return sum(1 for s in r.slides if s.enriched and get(s.enriched))

    rows = {
        "slides": lambda r: r.slide_count,
        "enriched / failed": lambda r: f"{r.stats.enriched_count} / {r.stats.failed_count}",
        "presenter notes: slides": lambda r: count(r, lambda e: e.presenter_notes),
        "presenter notes: avg chars": lambda r: avg(r, lambda e: e.presenter_notes),
        "explanation: avg chars": lambda r: avg(r, lambda e: e.explanation),
        "body points: avg count": lambda r: avg(r, lambda e: e.body_points),
        "slides with example": lambda r: count(r, lambda e: e.example),
        "slides with analogy": lambda r: count(r, lambda e: e.analogy),
        "slides with practice steps": lambda r: count(r, lambda e: e.practice_instruction),
        "avg practice steps": lambda r: avg(r, lambda e: e.practice_instruction.steps if e.practice_instruction else None),
        "validation passed": lambda r: r.validation.passed,
    }
    for label, fn in rows.items():
        lines.append(f"{label:28}" + "".join(f"{str(fn(r)):>28}" for r in results.values()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--mode", choices=["fixed", "own"], default="fixed")
    ap.add_argument("--base", choices=list(CASES), default="B")
    ap.add_argument("--concept", default="MQTT")
    ap.add_argument("--slides", help="comma separated slide numbers (fixed mode)")
    ap.add_argument("--out")
    a = ap.parse_args()

    path = Path(a.file)
    material = DocumentParser().parse(path, path.name, source_id=uuid.uuid4().hex)
    analysis = HeuristicAnalyzer().analyze(material)
    lines = [f"source: {analysis.title}   mode: {a.mode}   (MockLLMClient: deterministic offline writer)", ""]

    if a.mode == "fixed":
        _, base_plan, base_spec = build(material, analysis, a.base)
        lines.append(f"one SlideSpecification (profile {a.base}): {base_spec.slide_count} slides, {base_spec.duration_minutes} min")
        results = {}
        for name in CASES:
            profile, _, _ = build(material, analysis, name)
            results[name] = enrich(base_spec, base_plan, profile, analysis, material)
        same = len({tuple(structure_of(r)) for r in results.values()}) == 1
        lines.append(f"structure identical across A/B/C (number, section, type, seconds, title): {same}")
        lines.append("")
        summary(results, lines)
        numbers = (
            [int(x) for x in a.slides.split(",")] if a.slides
            else [n for n in (pick(base_spec, a.concept, "concept"), pick(base_spec, a.concept, "architecture"), pick(base_spec, a.concept, "practice")) if n]
        )
        for n in numbers:
            lines += ["", "=" * 100, f"SLIDE {n}", "=" * 100]
            for name, r in results.items():
                lines.append(f"  [{LABEL[name]}]")
                show_slide(r.slides[n - 1], lines)
                lines.append("")
    else:
        results = {}
        for name in CASES:
            profile, plan, spec = build(material, analysis, name)
            results[name] = (spec, enrich(spec, plan, profile, analysis, material))
        summary({k: v[1] for k, v in results.items()}, lines)
        for name, (spec, r) in results.items():
            n = pick(spec, a.concept, "concept")
            lines += ["", f"[{LABEL[name]}]  ({spec.slide_count} slides)"]
            if n:
                show_slide(r.slides[n - 1], lines)
    text = "\n".join(lines)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(a.out)
    else:
        print(text)


def structure_of(r):
    return [(s.slide_number, s.section_id, s.slide_type.value, s.estimated_explanation_time, s.title) for s in r.slides]


if __name__ == "__main__":
    main()
