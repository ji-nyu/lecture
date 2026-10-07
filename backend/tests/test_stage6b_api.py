"""STAGE 6B over HTTP: build / read the prompt, states, invalidation, restart, errors."""

import io
import json

import pytest

import app.api.prompt as prompt_api
from app.llm.mock import MockLLMClient
from app.models.project import PresentationStatus

from plan_helpers import CASE_A, CASE_B, CODE_DOC
from test_stage6a_api import approved, make_client, new_project, project, put_profile


@pytest.fixture()
def client(tmp_path):
    return make_client(tmp_path, MockLLMClient(), enrichment_provider="mock")


def enriched(client, **opts):
    pid = approved(client, **opts)
    assert client.post(f"/projects/{pid}/enrich").status_code == 200
    return pid


def built(client, **opts):
    pid = enriched(client, **opts)
    r = client.post(f"/projects/{pid}/prompt")
    assert r.status_code == 200, r.text
    return pid, r.json()


# ------------------------------------------------------------------ happy path
def test_build_the_prompt_of_an_enriched_project(client):
    pid = enriched(client, **CASE_B, code_level="snippet")
    slides = client.get(f"/projects/{pid}/slides").json()
    plan = client.get(f"/projects/{pid}/plan").json()
    enrichment = client.get(f"/projects/{pid}/enrichment").json()
    assert project(client, pid)["presentation_status"] == "enriched"

    r = client.post(f"/projects/{pid}/prompt")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["content_source"] == "enriched" and p["enrichment_status"] == "enriched"
    assert p["slide_count"] == slides["slide_count"] == len(p["slides"])
    assert p["validation"]["passed"] is True and p["validation"]["errors"] == []
    assert p["lecture_id"] == plan["id"] and p["project_id"] == pid and len(p["prompt_hash"]) == 64
    assert p["section_names"][0] == "ROLE" and p["section_names"][-1] == "SOURCE MATERIAL CONSTRAINTS"
    assert plan["title"] in p["final_prompt"]
    # the source file is read too (code block of the uploaded document)
    assert 'client.publish("sensor/temp", "21")' in p["final_prompt"]
    assert "원문 파일: doc.md" in p["final_prompt"]

    pr = project(client, pid)
    assert pr["presentation_status"] == "ready_for_presentation" and pr["has_prompt"] is True
    assert client.get(f"/projects/{pid}/prompt").json() == p
    # STAGE 1-6A results are untouched
    assert client.get(f"/projects/{pid}/slides").json() == slides
    assert client.get(f"/projects/{pid}/plan").json() == plan
    assert client.get(f"/projects/{pid}/enrichment").json() == enrichment


def test_the_text_endpoint_returns_only_the_prompt(client):
    pid, p = built(client, **CASE_B)
    r = client.get(f"/projects/{pid}/prompt/text")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.content.decode("utf-8") == p["final_prompt"]


def test_final_prompt_is_stored_with_the_project(client, tmp_path):
    pid, p = built(client, **CASE_B)
    stored = json.loads((tmp_path / "projects" / pid / "project.json").read_text(encoding="utf-8"))
    assert stored["final_prompt"] == p["final_prompt"]
    assert stored["presentation_prompt"]["prompt_hash"] == p["prompt_hash"]
    assert stored["presentation_provider"] is None and stored["presentation_result"] is None  # STAGE 7+


def test_a_prompt_can_be_built_without_enrichment(client):
    pid = approved(client, **CASE_B)
    r = client.post(f"/projects/{pid}/prompt")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["content_source"] == "rule_based" and p["enrichment_status"] is None
    assert any("콘텐츠 보강 없이" in w for w in p["warnings"]) and p["validation"]["passed"] is True
    assert project(client, pid)["presentation_status"] == "ready_for_presentation"


def test_a_project_without_an_llm_still_gets_a_prompt(tmp_path):
    c = make_client(tmp_path)  # ENRICHMENT_PROVIDER=none
    pid = approved(c)
    assert c.post(f"/projects/{pid}/enrich").json()["status"] == "enrichment_failed"
    r = c.post(f"/projects/{pid}/prompt")
    assert r.status_code == 200 and r.json()["content_source"] == "rule_based"
    assert any("모든 슬라이드에서 실패" in w for w in r.json()["warnings"])


def test_building_again_gives_the_same_prompt(client):
    pid, first = built(client, **CASE_B)
    again = client.post(f"/projects/{pid}/prompt").json()
    assert again["final_prompt"] == first["final_prompt"] and again["prompt_hash"] == first["prompt_hash"]
    assert project(client, pid)["presentation_status"] == "ready_for_presentation"


def test_the_prompt_survives_a_server_restart(tmp_path):
    c = make_client(tmp_path, MockLLMClient(), enrichment_provider="mock")
    pid, p = built(c, **CASE_B)
    again = make_client(tmp_path, MockLLMClient(), enrichment_provider="mock")
    assert again.get(f"/projects/{pid}/prompt").json() == p
    assert again.get(f"/projects/{pid}").json()["presentation_status"] == "ready_for_presentation"


def test_different_options_give_different_prompts_over_the_api(client):
    _, a = built(client, **CASE_A)
    _, b = built(client, **CASE_B)
    assert a["prompt_hash"] != b["prompt_hash"] and a["options"]["audience_level"] != b["options"]["audience_level"]
    assert a["final_prompt"] != b["final_prompt"]


