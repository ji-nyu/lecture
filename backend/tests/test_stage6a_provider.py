"""Real-provider path of STAGE 6A, verified WITHOUT any real API call.

The OpenAI SDK talks to an httpx MockTransport, so request bodies (structured output,
temperature, prompts), retries, parameter downgrades, token counting and the full
enricher flow are exercised offline. The real LLM is only used by scripts/live_enrichment_smoke.py.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.llm.base import LLMSlideOutput
from app.llm.factory import create_content_client
from app.llm.json_client import JsonContentClient
from app.llm.mock import MockLLMClient
from app.llm.prompts import PROMPT_VERSION, build_batch_system_prompt, build_system_prompt
from app.llm.schema import batch_schema, slide_schema
from app.models.enriched_slide_spec import CONTENT_FIELDS, FieldProvenance, RunStatus
from app.services.grounding_validator import GroundingValidator, visual_problems
from app.services.llm_client import (
    LLMConnectionError, LLMError, LLMRateLimit, LLMResponseError, LLMTimeout, OpenAICompatibleClient,
)
from app.services.source_context import build_requests

from enrich_helpers import CASE_B, make_ctx, run
from fake_llm import KOREAN_DOC
from test_stage6a_api import approved, make_client as make_app_client

KEY = "TESTSECRET-not-a-real-key"
BACKEND = Path(__file__).resolve().parents[1]


def completion(content, usage=None) -> httpx.Response:
    body = {
        "id": "x", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }
    if usage:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def api_client(handler, **kw):
    sleeps: list[float] = []
    kw.setdefault("temperature", 0.1)
    c = OpenAICompatibleClient(
        api_key=KEY, model="test-model", base_url="https://llm.test/v1", timeout=5.0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sdk_max_retries=0, sleep=sleeps.append, **kw,
    )
    c.slept = sleeps
    return c


def call(c, schema=None):
    return c.complete_json(task="t", system="SYS", user="USR", schema=schema)


# ------------------------------------------------------------------ structured output + temperature
def test_structured_output_and_low_temperature_are_requested():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return completion('{"ok": true}')

    assert call(api_client(handler), schema=slide_schema()) == {"ok": True}
    body = seen[0]
    assert body["temperature"] == 0.1
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == slide_schema()


def test_temperature_none_is_not_sent_and_json_object_is_used_without_a_schema():
    seen = []
    c = api_client(lambda r: (seen.append(json.loads(r.content)), completion("{}"))[1], temperature=None)
    call(c)
    assert "temperature" not in seen[0] and seen[0]["response_format"] == {"type": "json_object"}


def test_a_model_that_rejects_temperature_is_asked_again_without_it():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: temperature", "type": "invalid_request_error",
                                                        "param": "temperature", "code": "unsupported_value"}})
        return completion('{"ok": 1}')

    c = api_client(handler)
    assert call(c) == {"ok": 1} and call(c) == {"ok": 1}
    assert ["temperature" in b for b in seen] == [True, False, False]  # dropped once, remembered afterwards


def test_a_provider_without_json_schema_falls_back_to_json_object():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body["response_format"]["type"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema is not supported",
                                                        "type": "invalid_request_error", "param": "response_format", "code": None}})
        return completion('{"ok": 2}')

    c = api_client(handler)
    assert call(c, schema=slide_schema()) == {"ok": 2} and call(c, schema=slide_schema()) == {"ok": 2}
    assert seen == ["json_schema", "json_object", "json_object"]


def test_a_real_bad_request_is_not_retried_and_leaks_nothing():
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(400, json={"error": {"message": f"bad {KEY}", "param": "messages", "code": None}})

    with pytest.raises(LLMError) as e:
        call(api_client(handler, retries=3))
    assert len(n) == 1 and KEY not in str(e.value) and KEY not in repr(e.value)


# ------------------------------------------------------------------ retries
def test_transient_errors_are_retried_a_limited_number_of_times_then_succeed():
    answers = [httpx.Response(429, json={"error": {"message": "slow down", "code": "rate_limit_exceeded"}}),
               "timeout", httpx.Response(503, json={"error": {"message": "unavailable"}}), completion('{"ok": 3}')]

    def handler(req):
        a = answers.pop(0)
        if a == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        return a

    c = api_client(handler, retries=3)
    assert call(c) == {"ok": 3}
    assert c.usage["retries"] == 3 and len(c.slept) == 3 and c.slept == sorted(c.slept)  # exponential backoff


@pytest.mark.parametrize("make,exc", [
    (lambda req: httpx.Response(429, json={"error": {"message": "x", "code": "rate_limit_exceeded"}}), LLMRateLimit),
    (lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req)), LLMTimeout),
    (lambda req: (_ for _ in ()).throw(httpx.ConnectError("down", request=req)), LLMConnectionError),
    (lambda req: httpx.Response(500, json={"error": {"message": "boom"}}), LLMConnectionError),
    (lambda req: completion("not json at all"), LLMResponseError),
])
def test_retries_stop_after_the_limit(make, exc):
    n = []

    def handler(req):
        n.append(1)
        return make(req)

    with pytest.raises(exc):
        call(api_client(handler, retries=2))
    assert len(n) == 3  # 1 call + 2 retries, never indefinitely


def test_auth_errors_and_exhausted_quota_are_not_retried():
    n = []

    def unauthorized(req):
        n.append(1)
        return httpx.Response(401, json={"error": {"message": "Incorrect API key", "code": "invalid_api_key"}})

    with pytest.raises(LLMError):
        call(api_client(unauthorized, retries=3))
    assert len(n) == 1

    def quota(req):
        n.append(1)
        return httpx.Response(429, json={"error": {"message": "quota", "code": "insufficient_quota"}})

    n.clear()
    with pytest.raises(LLMError) as e:
        call(api_client(quota, retries=3))
    assert len(n) == 1 and not isinstance(e.value, LLMRateLimit)


def test_token_usage_is_counted():
    c = api_client(lambda r: completion("{}", usage={"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}))
    call(c)
    call(c)
    assert c.usage_snapshot() == {"calls": 2, "retries": 0, "input_tokens": 240, "output_tokens": 90}
    assert JsonContentClient(c).usage["input_tokens"] == 240


# ------------------------------------------------------------------ full flow through the SDK + HTTP layer
@pytest.fixture(scope="module")
def b():
    return make_ctx(**CASE_B)


def enrichment_handler(ctx, seen):
    """Plays the LLM: answers each requested slide like the MockLLMClient would."""
    mock = MockLLMClient()
    reqs = {r.slide.slide_number: r for r in build_requests(ctx.spec, ctx.plan, ctx.profile, ctx.analysis, PROMPT_VERSION)}

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        user = json.loads(body["messages"][1]["content"])
        if "slides" in user:
            numbers = [s["slide"]["slide_number"] for s in user["slides"]]
            out = {"slides": [mock.enrich_slide(reqs[n]) for n in numbers]}
        else:
            out = mock.enrich_slide(reqs[user["slide"]["slide_number"]])
        return completion(json.dumps(out, ensure_ascii=False), usage={"prompt_tokens": 1000, "completion_tokens": 400, "total_tokens": 1400})

    return handler


def test_the_whole_deck_is_enriched_through_the_openai_sdk_path(b):
    seen = []
    llm = api_client(enrichment_handler(b, seen), retries=2)
    r = run(b, JsonContentClient(llm, provider="openai"))
    assert r.status == RunStatus.enriched and len(r.slides) == 22 and r.validation.passed
    assert [s.slide_number for s in r.slides] == list(range(1, 23))
    assert r.version.model_provider == "openai" and r.version.model_name == "test-model"
    assert len(seen) == 5  # 22 slides, batches of 5
    for body in seen:
        assert body["response_format"]["type"] == "json_schema" and body["temperature"] == 0.1
        system, user = (m["content"] for m in body["messages"])
        assert "OUT OF SCOPE" in system and "QoS 2의 단계" in system  # scope notes are part of the instructions
        assert "source_first" in system
        payload = json.loads(user)
        assert payload["scope_notes"] and len(payload["slides"]) <= 5
        assert len(user) < 3 * len(b.text)  # excerpts, not the document
    assert llm.usage_snapshot()["input_tokens"] == 5000
    assert KEY not in json.dumps(seen, ensure_ascii=False)  # the key travels only in the Authorization header


def test_one_bad_http_answer_only_affects_its_batch(b):
    seen = []
    good = enrichment_handler(b, seen)

    def handler(req):
        user = json.loads(json.loads(req.content)["messages"][1]["content"])
        if 3 in [s["slide"]["slide_number"] for s in user.get("slides", [])]:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return good(req)

    r = run(b, JsonContentClient(api_client(handler, retries=1), provider="openai"))
    failed = [s.slide_number for s in r.slides if s.enriched is None]
    assert failed == [1, 2, 3, 4, 5] and r.status == RunStatus.enrichment_partial
    assert {s.failure_reason for s in r.slides if s.enriched is None} == {"provider_unavailable"}
    assert r.validation.passed


def test_the_real_client_falls_back_on_invalid_json_from_the_provider(b):
    r = run(b, JsonContentClient(api_client(lambda req: completion("네, 알겠습니다. JSON은 다음과 같습니다"), retries=1), provider="openai"))
    assert r.status == RunStatus.enrichment_failed and {s.failure_reason for s in r.slides} == {"invalid_json"}
    assert len(r.slides) == 22 and r.validation.passed


# ------------------------------------------------------------------ schema
def _walk(schema, path="root"):
    if isinstance(schema, dict):
        if schema.get("type") == "object" or "properties" in schema:
            props = schema["properties"]
            assert schema["additionalProperties"] is False, path
            assert schema["required"] == list(props), path  # strict mode: every key is required
            for k, v in props.items():
                _walk(v, f"{path}.{k}")
        for k in ("items", "anyOf"):
            if k in schema:
                for sub in schema[k] if isinstance(schema[k], list) else [schema[k]]:
                    _walk(sub, f"{path}.{k}")


def test_the_output_schema_is_strict_and_matches_the_models():
    s = slide_schema()
    _walk(s)
    _walk(batch_schema())
    assert set(CONTENT_FIELDS) <= set(s["properties"]) and "slide_number" in s["properties"]
    assert set(s["properties"]) - {"slide_number", "provenance"} == set(CONTENT_FIELDS)
    assert set(s["properties"]["provenance"]["properties"]) == set(CONTENT_FIELDS)
    assert set(s["properties"]["provenance"]["properties"]["example"]["enum"]) == {p.value for p in FieldProvenance} | {None}
    assert set(s["properties"]) <= set(LLMSlideOutput.model_fields)  # nothing the enricher would ignore
    assert batch_schema()["properties"]["slides"]["items"] == s


def test_the_offline_writer_answers_inside_the_schema(b):
    props = set(slide_schema()["properties"])
    for r in build_requests(b.spec, b.plan, b.profile, b.analysis, PROMPT_VERSION):
        assert set(MockLLMClient().enrich_slide(r)) <= props


# ------------------------------------------------------------------ prompts
def test_the_prompt_states_role_profile_scope_and_output_rules():
    ctx = make_ctx(KOREAN_DOC, source_policy="source_only", speaker_notes="full")
    req = build_requests(ctx.spec, ctx.plan, ctx.profile, ctx.analysis, PROMPT_VERSION)[3]
    p = build_system_prompt(req)
    for needle in ("콘텐츠 작성자", "바꿀 수 없", "source_context", "source_only", "OUT OF SCOPE", "캘빈 회로의 세부 화학식",
                   "JSON", "provenance", "Genspark", "퀴즈 방식=", "예시 수준=", "청중=", "난이도=", "강의 유형=", "speaker_notes=full"):
        assert needle in p, needle
    assert "concept:" in p and "practice:" not in p  # only the guide of this slide type
    assert PROMPT_VERSION != "enrich-v1"


def test_the_prompt_differs_by_profile():
    a, b_, c = (make_ctx(**{**BASE_A, "source_policy": "source_first"}), make_ctx(**CASE_B), make_ctx(**BASE_C))
    prompts = [build_system_prompt(build_requests(x.spec, x.plan, x.profile, x.analysis, PROMPT_VERSION)[3]) for x in (a, b_, c)]
    assert len(set(prompts)) == 3
    assert "비유를 하나 곁들인다" in prompts[0] and "동작 원리" in prompts[1] and "trade-off" in prompts[2]


def test_batch_prompt_contains_the_guides_of_all_types_in_the_batch(b):
    reqs = build_requests(b.spec, b.plan, b.profile, b.analysis, PROMPT_VERSION)
    p = build_batch_system_prompt([reqs[8], reqs[10], reqs[5]])  # concept, architecture, practice
    assert all(f"- {t}:" in p for t in ("concept", "architecture", "practice")) and "- quiz:" not in p
    assert '"slides"' in p


BASE_A = dict(audience_level="university_beginner", duration_minutes=60, difficulty="beginner", lecture_type="theory",
              explanation_depth="detailed")
BASE_C = dict(audience_level="professional", duration_minutes=60, difficulty="advanced", lecture_type="theory",
              explanation_depth="concise", source_policy="source_first")


# ------------------------------------------------------------------ validator additions
def test_visual_instructions_are_checked_for_concreteness(b):
    reqs = build_requests(b.spec, b.plan, b.profile, b.analysis, PROMPT_VERSION)
    arch = next(r for r in reqs if r.slide.slide_type == "architecture" and "Publisher" in r.slide.concepts)
    good = "Publisher를 왼쪽에, Subscriber를 오른쪽에, Broker를 중앙에 배치하고 Publisher → Broker → Subscriber 화살표로 메시지 흐름을 표현한다."
    assert visual_problems(arch, good) == []
    bad = visual_problems(arch, "그림을 사용한다.")
    assert len(bad) >= 3
    no_relation = visual_problems(arch, "Publisher, Broker, Subscriber 아이콘을 왼쪽부터 나란히 배치한다.")
    assert any("관계" in p for p in no_relation)


def test_short_full_notes_and_vague_visuals_become_warnings_not_failures():
    ctx = make_ctx(KOREAN_DOC, source_policy="source_first", speaker_notes="full")
    req = next(r for r in build_requests(ctx.spec, ctx.plan, ctx.profile, ctx.analysis, PROMPT_VERSION)
               if r.slide.slide_type == "concept")
    out = LLMSlideOutput(slide_number=req.slide.slide_number, key_message="광합성은 식물이 빛에너지를 이용하여 포도당을 만드는 과정이다.",
                         presenter_notes="광합성을 봅니다.", visual_instruction="그림을 사용한다.",
                         provenance={"key_message": "paraphrased_source", "presenter_notes": "llm_explanation",
                                     "visual_instruction": "llm_explanation"})
    v = GroundingValidator(ctx.text).validate(req, out)
    assert v.failure_reason is None and v.content.presenter_notes and v.content.visual_instruction
    assert any("짧습니다" in w for w in v.warnings) and any("visual_instruction" in w for w in v.warnings)


# ------------------------------------------------------------------ config / factory / api
def test_enrichment_environment_variables(monkeypatch):
    for k in ("ENRICHMENT_PROVIDER", "ENRICHMENT_MODEL", "ENRICHMENT_TEMPERATURE", "ENRICHMENT_TIMEOUT_SECONDS",
              "ENRICHMENT_MAX_RETRIES", "ENRICHMENT_BATCH_SIZE", "ENRICHMENT_MAX_CONCURRENCY", "ENRICHMENT_WORKERS",
              "ENRICHMENT_CACHE_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert (s.enrichment_provider, s.enrichment_model, s.enrichment_temperature, s.enrichment_timeout_seconds,
            s.enrichment_max_retries, s.enrichment_batch_size, s.enrichment_workers, s.enrichment_cache_enabled) == \
        ("none", None, 0.1, 90.0, 2, 5, 1, True)  # safe defaults: no provider
    for k, v in dict(ENRICHMENT_PROVIDER="openai", ENRICHMENT_MODEL="my-model", ENRICHMENT_TEMPERATURE="0",
                     ENRICHMENT_TIMEOUT_SECONDS="30", ENRICHMENT_MAX_RETRIES="3", ENRICHMENT_BATCH_SIZE="4",
                     ENRICHMENT_MAX_CONCURRENCY="2", ENRICHMENT_WORKERS="7", ENRICHMENT_CACHE_ENABLED="false").items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert (s.enrichment_provider, s.enrichment_model, s.enrichment_temperature, s.enrichment_timeout_seconds,
            s.enrichment_max_retries, s.enrichment_batch_size, s.enrichment_workers, s.enrichment_cache_enabled) == \
        ("openai", "my-model", 0.0, 30.0, 3, 4, 2, False)  # MAX_CONCURRENCY wins over the older WORKERS
    monkeypatch.setenv("ENRICHMENT_MAX_RETRIES", "99")
    monkeypatch.setenv("ENRICHMENT_TEMPERATURE", "warm")
    monkeypatch.setenv("ENRICHMENT_CACHE_ENABLED", "maybe")
    s = load_settings()
    assert s.enrichment_max_retries == 5 and s.enrichment_temperature == 0.1 and s.enrichment_cache_enabled is True
    monkeypatch.delenv("ENRICHMENT_MAX_CONCURRENCY")
    assert load_settings().enrichment_workers == 7  # legacy name still works


def test_the_factory_uses_the_enrichment_model_and_needs_key_and_model(tmp_path):
    base = dict(data_dir=tmp_path, max_upload_mb=1, cors_origins=(), enrichment_provider="openai")
    assert create_content_client(Settings(**base)) is None
    assert create_content_client(Settings(**base, llm_api_key=KEY)) is None  # no model anywhere
    c = create_content_client(Settings(**base, llm_api_key=KEY, llm_model="llm-model"))
    assert c.model == "llm-model"
    c = create_content_client(Settings(**base, llm_api_key=KEY, llm_model="llm-model", enrichment_model="enrich-model",
                                       enrichment_temperature=0.0, enrichment_max_retries=1))
    assert isinstance(c, JsonContentClient) and c.model == "enrich-model"
    assert c._llm.temperature == 0.0 and c._llm.retries == 1 and c._llm.supports_schema is True
    assert KEY not in repr(Settings(**base, llm_api_key=KEY))


def test_disabling_the_cache_makes_every_run_call_the_llm(tmp_path):
    llm = MockLLMClient()
    c = make_app_client(tmp_path, llm, enrichment_provider="mock", enrichment_cache_enabled=False)
    pid = approved(c)
    c.post(f"/projects/{pid}/enrich")
    n = llm.call_count
    second = c.post(f"/projects/{pid}/enrich").json()
    assert llm.call_count == 2 * n and second["stats"]["cache_hits"] == 0
    assert not list(tmp_path.rglob("enrichment_cache.json"))


# ------------------------------------------------------------------ live script + secrets
def load_script():
    spec = importlib.util.spec_from_file_location("live_enrichment_smoke", BACKEND / "scripts" / "live_enrichment_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_enrichment_smoke"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_live_script_never_fakes_a_result_without_a_key(monkeypatch, capsys, tmp_path):
    for k in ("LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    doc = tmp_path / "lecture.md"
    doc.write_text(KOREAN_DOC, encoding="utf-8")
    script = load_script()
    monkeypatch.setattr(sys, "argv", ["live_enrichment_smoke.py", str(doc), "--smoke", "--outdir", str(tmp_path / "out")])
    assert script.main() == 2
    assert "Live test not executed: API key not configured" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()  # no report was produced


def test_the_live_script_dry_run_shows_the_prompt_and_calls_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    doc = tmp_path / "lecture.md"
    doc.write_text(KOREAN_DOC, encoding="utf-8")
    script = load_script()

    def no_client(**kw):
        raise AssertionError("a client must not be built in a dry run")

    monkeypatch.setattr(script, "build_openai_content_client", no_client)
    monkeypatch.setattr(sys, "argv", ["live_enrichment_smoke.py", str(doc), "--dry-run", "--slides", "4"])
    assert script.main() == 0
    out = capsys.readouterr().out
    assert "OUT OF SCOPE" in out and "system prompt" in out and KEY not in out


def test_the_live_script_picks_representative_slides_of_different_types(b):
    script = load_script()
    chosen = script.select_slides(b.spec, 8, "MQTT", None)
    types = [b.spec.slides[n - 1].slide_type.value for n in chosen]
    assert len(chosen) == 8 and {"definition", "concept", "architecture", "practice", "summary"} <= set(types)
    assert script.select_slides(b.spec, 8, "MQTT", [9, 3]) == [3, 9]


def test_no_api_key_is_stored_in_the_repository():
    root = BACKEND.parent
    pattern = re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")
    skip = {"node_modules", ".git", "__pycache__", "data", "dist", ".pytest_cache", ".venv", "venv"}
    hits = []
    allowed_secret_file = BACKEND / ".env"  # the one place a real key may live (git-ignored)
    for p in root.rglob("*"):
        if p == allowed_secret_file:
            continue
        if p.is_file() and not (set(p.relative_to(root).parts) & skip) and p.suffix in (
                ".py", ".md", ".txt", ".json", ".ts", ".tsx", ".js", ".example", ".env", ".toml", ".yml", ".yaml", ".cfg", ".ini", ""):
            try:
                if pattern.search(p.read_text(encoding="utf-8", errors="ignore")):
                    hits.append(str(p.relative_to(root)))  # the file name only, never the value
            except OSError:
                pass
    assert hits == []
    example = (BACKEND / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^LLM_API_KEY=\s*$", example, re.M) and re.search(r"^ENRICHMENT_PROVIDER=none\s*$", example, re.M)
    assert re.search(r"^ENRICHMENT_MODEL=\s*$", example, re.M)
