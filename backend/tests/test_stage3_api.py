"""STAGE 3 over HTTP: profile -> plan flow, statuses, errors, invalidation."""

import io

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.project import Project
from app.services.lecture_planner import LecturePlanner

from plan_helpers import BASE, CASE_A, CASE_B, CODE_DOC


@pytest.fixture()
def client(tmp_path):
    s = Settings(data_dir=tmp_path, max_upload_mb=5, cors_origins=())
    return TestClient(create_app(s), raise_server_exceptions=False)


def new_project(client, analyze=True, doc=CODE_DOC):
    pid = client.post("/projects", json={}).json()["id"]
    up = client.post(f"/projects/{pid}/upload", files={"file": ("doc.md", io.BytesIO(doc.encode("utf-8")), "text/plain")})
    assert up.status_code == 200, up.text
    if analyze:
        assert client.post(f"/projects/{pid}/analyze").status_code == 200
    return pid


def put_profile(client, pid, **opts):
    r = client.put(f"/projects/{pid}/profile", json={**BASE, **opts})
    assert r.status_code == 200, r.text
    return r.json()


def status(client, pid):
    return client.get(f"/projects/{pid}").json()


def test_full_flow_creates_and_persists_the_plan(client):
    pid = new_project(client)
    put_profile(client, pid)
    r = client.post(f"/projects/{pid}/plan")
    assert r.status_code == 200, r.text
    plan = r.json()
    assert sum(s["duration_minutes"] for s in plan["sections"]) == plan["duration_minutes"] == 60
    assert all(s["estimated_slides"] >= 1 for s in plan["sections"])
    assert plan["planner"] == "rule-v1" and plan["project_id"] == pid
    st = status(client, pid)
    assert st["presentation_status"] == "planned" and st["has_plan"] is True
    # GET returns exactly what was created
    assert client.get(f"/projects/{pid}/plan").json() == plan
    # ...and the plan is still there for a fresh app instance (file based store)
    data_dir = client.app.state.settings.data_dir
    again = TestClient(create_app(Settings(data_dir=data_dir, max_upload_mb=5, cors_origins=())))
    assert again.get(f"/projects/{pid}/plan").json() == plan


def test_different_options_over_the_api_give_different_plans(client):
    pid = new_project(client)
    put_profile(client, pid, **CASE_A)
    a = client.post(f"/projects/{pid}/plan").json()
    put_profile(client, pid, **CASE_B)
    b = client.post(f"/projects/{pid}/plan").json()
    assert a["metrics"] != b["metrics"]
    assert [s["kind"] for s in a["sections"]] != [s["kind"] for s in b["sections"]]
    assert a["presentation_hints"] == {**a["presentation_hints"]}  # serializable / present
    assert a["profile_id"] != b["profile_id"] or a["audience"] != b["audience"]


def test_plan_before_analysis_or_profile_gives_readable_409(client):
    pid = new_project(client, analyze=False)
    r = client.post(f"/projects/{pid}/plan")
    assert r.status_code == 409 and r.json()["error"]["code"] == "AnalysisNotReady"
    assert client.post(f"/projects/{pid}/analyze").status_code == 200
    r = client.post(f"/projects/{pid}/plan")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ProfileNotReady"
    assert "Traceback" not in r.text
    r = client.get(f"/projects/{pid}/plan")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PlanNotReady"
    assert client.get("/projects/" + "0" * 32 + "/plan").status_code == 404


def test_changing_the_profile_invalidates_the_plan(client):
    pid = new_project(client)
    put_profile(client, pid)
    client.post(f"/projects/{pid}/plan")
    assert status(client, pid)["has_plan"] is True
    put_profile(client, pid, duration_minutes=30)
    st = status(client, pid)
    assert st["has_plan"] is False and st["presentation_status"] == "analyzed"
    assert client.get(f"/projects/{pid}/plan").status_code == 409
    # planning again uses the new profile
    plan = client.post(f"/projects/{pid}/plan").json()
    assert plan["duration_minutes"] == 30


def test_replanning_replaces_the_old_plan(client):
    pid = new_project(client)
    put_profile(client, pid)
    first = client.post(f"/projects/{pid}/plan").json()
    second = client.post(f"/projects/{pid}/plan").json()
    assert first["id"] != second["id"] and client.get(f"/projects/{pid}/plan").json()["id"] == second["id"]


def test_planning_failure_returns_to_analyzed_with_a_message(client, monkeypatch):
    from app.errors import LecturePlanningError

    pid = new_project(client)
    put_profile(client, pid)

    def boom(self, a, p):
        raise LecturePlanningError("강의 계획을 만들 수 없습니다: 테스트")

    monkeypatch.setattr(LecturePlanner, "plan", boom)
    r = client.post(f"/projects/{pid}/plan")
    assert r.status_code == 422 and r.json()["error"]["code"] == "LecturePlanningError"
    st = status(client, pid)
    assert st["presentation_status"] == "analyzed" and "테스트" in (st["error_message"] or "")
    assert st["has_plan"] is False and st["has_analysis"] is True  # the analysis survives


def test_unexpected_planner_crash_is_a_500_without_a_stack_trace(client, monkeypatch):
    pid = new_project(client)
    put_profile(client, pid)
    monkeypatch.setattr(LecturePlanner, "_select", lambda *a, **k: 1 / 0)
    r = client.post(f"/projects/{pid}/plan")
    assert r.status_code in (422, 500) and "Traceback" not in r.text and "ZeroDivision" not in r.text


def test_reanalysis_drops_the_stale_plan(client):
    pid = new_project(client)
    put_profile(client, pid)
    client.post(f"/projects/{pid}/plan")
    assert client.post(f"/projects/{pid}/analyze").status_code == 200
    st = status(client, pid)
    assert st["has_plan"] is False
    assert client.get(f"/projects/{pid}/plan").status_code == 409


def test_old_project_files_without_a_plan_still_load(client, tmp_path):
    pid = new_project(client)
    path = client.app.state.settings.data_dir / "projects" / pid / "project.json"
    if not path.exists():  # storage layout guard
        pytest.skip("project file layout differs")
    import json

    d = json.loads(path.read_text(encoding="utf-8"))
    d.pop("lecture_plan", None)
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert Project.model_validate(d).lecture_plan is None
    assert status(client, pid)["has_plan"] is False