# ------------------------------------------------------------------ preconditions
def test_the_prompt_needs_the_earlier_stages_and_an_approval(client):
    pid = new_project(client)
    assert client.post(f"/projects/{pid}/prompt").json()["error"]["code"] == "PlanNotReady"
    put_profile(client, pid, **CASE_B)
    assert client.post(f"/projects/{pid}/plan").status_code == 200
    assert client.post(f"/projects/{pid}/prompt").json()["error"]["code"] == "SlidesNotReady"
    assert client.post(f"/projects/{pid}/slides").status_code == 200
    r = client.post(f"/projects/{pid}/prompt")  # slides exist but the structure is not approved
    assert r.status_code == 409 and r.json()["error"]["code"] == "PromptNotAllowed"
    assert project(client, pid)["presentation_status"] == "planned"


def test_nothing_to_read_before_the_prompt_is_built(client):
    pid = approved(client)
    for url in (f"/projects/{pid}/prompt", f"/projects/{pid}/prompt/text"):
        r = client.get(url)
        assert r.status_code == 409 and r.json()["error"]["code"] == "PromptNotReady"
    assert project(client, pid)["has_prompt"] is False


def test_unknown_project(client):
    r = client.post("/projects/" + "0" * 32 + "/prompt")
    assert r.status_code == 404 and r.json()["error"]["code"] == "ProjectNotFound"


def test_no_prompt_while_the_enrichment_is_running(client):
    pid = approved(client)
    store = client.app.state.store
    p = store.get(pid)
    p.presentation_status = PresentationStatus.enriching
    store.save(p)
    r = client.post(f"/projects/{pid}/prompt")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PromptNotAllowed"


# ------------------------------------------------------------------ invalidation
def stale_everywhere(client, pid):
    for url in (f"/projects/{pid}/prompt", f"/projects/{pid}/prompt/text"):
        assert client.get(url).json()["error"]["code"] == "PromptNotReady"
    pr = project(client, pid)
    assert pr["has_prompt"] is False and pr["presentation_status"] != "ready_for_presentation"


def test_new_options_withdraw_the_prompt(client):
    pid, _ = built(client, **CASE_B)
    put_profile(client, pid, **CASE_A)
    stale_everywhere(client, pid)
    assert project(client, pid)["presentation_status"] == "analyzed"


def test_a_new_plan_withdraws_the_prompt(client):
    pid, _ = built(client, **CASE_B)
    assert client.post(f"/projects/{pid}/plan").status_code == 200
    stale_everywhere(client, pid)
    assert project(client, pid)["presentation_status"] == "planned"


def test_new_slides_withdraw_the_prompt(client):
    pid, _ = built(client, **CASE_B)
    assert client.post(f"/projects/{pid}/slides").status_code == 200
    stale_everywhere(client, pid)
    assert project(client, pid)["presentation_status"] == "planned"


def test_enriching_again_withdraws_the_prompt(client):
    pid, _ = built(client, **CASE_B)
    assert client.post(f"/projects/{pid}/enrich?force=true").status_code == 200
    stale_everywhere(client, pid)
    assert project(client, pid)["presentation_status"] == "enriched"
    assert client.post(f"/projects/{pid}/prompt").status_code == 200  # and it can be built again


def test_a_new_upload_and_a_new_analysis_withdraw_the_prompt(client):
    pid, _ = built(client, **CASE_B)
    assert client.post(f"/projects/{pid}/analyze").status_code == 200
    stale_everywhere(client, pid)
    pid2, _ = built(client, **CASE_B)
    up = client.post(f"/projects/{pid2}/upload", files={"file": ("doc.md", io.BytesIO(CODE_DOC.encode("utf-8")), "text/plain")})
    assert up.status_code == 200
    stale_everywhere(client, pid2)


def test_approving_again_keeps_a_built_prompt(client):
    pid, p = built(client, **CASE_B)
    assert client.post(f"/projects/{pid}/plan/approve").status_code == 200
    assert client.get(f"/projects/{pid}/prompt").json() == p
    assert project(client, pid)["presentation_status"] == "ready_for_presentation"


# ------------------------------------------------------------------ errors
def test_an_invalid_prompt_is_never_stored(client, monkeypatch):
    pid = enriched(client, **CASE_B)
    real = prompt_api.GensparkPromptBuilder

    class Broken(real):
        def build(self, **kw):
            r = super().build(**kw)
            r.validation.errors.append("slide 3 is missing")
            return r

    monkeypatch.setattr(prompt_api, "GensparkPromptBuilder", Broken)
    r = client.post(f"/projects/{pid}/prompt")
    assert r.status_code == 422 and r.json()["error"]["code"] == "PromptBuildError"
    assert "slide 3 is missing" in r.json()["error"]["details"]
    assert project(client, pid)["presentation_status"] == "enriched" and project(client, pid)["has_prompt"] is False


def test_a_crash_in_the_builder_returns_a_readable_error_without_a_trace(client, monkeypatch):
    pid = enriched(client, **CASE_B)

    class Boom:
        def build(self, **kw):
            raise RuntimeError("internal detail /secret/path")

    monkeypatch.setattr(prompt_api, "GensparkPromptBuilder", Boom)
    r = client.post(f"/projects/{pid}/prompt")
    body = json.dumps(r.json(), ensure_ascii=False)
    assert r.status_code == 422 and r.json()["error"]["code"] == "PromptBuildError"
    assert "secret" not in body and "Traceback" not in body and "RuntimeError" not in body
    assert project(client, pid)["has_prompt"] is False


def test_project_response_never_exposes_server_paths(client):
    pid, _ = built(client, **CASE_B)
    body = json.dumps(project(client, pid))
    assert "source_file_path" not in body and "final_prompt" not in body  # the text is read via /prompt

