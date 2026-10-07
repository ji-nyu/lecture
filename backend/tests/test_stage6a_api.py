"""STAGE 6A over HTTP: enrich, status, retry, cache, statuses, invalidation, errors, settings."""

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings, parse_enrichment_provider
from app.llm.factory import create_content_client
from app.llm.json_client import JsonContentClient
from app.llm.mock import MockLLMClient
from app.main import create_app
from app.services.llm_client import LLMTimeout

from enrich_helpers import ScriptedLLM, only
from plan_helpers import BASE, CASE_A, CASE_B, CODE_DOC

SECRET = "TESTSECRET-not-a-real-key"


def make_client(tmp_path, content_client=None, **settings):
    s = Settings(data_dir=tmp_path, max_upload_mb=5, cors_origins=(), **settings)
    return TestClient(create_app(s, content_client=content_client), raise_server_exceptions=False)


@pytest.fixture()
def client(tmp_path):
    return make_client(tmp_path, MockLLMClient(), enrichment_provider="mock")


def new_project(client, doc=CODE_DOC):
    pid = client.post("/projects", json={}).json()["id"]
    up = client.post(f"/projects/{pid}/upload", files={"file": ("doc.md", io.BytesIO(doc.encode("utf-8")), "text/plain")})
    assert up.status_code == 200, up.text
    assert client.post(f"/projects/{pid}/analyze").status_code == 200
    return pid


def put_profile(client, pid, **opts):
    r = client.put(f"/projects/{pid}/profile", json={**BASE, **opts})
    assert r.status_code == 200, r.text


def approved(client, **opts):
    """A project that went through profile -> plan -> slides -> approval."""
    pid = new_project(client)
    put_profile(client, pid, **opts)
    assert client.post(f"/projects/{pid}/plan").status_code == 200
    assert client.post(f"/projects/{pid}/slides").status_code == 200
    r = client.post(f"/projects/{pid}/plan/approve")
    assert r.status_code == 200, r.text
    return pid


def project(client, pid):
    return client.get(f"/projects/{pid}").json()


def skeleton(spec):
    return [(s["slide_number"], s["section_id"], s["slide_type"], s["estimated_explanation_time"]) for s in spec["slides"]]


# ------------------------------------------------------------------ happy path
def test_enrich_an_approved_project(client):
    pid = approved(client, **CASE_B, code_level="snippet")
    slides_before = client.get(f"/projects/{pid}/slides").json()
    plan_before = client.get(f"/projects/{pid}/plan").json()
    assert project(client, pid)["presentation_status"] == "ready_to_generate"

    r = client.post(f"/projects/{pid}/enrich")
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["status"] == "enriched" and e["slide_count"] == len(e["slides"]) == slides_before["slide_count"]
    assert skeleton(e) == skeleton(slides_before)
    assert e["lecture_id"] == plan_before["id"] and e["project_id"] == pid
    assert e["validation"]["passed"] is True and e["validation"]["slide_count_same"] is True
    assert all(s["status"] == "enriched" and s["enriched"]["key_message"] for s in e["slides"])
    assert e["version"]["model_provider"] == "mock" and len(e["version"]["source_hash"]) == 64

    p = project(client, pid)
    assert p["presentation_status"] == "enriched" and p["has_enrichment"] is True
    assert client.get(f"/projects/{pid}/enrichment").json() == e
    # STAGE 1-5 results are untouched
    assert client.get(f"/projects/{pid}/slides").json() == slides_before
    assert client.get(f"/projects/{pid}/plan").json() == plan_before


def test_status_endpoint(client):
    pid = approved(client)
    s = client.get(f"/projects/{pid}/enrichment/status").json()
    assert s["status"] == "not_started" and s["configured"] is True and s["provider"] == "mock"
    client.post(f"/projects/{pid}/enrich")
    s = client.get(f"/projects/{pid}/enrichment/status").json()
    assert s["status"] == "enriched" and s["project_status"] == "enriched"
    assert s["slide_count"] == s["enriched_count"] > 0 and s["failed_count"] == 0 and s["failed_slides"] == []
    assert s["llm_calls"] == s["slide_count"] and s["validation_passed"] is True and s["generated_at"]
    assert s["model"] == "mock-content-writer-v1"


