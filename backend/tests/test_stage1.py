import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BASIC = {
    "audience_level": "university_intermediate",
    "duration_minutes": 60,
    "difficulty": "intermediate",
    "lecture_type": "practice",
    "explanation_depth": "detailed",
    "source_policy": "source_first",
}


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, max_upload_mb=1, cors_origins=())
    return TestClient(create_app(settings), raise_server_exceptions=False)


def _new_project(client, title=None):
    r = client.post("/projects", json={"title": title} if title else {})
    assert r.status_code == 201
    return r.json()


def _upload(client, pid, name="mqtt.txt", data=b"MQTT is a lightweight protocol."):
    return client.post(
        f"/projects/{pid}/upload", files={"file": (name, io.BytesIO(data), "text/plain")}
    )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_get_project(client):
    p = _new_project(client)
    assert p["presentation_status"] is None
    assert client.get(f"/projects/{p['id']}").json()["id"] == p["id"]
    assert len(client.get("/projects").json()) == 1


def test_project_not_found_and_traversal(client):
    assert client.get("/projects/" + "0" * 32).status_code == 404
    assert client.get("/projects/..%2F..%2Fetc").status_code == 404


def test_upload_saves_file_and_updates_state(client, tmp_path):
    p = _new_project(client)
    r = _upload(client, p["id"])
    assert r.status_code == 200
    body = r.json()
    assert body["presentation_status"] == "uploaded"
    assert body["title"] == "mqtt"  # derived from filename
    assert body["source_file"]["filename"] == "mqtt.txt"
    assert "source_file_path" not in body  # no server paths leaked
    saved = tmp_path / "projects" / p["id"] / "source" / "source.txt"
    assert saved.read_bytes() == b"MQTT is a lightweight protocol."


def test_upload_keeps_explicit_title_and_replaces_file(client, tmp_path):
    p = _new_project(client, "My Lecture")
    _upload(client, p["id"], "a.txt", b"first")
    r = _upload(client, p["id"], "b.md", b"# second")
    assert r.json()["title"] == "My Lecture"
    src = tmp_path / "projects" / p["id"] / "source"
    assert [f.name for f in src.iterdir()] == ["source.md"]


@pytest.mark.parametrize("name", ["a.pdf", "a.pptx", "a.docx", "a.txt", "a.md", "a.ppt"])
def test_accepted_formats(client, name):
    p = _new_project(client)
    assert _upload(client, p["id"], name, b"x").status_code == 200


def test_unsupported_type(client):
    p = _new_project(client)
    r = _upload(client, p["id"], "evil.exe", b"x")
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "UnsupportedFileType"


def test_empty_file(client):
    p = _new_project(client)
    r = _upload(client, p["id"], "a.txt", b"")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "EmptyDocument"


def test_too_large(client):
    p = _new_project(client)
    r = _upload(client, p["id"], "a.txt", b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413
    assert client.get(f"/projects/{p['id']}").json()["source_file"] is None


def test_path_traversal_filename_is_sanitized(client, tmp_path):
    p = _new_project(client)
    r = _upload(client, p["id"], "../../evil.txt", b"x")
    assert r.status_code == 200
    assert r.json()["source_file"]["filename"] == "evil.txt"
    assert not (tmp_path / "evil.txt").exists()


def test_profile_created_persisted_and_inferred(client, tmp_path):
    p = _new_project(client)
    r = client.put(f"/projects/{p['id']}/profile", json=BASIC)
    assert r.status_code == 200
    prof = r.json()
    for k, v in BASIC.items():
        assert prof[k] == v
    # spec example: practice + detailed => full / medium / medium / full
    assert prof["practice_level"] == "full"
    assert prof["example_level"] == "medium"
    assert prof["visual_level"] == "medium"
    assert prof["speaker_notes"] == "full"
    assert "practice_level" in prof["inferred_fields"]

    # persisted on disk as JSON
    disk = json.loads(
        (tmp_path / "projects" / p["id"] / "project.json").read_text(encoding="utf-8")
    )
    assert disk["lecture_profile"]["id"] == prof["id"]
    assert client.get(f"/projects/{p['id']}/profile").json() == prof
    assert client.get(f"/projects/{p['id']}").json()["lecture_profile"] == prof


def test_profile_explicit_advanced_not_overridden_and_id_stable(client):
    p = _new_project(client)
    first = client.put(f"/projects/{p['id']}/profile", json=BASIC).json()
    second = client.put(
        f"/projects/{p['id']}/profile",
        json={**BASIC, "practice_level": "simple", "quiz_mode": "final"},
    ).json()
    assert second["id"] == first["id"]
    assert second["practice_level"] == "simple"
    assert "practice_level" not in second["inferred_fields"]
    assert second["quiz_mode"] == "final"


def test_profile_different_options_give_different_inference(client):
    p = _new_project(client)
    theory = client.put(
        f"/projects/{p['id']}/profile",
        json={**BASIC, "lecture_type": "theory", "explanation_depth": "concise"},
    ).json()
    assert theory["practice_level"] == "none"
    assert theory["code_level"] == "none"
    assert theory["speaker_notes"] == "concise"
    assert theory["slide_density"] == "concise"


@pytest.mark.parametrize(
    "patch",
    [
        {"audience_level": "martian"},
        {"duration_minutes": 0},
        {"duration_minutes": "abc"},
        {"unknown": 1},
    ],
)
def test_profile_invalid_input(client, patch):
    p = _new_project(client)
    r = client.put(f"/projects/{p['id']}/profile", json={**BASIC, **patch})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "InvalidLectureProfile"


def test_profile_missing_basic_option(client):
    p = _new_project(client)
    body = {k: v for k, v in BASIC.items() if k != "difficulty"}
    r = client.put(f"/projects/{p['id']}/profile", json=body)
    assert r.status_code == 422
    assert "difficulty" in " ".join(r.json()["error"]["details"])


def test_profile_conflict(client):
    p = _new_project(client)
    r = client.put(
        f"/projects/{p['id']}/profile", json={**BASIC, "practice_level": "none"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "InvalidLectureProfile"


def test_profile_on_missing_project(client):
    assert client.put("/projects/" + "1" * 32 + "/profile", json=BASIC).status_code == 404


def test_option_schema_matches_backend(client):
    s = client.get("/lecture-options").json()
    assert len(s["basic"]) == 6
    assert "university_beginner" in s["values"]["audience_level"]
    assert s["values"]["source_policy"] == ["source_only", "source_first", "expanded"]
    assert "video_intro" in s["advanced"]
    assert s["values"]["video_intro"] == ["none", "include"]


def test_no_stack_trace_on_unexpected_error(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internals")

    monkeypatch.setattr("app.storage.project_store.ProjectStore.list_projects", boom)
    r = client.get("/projects")
    assert r.status_code == 500
    assert "secret" not in r.text and "Traceback" not in r.text
