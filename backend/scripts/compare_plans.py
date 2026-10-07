"""Compare LecturePlans for the SAME source under the spec's CASE A / B / C.

    python scripts/compare_plans.py <file> [--out result.txt] [--detail]

Prints a metric table (sections, slides, minutes per kind, definitions, examples,
practice, ...) and, with --detail, every section of every plan.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.lecture_profile import LectureOptionsInput  # noqa: E402
from app.services.document_parser import DocumentParser  # noqa: E402
from app.services.lecture_planner import LecturePlanner  # noqa: E402
from app.services.lecture_profile_service import build_profile  # noqa: E402
from app.services.lecture_analyzer import HeuristicAnalyzer  # noqa: E402

CASES = {
    "A theory/beginner/60": dict(
        audience_level="university_beginner", duration_minutes=60, difficulty="introductory",
        lecture_type="theory", explanation_depth="detailed", source_policy="source_first",
    ),
    "B practice/intermediate/60": dict(
        audience_level="university_intermediate", duration_minutes=60, difficulty="intermediate",
        lecture_type="practice", explanation_depth="standard", source_policy="source_first",
    ),
    "C professional/advanced/30": dict(
        audience_level="professional", duration_minutes=30, difficulty="advanced",
        lecture_type="theory", explanation_depth="concise", source_policy="source_first",
    ),
}


def plans_for(path: Path):
    material = DocumentParser().parse(path, path.name, source_id=uuid.uuid4().hex)
    analysis = HeuristicAnalyzer().analyze(material)
    out = {}
    for name, opts in CASES.items():
        profile = build_profile("p" * 32, LectureOptionsInput(**opts))
        out[name] = LecturePlanner().plan(analysis, profile)
    return analysis, out


def render(analysis, plans, detail: bool) -> str:
    L = [f"source: {analysis.title}  concepts={len(analysis.concepts)} topics={len(analysis.main_topics)}", ""]
    rows = [
        ("sections", lambda p: p.metrics.section_count),
        ("slides", lambda p: p.metrics.slide_count),
        ("concepts", lambda p: p.metrics.concept_count),
        ("definitions", lambda p: p.metrics.definition_count),
        ("examples", lambda p: p.metrics.example_count),
        ("practice activities", lambda p: p.metrics.practice_activity_count),
        ("practice steps", lambda p: p.metrics.practice_step_count),
        ("code items", lambda p: p.metrics.code_count),
        ("quiz questions", lambda p: p.metrics.quiz_question_count),
        ("explanation min", lambda p: p.metrics.explanation_minutes),
        ("practice min", lambda p: p.metrics.practice_minutes),
        ("kinds (min)", lambda p: dict(p.metrics.minutes_by_kind)),
    ]
    names = list(plans)
    L.append(f"{'':22}" + "".join(f"{n:>36}" for n in names))
    for label, fn in rows:
        L.append(f"{label:22}" + "".join(f"{str(fn(plans[n])):>36}" for n in names))
    for n, p in plans.items():
        L += ["", f"=== {n}  ({p.duration_minutes} min, {p.estimated_slide_count} slides)"]
        L += [f"  objective: {o}" for o in p.learning_objectives]
        L += [f"  warning: {w}" for w in p.warnings]
        if detail:
            for s in p.sections:
                L.append(
                    f"  {s.order:>2}. [{s.kind.value:<12}] {s.duration_minutes:>2}min {s.estimated_slides:>2}sl "
                    f"{s.title}  concepts={s.concepts} terms={len(s.terms)} ex={len(s.examples)} "
                    f"prac={len(s.practice)} code={len(s.code)} quiz={s.quiz.question_count if s.quiz else 0}"
                )
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--out")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    analysis, plans = plans_for(Path(a.file))
    text = render(analysis, plans, a.detail)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        print(text)