def test_enrichment_survives_a_server_restart(tmp_path):
    c = make_client(tmp_path, MockLLMClient(), enrichment_provider="mock")
    pid = approved(c)
    e = c.post(f"/projects/{pid}/enrich").json()
    again = make_client(tmp_path, MockLLMClient(), enrichment_provider="mock")
    assert again.get(f"/projects/{pid}/enrichment").json() == e
    assert again.get(f"/projects/{pid}").json()["presentation_status"] == "enriched"


def test_different_profiles_give_different_content_over_the_api(client):
    a, b = approved(client, **CASE_A), approved(client, **CASE_B)
    ea, eb = (client.post(f"/projects/{p}/enrich").json() for p in (a, b))
    assert ea["metadata"]["audience_level"] != eb["metadata"]["audience_level"]
    assert [s["enriched"]["key_message"] for s in ea["slides"]][:3] != [s["enriched"]["key_message"] for s in eb["slides"]][:3]


# ------------------------------------------------------------------ preconditions
def test_enrichment_needs_the_earlier_stages_and_an_approval(client):
    pid = new_project(client)
    r = client.post(f"/projects/{pid}/enrich")
    assert r.status_code == 409 and r.json()["error"]["code"] in ("ProfileNotReady", "PlanNotReady")
    put_profile(client, pid)
    client.post(f"/projects/{pid}/plan")
    r = client.post(f"/projects/{pid}/enrich")
    assert r.status_code == 409 and r.json()["error"]["code"] == "SlidesNotReady"
    client.post(f"/projects/{pid}/slides")
    r = client.post(f"/projects/{pid}/enrich")  # slides exist but were not approved
    assert r.status_code == 409 and r.json()["error"]["code"] == "EnrichmentNotAllowed"
    assert "Traceback" not in r.text and r.json()["error"]["message"]
    assert client.get(f"/projects/{pid}").json()["presentation_status"] != "enriching"


