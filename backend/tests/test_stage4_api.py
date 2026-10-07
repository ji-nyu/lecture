"""STAGE 4/5 over HTTP: plan -> slides -> approve, statuses, invalidation, errors."""

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.project import Project
from app.services.slide_planner import SlidePlanner

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


def planned(client, **opts):
    pid = new_project(client)
    put_profile(client, pid, **opts)
    assert client.post(f"/projects/{pid}/plan").status_code == 200
    return pid


def project(client, pid):
    return client.get(f"/projects/{pid}").json()


def test_create_and_read_slides(client):
    pid = planned(client)
    r = client.post(f"/projects/{pid}/slides")
    assert r.status_code == 200, r.text
    spec = r.json()
    plan = client.get(f"/projects/{pid}/plan").json()
    assert spec["lecture_id"] == plan["id"] and spec["project_id"] == pid
    assert spec["slide_count"] == len(spec["slides"]) == plan["estimated_slide_count"]
    assert sum(s["estimated_explanation_time"] for s in spec["slides"]) == plan["duration_minutes"] * 60
    for s in spec["slides"]:
        for f in ("slide_number", "section_id", "title", "slide_type", "learning_purpose", "key_message", "key_points",
                  "source_reference", "visual_instruction", "presenter_instruction", "estimated_explanation_time"):
            assert f in s
        assert s["learning_purpose"].strip()
    assert client.get(f"/projects/{pid}/slides").json() == spec
    assert project(client, pid)["has_slides"] is True
    assert project(client, pid)["presentation_status"] == "planned"  # slides do not change the status


def test_slides_use_the_source_for_sentences_and_locations(client):
    pid = planned(client, code_level="snippet")
    spec = client.post(f"/projects/{pid}/slides").json()
    code = next(s for s in spec["slides"] if s["slide_type"] == "code")
    assert code["source_reference"]["section_title"] == "Publish 실습"
    assert any(s["message_from_source"] for s in spec["slides"])


def test_slides_survive_a_server_restart(client):
    pid = planned(client)
    spec = client.post(f"/projects/{pid}/slides").json()
    data_dir = client.app.state.settings.data_dir
    again = TestClient(create_app(Settings(data_dir=data_dir, max_upload_mb=5, cors_origins=())))
    assert again.get(f"/projects/{pid}/slides").json() == spec


def test_slides_need_a_plan_and_are_readable_errors(client):
    pid = new_project(client)
    r = client.post(f"/projects/{pid}/slides")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PlanNotReady"
    r = client.get(f"/projects/{pid}/slides")
    assert r.status_code == 409 and r.json()["error"]["code"] == "SlidesNotReady"
    assert "Traceback" not in r.text
    assert client.get("/projects/" + "0" * 32 + "/slides").status_code == 404


def test_different_options_give_different_slides_over_the_api(client):
    pid = new_project(client)
    put_profile(client, pid, **CASE_A)
    client.post(f"/projects/{pid}/plan")
    a = client.post(f"/projects/{pid}/slides").json()
    put_profile(client, pid, **CASE_B)
    client.post(f"/projects/{pid}/plan")
    b = client.post(f"/projects/{pid}/slides").json()
    assert a["metrics"]["slides_by_type"] != b["metrics"]["slides_by_type"]
    assert [s["title"] for s in a["slides"]] != [s["title"] for s in b["slides"]]


# ------------------------------------------------------------------- approval
def test_approve_marks_the_project_ready_to_generate(client):
    pid = planned(client)
    client.post(f"/projects/{pid}/slides")
    r = client.post(f"/projects/{pid}/plan/approve")
    assert r.status_code == 200, r.text
    assert r.json()["presentation_status"] == "ready_to_generate"
    assert project(client, pid)["presentation_status"] == "ready_to_generate"
    # idempotent
    assert client.post(f"/projects/{pid}/plan/approve").status_code == 200


def test_approve_needs_plan_and_slides(client):
    pid = new_project(client)
    r = client.post(f"/projects/{pid}/plan/approve")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PlanNotReady"
    put_profile(client, pid)
    client.post(f"/projects/{pid}/plan")
    r = client.post(f"/projects/{pid}/plan/approve")
    assert r.status_code == 409 and r.json()["error"]["code"] == "SlidesNotReady"
    assert project(client, pid)["presentation_status"] == "planned"


def test_approval_is_withdrawn_by_option_change_replan_and_new_slides(client):
    def approved():
        pid = planned(client)
        client.post(f"/projects/{pid}/slides")
        client.post(f"/projects/{pid}/plan/approve")
        assert project(client, pid)["presentation_status"] == "ready_to_generate"
        return pid

    pid = approved()  # Edit Option
    put_profile(client, pid, duration_minutes=30)
    st = project(client, pid)
    assert st["presentation_status"] == "analyzed" and not st["has_plan"] and not st["has_slides"]
    assert client.get(f"/projects/{pid}/slides").status_code == 409

    pid = approved()  # Regenerate Plan: slides and approval go with the old plan
    assert client.post(f"/projects/{pid}/plan").status_code == 200
    st = project(client, pid)
    assert st["presentation_status"] == "planned" and st["has_plan"] and not st["has_slides"]

    pid = approved()  # new slides for the same plan need a new approval
    assert client.post(f"/projects/{pid}/slides").status_code == 200
    assert project(client, pid)["presentation_status"] == "planned"


def test_reanalysis_drops_plan_and_slides(client):
    pid = planned(client)
    client.post(f"/projects/{pid}/slides")
    assert client.post(f"/projects/{pid}/analyze").status_code == 200
    st = project(client, pid)
    assert not st["has_plan"] and not st["has_slides"]


# --------------------------------------------------------------------- errors
def test_slide_planning_failure_keeps_the_plan_and_explains(client, monkeypatch):
    from app.errors import SlidePlanningError

    pid = planned(client)

    def boom(self, plan, analysis=None):
        raise SlidePlanningError("슬라이드 구성을 만들 수 없습니다: 테스트")

    monkeypatch.setattr(SlidePlanner, "plan", boom)
    r = client.post(f"/projects/{pid}/slides")
    assert r.status_code == 422 and r.json()["error"]["code"] == "SlidePlanningError"
    st = project(client, pid)
    assert st["has_plan"] is True and st["has_slides"] is False and "테스트" in (st["error_message"] or "")
    assert st["presentation_status"] == "planned"
    monkeypatch.undo()
    assert client.post(f"/projects/{pid}/slides").status_code == 200
    assert project(client, pid)["error_message"] is None


def test_unexpected_crash_is_not_leaked(client, monkeypatch):
    pid = planned(client)
    monkeypatch.setattr(SlidePlanner, "_section_seeds", lambda *a, **k: 1 / 0)
    r = client.post(f"/projects/{pid}/slides")
    assert r.status_code in (422, 500) and "Traceback" not in r.text and "ZeroDivision" not in r.text


def test_old_project_files_without_slides_still_load(client):
    pid = planned(client)
    path = client.app.state.settings.data_dir / "projects" / pid / "project.json"
    if not path.exists():
        pytest.skip("project file layout differs")
    d = json.loads(path.read_text(encoding="utf-8"))
    d.pop("slide_specification", None)
    path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert Project.model_validate(d).slide_specification is None
    assert project(client, pid)["has_slides"] is False
