"""ANALYZER_MODE config and the LLM client abstraction (no real network: httpx MockTransport)."""

import json

import httpx
import pytest

from app.config import Settings, load_settings, parse_analyzer_mode
from app.services.analyzer_factory import build_analyzer
from app.services.hybrid_analyzer import HybridAnalyzer
from app.services.lecture_analyzer import HeuristicAnalyzer
from app.services.llm_client import (
    LLMError,
    LLMResponseError,
    LLMTimeout,
    OpenAICompatibleClient,
    create_llm_client,
    parse_json_object,
)

SECRET = "sk-test-secret-123"


def settings(**kw):
    return Settings(data_dir="x", max_upload_mb=1, cors_origins=(), **kw)


# ------------------------------------------------------------------ config
def test_default_mode_is_heuristic_and_needs_no_llm_settings():
    s = settings()
    assert s.analyzer_mode == "heuristic"
    assert s.llm_api_key is None and s.llm_model is None
    assert isinstance(build_analyzer(s), HeuristicAnalyzer)


@pytest.mark.parametrize(
    "value, expected",
    [(None, "heuristic"), ("", "heuristic"), ("HYBRID", "hybrid"), (" llm ", "llm"),
     ("heuristic", "heuristic"), ("gpt", "heuristic"), ("banana", "heuristic")],
)
def test_parse_analyzer_mode(value, expected):
    assert parse_analyzer_mode(value) == expected


def test_load_settings_reads_environment(monkeypatch):
    for k in ("ANALYZER_MODE", "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    assert load_settings().analyzer_mode in ("heuristic", "hybrid", "llm")  # .env may set it
    monkeypatch.setenv("ANALYZER_MODE", "hybrid")
    monkeypatch.setenv("LLM_API_KEY", SECRET)
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12.5")
    s = load_settings()
    assert (s.analyzer_mode, s.llm_model, s.llm_base_url, s.llm_timeout_seconds) == (
        "hybrid", "some-model", "https://llm.example/v1", 12.5)
    assert s.llm_api_key == SECRET


def test_api_key_is_never_printed():
    s = settings(llm_api_key=SECRET)
    assert SECRET not in repr(s) and SECRET not in str(s)


def test_no_secret_is_hard_coded_in_the_source():
    from pathlib import Path
    import re

    for f in (Path(__file__).resolve().parents[1] / "app").rglob("*.py"):
        assert not re.search(r"sk-[A-Za-z0-9]{16,}", f.read_text(encoding="utf-8")), f


def test_hybrid_mode_without_key_builds_a_fallback_analyzer():
    a = build_analyzer(settings(analyzer_mode="hybrid"))
    assert isinstance(a, HybridAnalyzer) and a.client is None
    assert create_llm_client(settings(analyzer_mode="hybrid", llm_api_key=SECRET)) is None  # no model
    assert create_llm_client(settings(llm_model="m")) is None  # no key


def test_injected_client_is_used_in_hybrid_but_ignored_in_heuristic_mode():
    class C:  # duck-typed client
        model = "c"

    c = C()
    assert build_analyzer(settings(analyzer_mode="hybrid"), c).client is c
    assert isinstance(build_analyzer(settings(analyzer_mode="heuristic"), c), HeuristicAnalyzer)


# ------------------------------------------------------------ JSON parsing
def test_parse_json_object_accepts_fenced_json_and_rejects_the_rest():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('  {"a": 1} ') == {"a": 1}
    for bad in ("not json", "[1, 2]", '"text"', ""):
        with pytest.raises(LLMResponseError):
            parse_json_object(bad)


# ------------------------------------------------- OpenAI-compatible adapter
def make_client(handler, timeout=5.0):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleClient(
        api_key=SECRET, model="test-model", base_url="https://llm.test/v1",
        timeout=timeout, http_client=http,
    )


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
        },
    )


def test_adapter_is_deterministic_json_only_and_returns_the_object():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return completion('{"concepts": []}')

    out = make_client(handler).complete_json(task="extract", system="SYS", user="USR")
    assert out == {"concepts": []}
    body = seen["body"]
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == "test-model"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert seen["url"].startswith("https://llm.test/v1/chat/completions")
    assert seen["auth"] == f"Bearer {SECRET}"  # the key comes from settings, sent as a header only


def test_adapter_maps_timeout_http_errors_and_bad_json_without_leaking_the_key():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMTimeout):
        make_client(timeout).complete_json(task="t", system="s", user="u")

    def server_error(request):
        return httpx.Response(500, json={"error": {"message": f"boom {SECRET}"}})

    with pytest.raises(LLMError) as e1:
        make_client(server_error).complete_json(task="t", system="s", user="u")
    assert SECRET not in str(e1.value) and SECRET not in repr(e1.value)

    def auth_error(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(LLMError):
        make_client(auth_error).complete_json(task="t", system="s", user="u")

    with pytest.raises(LLMResponseError):
        make_client(lambda r: completion("이건 JSON이 아니다")).complete_json(task="t", system="s", user="u")


def test_full_hybrid_flow_through_the_adapter_and_http_layer(tmp_path):
    """Analyzer -> OpenAICompatibleClient -> (mock) HTTP: still no real network."""
    from fake_llm import KOREAN_NARRATIVE, parse_text

    answer = {
        "concepts": [
            {"name": "광합성", "category": "process", "quote": "광합성 덕분에 식물은 스스로 양분을 얻는다."},
        ]
    }

    def handler(request):
        task_is_topics = "label groups of sections" in json.loads(request.content)["messages"][0]["content"]
        return completion(json.dumps({"topics": []} if task_is_topics else answer, ensure_ascii=False))

    m = parse_text(tmp_path, KOREAN_NARRATIVE)
    a = HybridAnalyzer(make_client(handler)).analyze(m)
    assert [c.name for c in a.concepts] == ["광합성"]
    assert a.analyzer_info.llm_model == "test-model" and a.analyzer_info.llm_calls == 2

    # and an HTTP failure degrades to the heuristic result instead of failing
    def down(request):
        return httpx.Response(503, json={})

    b = HybridAnalyzer(make_client(down)).analyze(m)
    assert b.analyzer_info.fallback and b.analyzer_info.fallback_reason == "error"