def test_reading_before_enriching_is_a_readable_409(client):
    pid = approved(client)
    r = client.get(f"/projects/{pid}/enrichment")
    assert r.status_code == 409 and r.json()["error"]["code"] == "EnrichmentNotReady"
    r = client.post(f"/projects/{pid}/enrichment/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "EnrichmentNotReady"
    assert client.get("/projects/" + "0" * 32 + "/enrichment").status_code == 404
    assert client.post("/projects/" + "0" * 32 + "/enrich").status_code == 404
    assert client.get("/projects/" + "0" * 32 + "/enrichment/status").status_code == 404


# ------------------------------------------------------------------ fallback + retry
def test_partial_failure_keeps_the_project_going_and_retry_fixes_only_the_failed_slides(tmp_path):
    bad = {3, 5}
    llm = ScriptedLLM(error=lambda req: LLMTimeout("t") if req.slide.slide_number in bad else None)
    c = make_client(tmp_path, llm, enrichment_provider="mock")
    pid = approved(c)
    r = c.post(f"/projects/{pid}/enrich")
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["status"] == "enrichment_partial" and e["stats"]["failed_count"] == 2
    assert project(c, pid)["presentation_status"] == "enrichment_partial"
    failed = [s for s in e["slides"] if s["status"] == "enrichment_failed"]
    assert [s["slide_number"] for s in failed] == [3, 5]
    assert all(s["failure_reason"] == "timeout" and s["enriched"] is None and s["original"]["key_message"] for s in failed)
    st = c.get(f"/projects/{pid}/enrichment/status").json()
    assert st["status"] == "enrichment_partial" and [f["slide_number"] for f in st["failed_slides"]] == [3, 5]
    assert st["failed_slides"][0]["reason"] == "timeout" and st["failed_slides"][0]["message"]

    bad.clear()
    llm.slide_calls.clear()
    r = c.post(f"/projects/{pid}/enrichment/retry")
    assert r.status_code == 200, r.text
    assert sorted(llm.slide_calls) == [3, 5]
    fixed = r.json()
    assert fixed["status"] == "enriched" and all(s["status"] == "enriched" for s in fixed["slides"])
    assert skeleton(fixed) == skeleton(e)
    assert project(c, pid)["presentation_status"] == "enriched"


def test_without_a_configured_llm_every_slide_falls_back_with_a_200(tmp_path):
    c = make_client(tmp_path)  # ENRICHMENT_PROVIDER defaults to "none"
    pid = approved(c)
    r = c.post(f"/projects/{pid}/enrich")
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["status"] == "enrichment_failed"
    assert {s["failure_reason"] for s in e["slides"]} == {"not_configured"}
    assert all(s["enriched"] is None and s["original"]["key_message"] for s in e["slides"])
    assert project(c, pid)["presentation_status"] == "enrichment_partial"
    st = c.get(f"/projects/{pid}/enrichment/status").json()
    assert st["configured"] is False and st["provider"] == "none"
    assert c.get(f"/projects/{pid}/slides").status_code == 200  # the rule-based slides are still there


def test_a_provider_error_never_leaks_secrets_or_stack_traces(tmp_path):
    c = make_client(tmp_path, ScriptedLLM(error=lambda req: RuntimeError(f"Bearer {SECRET} failed")), enrichment_provider="mock")
    pid = approved(c)
    r = c.post(f"/projects/{pid}/enrich")
    assert r.status_code == 200 and SECRET not in r.text and "Traceback" not in r.text
    assert {s["failure_reason"] for s in r.json()["slides"]} == {"unexpected"}
    assert SECRET not in c.get(f"/projects/{pid}/enrichment/status").text


def test_an_internal_error_gives_a_readable_message_and_restores_the_state(client, monkeypatch):
    pid = approved(client)
    from app.services import slide_content_enricher as mod

    def boom(*a, **k):
        raise ValueError(f"internal {SECRET}")

    monkeypatch.setattr(mod.SlideContentEnricher, "enrich", boom)
    r = client.post(f"/projects/{pid}/enrich")
    assert r.status_code == 422 and r.json()["error"]["message"]
    assert SECRET not in r.text and "Traceback" not in r.text and "ValueError" not in r.text
    p = project(client, pid)
    assert p["presentation_status"] == "ready_to_generate" and p["has_enrichment"] is False


# ------------------------------------------------------------------ cache
def test_the_second_run_uses_the_cache_and_force_ignores_it(tmp_path):
    llm = MockLLMClient()
    c = make_client(tmp_path, llm, enrichment_provider="mock")
    pid = approved(c)
    first = c.post(f"/projects/{pid}/enrich").json()
    n = llm.call_count
    assert n == first["slide_count"]
    assert [p.parent.name for p in tmp_path.rglob("enrichment_cache.json")] == [pid]
    second = c.post(f"/projects/{pid}/enrich").json()
    assert llm.call_count == n
    assert second["stats"]["llm_calls"] == 0 and second["stats"]["cache_hits"] == second["slide_count"]
    assert [s["enriched"] for s in second["slides"]] == [s["enriched"] for s in first["slides"]]
    forced = c.post(f"/projects/{pid}/enrich?force=true").json()
    assert llm.call_count == 2 * n and forced["stats"]["llm_calls"] == n and forced["stats"]["cache_hits"] == 0


def test_a_changed_option_is_not_answered_from_the_cache(tmp_path):
    llm = MockLLMClient()
    c = make_client(tmp_path, llm, enrichment_provider="mock")
    pid = approved(c, speaker_notes="concise")
    c.post(f"/projects/{pid}/enrich")
    n = llm.call_count
    put_profile(c, pid, speaker_notes="full")
    c.post(f"/projects/{pid}/plan")
    c.post(f"/projects/{pid}/slides")
    c.post(f"/projects/{pid}/plan/approve")
    c.post(f"/projects/{pid}/enrich")
    assert llm.call_count > n


# ------------------------------------------------------------------ invalidation
@pytest.mark.parametrize("change", ["profile", "plan", "slides", "analyze", "upload"])
def test_enrichment_is_dropped_when_an_earlier_result_changes(client, change):
    pid = approved(client)
    assert client.post(f"/projects/{pid}/enrich").status_code == 200
    assert project(client, pid)["has_enrichment"] is True
    if change == "profile":
        put_profile(client, pid, lecture_type="theory")
    elif change == "plan":
        assert client.post(f"/projects/{pid}/plan").status_code == 200
    elif change == "slides":
        assert client.post(f"/projects/{pid}/slides").status_code == 200
    elif change == "analyze":
        assert client.post(f"/projects/{pid}/analyze").status_code == 200
    else:
        up = client.post(f"/projects/{pid}/upload", files={"file": ("doc.md", io.BytesIO(CODE_DOC.encode()), "text/plain")})
        assert up.status_code == 200
    p = project(client, pid)
    assert p["has_enrichment"] is False
    assert p["presentation_status"] not in ("enriched", "enrichment_partial", "enriching", "ready_for_presentation")
    r = client.get(f"/projects/{pid}/enrichment")
    assert r.status_code == 409 and r.json()["error"]["code"] == "EnrichmentNotReady"


def test_approving_again_does_not_throw_away_an_enrichment(client):
    pid = approved(client)
    client.post(f"/projects/{pid}/enrich")
    assert client.post(f"/projects/{pid}/plan/approve").status_code == 200
    p = project(client, pid)
    assert p["has_enrichment"] is True and p["presentation_status"] == "enriched"


def test_a_change_during_the_run_makes_the_result_stale(tmp_path):
    holder = {}

    def change_profile_once(req, out):
        if req.slide.slide_number == 1 and not holder.get("done"):
            holder["done"] = True
            holder["r"] = holder["client"].put(f"/projects/{holder['pid']}/profile", json={**BASE, "lecture_type": "theory"})
        return out

    llm = ScriptedLLM(patch=change_profile_once)
    c = make_client(tmp_path, llm, enrichment_provider="mock")
    holder["client"] = c
    pid = holder["pid"] = approved(c)
    r = c.post(f"/projects/{pid}/enrich")
    assert holder["r"].status_code == 200  # the profile was changed while the LLM was working ...
    assert r.status_code == 409 and r.json()["error"]["code"] == "EnrichmentStale"  # ... so the result is not stored
    assert project(c, pid)["has_enrichment"] is False


# ------------------------------------------------------------------ old data / settings
def test_a_project_saved_before_stage_6a_still_loads(tmp_path):
    c = make_client(tmp_path, MockLLMClient(), enrichment_provider="mock")
    pid = approved(c)
    f = next(tmp_path.rglob("project.json"))
    data = json.loads(f.read_text(encoding="utf-8"))
    data.pop("enriched_specification", None)
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    again = make_client(tmp_path, MockLLMClient(), enrichment_provider="mock")
    p = again.get(f"/projects/{pid}").json()
    assert p["has_enrichment"] is False and p["presentation_status"] == "ready_to_generate"
    assert again.post(f"/projects/{pid}/enrich").status_code == 200


def test_enrichment_provider_settings(monkeypatch):
    assert parse_enrichment_provider(None) == "none" and parse_enrichment_provider(" MOCK ") == "mock"
    assert parse_enrichment_provider("openai") == "openai" and parse_enrichment_provider("gemini") == "none"
    for k, v in (("ENRICHMENT_PROVIDER", "mock"), ("ENRICHMENT_BATCH_SIZE", "8"), ("ENRICHMENT_WORKERS", "3")):
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert (s.enrichment_provider, s.enrichment_batch_size, s.enrichment_workers) == ("mock", 8, 3)
    monkeypatch.setenv("ENRICHMENT_BATCH_SIZE", "abc")
    monkeypatch.setenv("ENRICHMENT_WORKERS", "999")
    s = load_settings()
    assert s.enrichment_batch_size == 5 and s.enrichment_workers == 8
    assert Settings(data_dir="x", max_upload_mb=1, cors_origins=()).enrichment_provider == "none"


def test_the_content_client_follows_the_provider_setting(tmp_path):
    base = dict(data_dir=tmp_path, max_upload_mb=1, cors_origins=())
    assert create_content_client(Settings(**base)) is None
    assert isinstance(create_content_client(Settings(**base, enrichment_provider="mock")), MockLLMClient)
    # 'openai' without LLM_API_KEY / LLM_MODEL is "not configured", not an error
    assert create_content_client(Settings(**base, enrichment_provider="openai", llm_api_key=None)) is None
    with_key = create_content_client(Settings(**base, enrichment_provider="openai", llm_api_key="test-key", llm_model="m"))
    assert isinstance(with_key, JsonContentClient)  # only constructed: no request is sent
    assert with_key.provider == "openai"
