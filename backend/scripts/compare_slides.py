"""Show the SlideSpecification of the SAME source under the spec's CASE A / B / C.

    python scripts/compare_slides.py <file> [--case A|B|C] [--full] [--out result.txt]

Without --full: a metric table and one line per slide (number, type, seconds, title,
learning purpose). With --full: also key message, key points and the visual /
presenter instructions of every slide.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.lecture_profile import LectureOptionsInput  # noqa: E402
from app.services.document_parser import DocumentParser  # noqa: E402
from app.services.lecture_analyzer import HeuristicAnalyzer  # noqa: E402
from app.services.lecture_planner import LecturePlanner  # noqa: E402
from app.services.lecture_profile_service import build_profile  # noqa: E402
from app.services.slide_planner import SlidePlanner  # noqa: E402

CASES = {
    "A": dict(audience_level="university_beginner", duration_minutes=60, difficulty="introductory",
              lecture_type="theory", explanation_depth="detailed", source_policy="source_first"),
    "B": dict(audience_level="university_intermediate", duration_minutes=60, difficulty="intermediate",
              lecture_type="practice", explanation_depth="standard", source_policy="source_first"),
    "C": dict(audience_level="professional", duration_minutes=30, difficulty="advanced",
              lecture_type="theory", explanation_depth="concise", source_policy="source_first"),
}


def specs_for(path: Path, cases):
    material = DocumentParser().parse(path, path.name, source_id=uuid.uuid4().hex)
    analysis = HeuristicAnalyzer().analyze(material)
    out = {}
    for name in cases:
        profile = build_profile("p" * 32, LectureOptionsInput(**CASES[name]))
        plan = LecturePlanner().plan(analysis, profile)
        out[name] = (plan, SlidePlanner().plan(plan, analysis))
    return analysis, out


def render(analysis, specs, full: bool) -> str:
    L = [f"source: {analysis.title}", ""]
    rows = [
        ("plan slides", lambda p, s: p.estimated_slide_count),
        ("spec slides", lambda p, s: s.slide_count),
        ("total seconds", lambda p, s: s.metrics.total_seconds),
        ("avg key points", lambda p, s: s.metrics.avg_key_points),
        ("visual slides", lambda p, s: s.metrics.visual_slide_count),
        ("source slides", lambda p, s: s.metrics.source_slide_count),
        ("by type", lambda p, s: dict(s.metrics.slides_by_type)),
    ]
    names = list(specs)
    L.append(f"{'':16}" + "".join(f"{n:>44}" for n in names))
    for label, fn in rows:
        L.append(f"{label:16}" + "".join(f"{str(fn(*specs[n]))[:43]:>44}" for n in names))
    for n, (plan, spec) in specs.items():
        L += ["", f"=== CASE {n}: {plan.duration_minutes} min, {spec.slide_count} slides"]
        L += [f"  warning: {w}" for w in spec.warnings]
        for s in spec.slides:
            L.append(f"{s.slide_number:>3}. [{s.slide_type.value:<12}] {s.estimated_explanation_time:>4}s  "
                     f"{s.title}\n       purpose: {s.learning_purpose}")
            if full:
                L.append(f"       message: {s.key_message}  ({s.content_origin.value})")
                L.append(f"       points : {s.key_points}")
                L.append(f"       visual : {s.visual_instruction}")
                L.append(f"       present: {s.presenter_instruction}")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--case", choices=list(CASES))
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    analysis, specs = specs_for(Path(a.file), [a.case] if a.case else list(CASES))
    text = render(analysis, specs, a.full)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        print(text)
