"""API level: ANALYZER_MODE wiring, fallback, and response schema (fake LLM only)."""

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.llm_client import LLMTimeout

from fake_llm import KOREAN_NARRATIVE, FakeLLM

EXTRACT = {
    "concepts": [
        {"name": "광합성", "category": "process", "quote": "광합성 덕분에 식물은 스스로 양분을 얻는다."},
        {"name": "엽록체", "category": "architecture", "quote": "엽록체 내부의 틸라코이드 막이 빛을 받아들인다."},
        {"name": "명반응", "category": "process", "quote": "명반응 단계에서 물이 분해되고 산소가 나온다."},
    ],
    "scope_notes": [{"quote": "세포 호흡은 이후에 설명한다."}],
}


def make_client(tmp_path: Path, mode: str, llm=None) -> TestClient:
    s = Settings(data_dir=tmp_path, max_upload_mb=5, cors_origins=(), analyzer_mode=mode)
    return TestClient(create_app(s, llm_client=llm), raise_server_exceptions=False)


def analyze(client: TestClient):
    p = client.post("/projects", json={}).json()
    up = client.post(
        f"/projects/{p['id']}/upload",
        files={"file": ("doc.md", io.BytesIO(KOREAN_NARRATIVE.encode("utf-8")), "text/plain")},
    )
    assert up.status_code == 200, up.text
    r = client.post(f"/projects/{p['id']}/analyze")
    return p["id"], r


def test_default_mode_is_heuristic_and_never_calls_the_llm(tmp_path):
    llm = FakeLLM(extract=EXTRACT)
    client = make_client(tmp_path, "heuristic", llm)
    _, r = analyze(client)
    assert r.status_code == 200
    a = r.json()["source_analysis"]
    assert a["analyzer"] == "heuristic-v1" and a["concepts"] == []
    assert a["analyzer_info"]["mode_used"] == "heuristic" and a["analyzer_info"]["fallback"] is False
    assert llm.calls == []


def test_hybrid_mode_adds_llm_concepts_with_provenance(tmp_path):
    llm = FakeLLM(extract=EXTRACT)
    client = make_client(tmp_path, "hybrid", llm)
    pid, r = analyze(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["presentation_status"] == "analyzed"
    a = body["source_analysis"]
    assert a["analyzer"] == "hybrid-v1"
    by_name = {c["name"]: c for c in a["concepts"]}
    assert {"광합성", "엽록체", "명반응"} <= set(by_name)
    c = by_name["광합성"]
    assert c["provenance"] == "llm" and c["inferred"] is False and 0 <= c["confidence"] <= 1
    assert set(c["evidence"]) >= {"quote", "page", "line_start", "line_end"}
    assert c["evidence"]["line_start"] >= 1
    assert a["scope_notes"][0]["evidence"]["quote"] == "세포 호흡은 이후에 설명한다."
    assert a["analyzer_info"]["llm_model"] == "fake-model"
    # existing API shape is untouched, GET /analysis returns the same persisted analysis
    assert client.get(f"/projects/{pid}/analysis").json()["analyzer"] == "hybrid-v1"
    disk = json.loads((tmp_path / "projects" / pid / "project.json").read_text(encoding="utf-8"))
    assert disk["source_analysis"]["analyzer_info"]["mode_used"] == "hybrid"


def test_hybrid_mode_without_api_key_still_analyzes(tmp_path):
    client = make_client(tmp_path, "hybrid", llm=None)  # no key, no client
    _, r = analyze(client)
    assert r.status_code == 200
    a = r.json()["source_analysis"]
    assert a["analyzer_info"]["fallback"] is True
    assert a["analyzer_info"]["fallback_reason"] == "not_configured"
    assert any("규칙 기반" in w for w in a["warnings"])
    assert r.json()["presentation_status"] == "analyzed"


def test_llm_timeout_does_not_fail_the_request(tmp_path):
    client = make_client(tmp_path, "hybrid", FakeLLM(error=LLMTimeout("slow")))
    _, r = analyze(client)
    assert r.status_code == 200
    info = r.json()["source_analysis"]["analyzer_info"]
    assert info["fallback"] is True and info["fallback_reason"] == "timeout"
    assert info["mode_requested"] == "hybrid" and info["mode_used"] == "heuristic"
    assert "Traceback" not in r.text


def test_unexpected_llm_exception_is_hidden_from_the_client(tmp_path):
    client = make_client(tmp_path, "llm", FakeLLM(error=RuntimeError("secret internal detail")))
    _, r = analyze(client)
    assert r.status_code == 200
    assert "secret internal detail" not in r.text
    assert r.json()["source_analysis"]["analyzer_info"]["fallback_reason"] == "error"
