"""Live smoke test against a running server (real HTTP, Korean filename).

    python scripts/live_smoke.py http://127.0.0.1:8011 ../testdocument/<file>.txt <out.txt>
"""

import sys
from pathlib import Path

import httpx

base, doc, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
lines = []
with httpx.Client(base_url=base, timeout=30) as c:
    pid = c.post("/projects", json={}).json()["id"]
    with open(doc, "rb") as fh:
        r = c.post(f"/projects/{pid}/upload", files={"file": (doc.name, fh, "text/plain")})
    lines.append(f"upload -> {r.status_code} filename={r.json()['source_file']['filename']} title={r.json()['title']}")
    r = c.post(f"/projects/{pid}/analyze")
    body = r.json()
    a = body["source_analysis"]
    lines.append(f"analyze -> {r.status_code} status={body['presentation_status']} time={r.elapsed.total_seconds():.3f}s")
    lines.append(f"concepts({len(a['concepts'])}): " + ", ".join(c["name"] for c in a["concepts"]))
    lines.append(f"topics={len(a['main_topics'])} definitions={len(a['definitions'])} examples={len(a['examples'])} scope_notes={len(a['scope_notes'])}")
    lines.append(f"complexity={a['estimated_complexity']}")
    r = c.put(
        f"/projects/{pid}/profile",
        json={"audience_level": "university_beginner", "duration_minutes": 60, "difficulty": "introductory",
              "lecture_type": "theory", "explanation_depth": "detailed", "source_policy": "source_first"},
    )
    lines.append(f"profile -> {r.status_code}")
    r = c.get(f"/projects/{pid}")
    lines.append(f"reload -> analysis={r.json()['has_analysis']} profile={r.json()['lecture_profile'] is not None} status={r.json()['presentation_status']}")
out.write_text("\n".join(lines), encoding="utf-8")
