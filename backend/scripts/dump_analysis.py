"""Dev helper: parse + analyze a file and print the SourceAnalysis.

    python scripts/dump_analysis.py <file> [--brief] [--out result.txt]

`--out` writes UTF-8 directly (avoids Windows console code-page issues).
"""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.document_parser import DocumentParser  # noqa: E402
from app.services.lecture_analyzer import LectureAnalyzer  # noqa: E402


def render(analysis, brief: bool) -> str:
    if not brief:
        return json.dumps(analysis.model_dump(mode="json"), ensure_ascii=False, indent=2)
    out = io.StringIO()
    p = lambda *a: print(*a, file=out)  # noqa: E731
    p("TITLE:", analysis.title)
    p("SUMMARY:", analysis.summary)
    p("COMPLEXITY:", analysis.estimated_complexity.model_dump(mode="json"))
    p("\nMAIN TOPICS")
    for t in analysis.main_topics:
        p(" -", t.title, "|", t.summary, "|", t.key_concepts)
    p("\nCONCEPTS (%d)" % len(analysis.concepts))
    for c in analysis.concepts:
        p(
            f" [{c.importance}] {c.name} ({c.category.value}) x{c.mention_count} "
            f"aliases={c.aliases} prereq={c.prerequisite} @{c.source_location.section_title}"
        )
        p("      ", c.description)
    p("\nDEFINITIONS", [d.term for d in analysis.definitions])
    p("EXAMPLES")
    for e in analysis.examples:
        p(" -", e.kind, e.text[:90], e.related_concepts)
    p("IMPORTANT")
    for x in analysis.important_points:
        p(" -", x.text[:100])
    p("SCOPE")
    for x in analysis.scope_notes:
        p(" -", x.text[:100])
    p("WARNINGS", analysis.warnings)
    return out.getvalue()


def main() -> None:
    args = sys.argv[1:]
    path = Path(args[0])
    material = DocumentParser().parse(path, path.name, "dev")
    text = render(LectureAnalyzer().analyze(material), "--brief" in args)
    if "--out" in args:
        Path(args[args.index("--out") + 1]).write_text(text, encoding="utf-8")
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))


main()
