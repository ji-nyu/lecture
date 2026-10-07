import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

DOC_DIR = Path(__file__).resolve().parents[2] / "testdocument"
MQTT_DOCS = sorted(DOC_DIR.glob("*.txt")) if DOC_DIR.exists() else []

SAMPLE = (
    "# 샘플 강의\n\n이 자료는 샘플이다.\n\n## 1. Broker란 무엇인가\n\n"
    "Broker는 메시지를 중계하는 서버이다.\nBroker는 Client 연결을 관리한다.\n\n"
    "## 2. Topic의 역할\n\nTopic은 메시지를 분류하는 이름이다.\nBroker와 Topic은 함께 쓰인다.\n"
)


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, max_upload_mb=5, cors_origins=())
    return TestClient(create_app(settings), raise_server_exceptions=False)


def _project(client, name="s.md", data=SAMPLE.encode("utf-8")):
    p = client.post("/projects", json={}).json()
    r = client.post(
        f"/projects/{p['id']}/upload", files={"file": (name, io.BytesIO(data), "text/plain")}
    )
    assert r.status_code == 200, r.text
    return p["id"]


def test_analyze_end_to_end_status_and_persistence(client, tmp_path):
    pid = _project(client)
    r = client.post(f"/projects/{pid}/analyze")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["presentation_status"] == "analyzed"
    assert body["has_analysis"] is True
    a = body["source_analysis"]
    assert {c["name"] for c in a["concepts"]} >= {"Broker", "Topic"}
    assert a["main_topics"] and a["sections"]

    # persisted: analysis in project.json, parsed material as separate artifact
    disk = json.loads((tmp_path / "projects" / pid / "project.json").read_text(encoding="utf-8"))
    assert disk["source_analysis"]["source_id"]
    material = json.loads(
        (tmp_path / "projects" / pid / "source_material.json").read_text(encoding="utf-8")
    )
    assert material["id"] == disk["source_analysis"]["source_id"]
    assert material["sections"][1]["title"] == "1. Broker란 무엇인가"

    # GET endpoints
    assert client.get(f"/projects/{pid}/analysis").json()["title"] == "샘플 강의"
    assert client.get(f"/projects/{pid}").json()["source_analysis"]["title"] == "샘플 강의"


def test_list_omits_large_analysis_but_flags_it(client):
    pid = _project(client)
    client.post(f"/projects/{pid}/analyze")
    item = client.get("/projects").json()[0]
    assert item["has_analysis"] is True and item["source_analysis"] is None


def test_analyze_without_upload_is_409(client):
    p = client.post("/projects", json={}).json()
    r = client.post(f"/projects/{p['id']}/analyze")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SourceNotUploaded"


def test_get_analysis_before_analyze_is_409(client):
    pid = _project(client)
    r = client.get(f"/projects/{pid}/analysis")
    assert r.status_code == 409 and r.json()["error"]["code"] == "AnalysisNotReady"


def test_analyze_unknown_project_404(client):
    assert client.post("/projects/" + "a" * 32 + "/analyze").status_code == 404


def test_legacy_ppt_marks_failed_with_readable_message_and_recovers(client):
    pid = _project(client, "old.ppt", b"\xd0\xcf\x11\xe0legacy")
    r = client.post(f"/projects/{pid}/analyze")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DocumentParsingError"
    assert "PPTX" in r.json()["error"]["message"]
    proj = client.get(f"/projects/{pid}").json()
    assert proj["presentation_status"] == "failed"
    assert "PPTX" in proj["error_message"]

    # uploading a valid file resets the failure and analysis works again
    client.post(
        f"/projects/{pid}/upload",
        files={"file": ("s.md", io.BytesIO(SAMPLE.encode("utf-8")), "text/plain")},
    )
    assert client.get(f"/projects/{pid}").json()["presentation_status"] == "uploaded"
    assert client.post(f"/projects/{pid}/analyze").status_code == 200


def test_whitespace_only_file_is_empty_document_error(client):
    pid = _project(client, "blank.txt", b"   \n\n  ")
    r = client.post(f"/projects/{pid}/analyze")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "EmptyDocument"
    assert client.get(f"/projects/{pid}").json()["presentation_status"] == "failed"


def test_corrupted_pdf_does_not_leak_traceback(client):
    pid = _project(client, "bad.pdf", b"not a pdf")
    r = client.post(f"/projects/{pid}/analyze")
    assert r.status_code == 422
    assert "Traceback" not in r.text and "pypdf" not in r.text


def test_reupload_discards_previous_analysis(client):
    pid = _project(client)
    client.post(f"/projects/{pid}/analyze")
    client.post(
        f"/projects/{pid}/upload",
        files={"file": ("n.md", io.BytesIO("# 다른 자료\n\n내용이다.\n".encode()), "text/plain")},
    )
    proj = client.get(f"/projects/{pid}").json()
    assert proj["has_analysis"] is False and proj["presentation_status"] == "uploaded"


def test_profile_and_analysis_coexist(client):
    pid = _project(client)
    client.post(f"/projects/{pid}/analyze")
    prof = {
        "audience_level": "university_beginner", "duration_minutes": 60,
        "difficulty": "introductory", "lecture_type": "theory",
        "explanation_depth": "detailed", "source_policy": "source_first",
    }
    assert client.put(f"/projects/{pid}/profile", json=prof).status_code == 200
    proj = client.get(f"/projects/{pid}").json()
    assert proj["lecture_profile"] and proj["source_analysis"]
    assert proj["presentation_status"] == "analyzed"  # saving options does not reset status


@pytest.mark.skipif(not MQTT_DOCS, reason="testdocument not present")
def test_real_mqtt_document_through_api(client):
    f = MQTT_DOCS[0]
    pid = _project(client, f.name, f.read_bytes())
    r = client.post(f"/projects/{pid}/analyze")
    assert r.status_code == 200
    a = r.json()["source_analysis"]
    names = {c["name"] for c in a["concepts"]}
    assert {"MQTT", "Publisher", "Subscriber", "Broker", "Topic", "QoS"} <= names
    assert 3 <= len(a["main_topics"]) <= 8
