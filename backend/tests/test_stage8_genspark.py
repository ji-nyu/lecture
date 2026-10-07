"""STAGE 8: the GensparkProvider drives the official Genspark CLI (`gsk`).

The real CLI cannot be used here (its `task create` commands are generated from a schema that is only
downloaded with a valid API key, and a real run is billed), so these tests run the provider against a
FAKE `gsk` executable that follows the documented command line: `gsk [--no-input] [--base-url U] task
create slides --args-file F --follow -o FILE` and `gsk task export <project_id> --format pptx -o FILE`.
What they prove is our side of the contract; they do not prove Genspark's side.
"""

import ast
import io
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pptx import Presentation

import app
from app.config import Settings, load_settings
from app.errors import PresentationGenerationFailed, PresentationProviderUnavailable
from app.llm.mock import MockLLMClient
from app.main import create_app
from app.models.presentation import JobState, PresentationRequest, SlideBrief
from app.providers import genspark as gs
from app.providers.genspark import GensparkProvider, build_instructions, build_query

from plan_helpers import CASE_B
from test_stage6a_api import approved, project

APP_DIR = Path(app.__file__).parent
KEY = "test-key-not-a-real-credential"

FAKE_GSK = r'''
import json, os, sys, time
argv = sys.argv[1:]
mode = os.environ.get("FAKE_GSK_MODE", "ok")
key = os.environ.get("GSK_API_KEY", "")
with open("calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({
        "argv": argv,
        "key_ok": key == os.environ.get("FAKE_GSK_EXPECT_KEY", ""),
        "key_set": "GSK_API_KEY" in os.environ,
        "no_update": os.environ.get("GSK_NO_AUTO_UPDATE"),
        "llm_keys_visible": ("LLM_API_KEY" in os.environ) or ("OPENAI_API_KEY" in os.environ),
    }) + "\n")
if key != os.environ.get("FAKE_GSK_EXPECT_KEY", ""):
    print("[ERROR] API key is required", file=sys.stderr)
    sys.exit(1)

def deck(path, n):
    from pptx import Presentation
    p = Presentation()
    for i in range(n):
        p.slides.add_slide(p.slide_layouts[5]).shapes.title.text = "S%d" % (i + 1)
    p.save(path)

def out_path():
    return argv[argv.index("-o") + 1] if "-o" in argv else None

n = int(os.environ.get("FAKE_GSK_SLIDES", "3"))

if "export" in argv:  # gsk task export <project_id> --format pptx -o <file>
    if mode == "export_fails":
        print("[ERROR] export failed", file=sys.stderr)
        sys.exit(1)
    if mode == "bad_pptx":
        open(out_path(), "wb").write(b"this is not a pptx")
        print(json.dumps({"status": "ok", "data": {"format": "pptx"}}))
        sys.exit(0)
    deck(out_path(), n)
    print(json.dumps({"status": "ok", "data": {"format": "pptx"}}))
    sys.exit(0)

if "status" in argv:  # gsk task status <run_id>
    polls = 0
    if os.path.exists("polls.txt"):
        polls = int(open("polls.txt", encoding="utf-8").read() or "0")
    polls += 1
    open("polls.txt", "w", encoding="utf-8").write(str(polls))
    if mode == "hang":
        time.sleep(600)
    state = "failed" if mode == "task_failed" else ("running" if polls < 2 else "succeeded")
    print(json.dumps({"status": "ok", "data": {"state": state, "project_id": "proj-123"}}))
    sys.exit(0)

# gsk task create slides --args-file ...
time.sleep(float(os.environ.get("FAKE_GSK_DELAY", "0")))
if mode == "fail":
    print("[ERROR] boom " + key, file=sys.stderr)
    sys.exit(1)
if mode == "hang":
    time.sleep(600)
if mode == "error_json":
    print(json.dumps({"status": "error", "message": "task ended with status failed"}))
    sys.exit(0)
project_id = None if mode == "no_project" else "proj-123"
run_id = None if mode == "no_project" else "sb_task_run::1"
print(json.dumps({"status": "ok", "message": "success",
                  "data": {"status": "submitted", "project_id": project_id, "run_id": run_id}}))
if mode in ("ok_exit_crash", "crash_after_json"):
    sys.exit(3221226505 % 256)
'''


@pytest.fixture(autouse=True)
def isolated_login(tmp_path, monkeypatch):
    """The tests must not depend on whether this machine ran `gsk login` or has a key in its environment."""
    monkeypatch.delenv("GSK_API_KEY", raising=False)
    monkeypatch.setenv("GSK_CONFIG", str(tmp_path / "no-login" / "config.json"))


@pytest.fixture()
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "fake_gsk.py"
    script.write_text(FAKE_GSK, encoding="utf-8")
    if os.name == "nt":
        cli = tmp_path / "fake_gsk.cmd"
        cli.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        cli = tmp_path / "fake_gsk"
        cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_GSK_EXPECT_KEY", KEY)
    for name in ("FAKE_GSK_MODE", "FAKE_GSK_SLIDES", "FAKE_GSK_DELAY"):
        monkeypatch.delenv(name, raising=False)
    return str(cli)


def make_provider(tmp_path, cli, **kw):
    kw.setdefault("api_key", KEY)
    kw.setdefault("poll_seconds", 0.05)
    return GensparkProvider(tmp_path / "jobs", cli_command=cli, **kw)


def request(n=3, prompt="# 프롬프트\n본문 $HOME `x` \"q\"\n"):
    slides = [SlideBrief(slide_number=i, title=f"제목 {i}", slide_type="concept") for i in range(1, n + 1)]
    return PresentationRequest(
        project_id="a" * 32, title="MQTT 입문", final_prompt=prompt, prompt_hash="f" * 64,
        slide_count=n, slides=slides, duration_minutes=60,
    )


def wait_done(provider, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        st = provider.get_status(job_id)
        if st.state in (JobState.completed, JobState.failed):
            return st
        time.sleep(0.3)
    raise AssertionError("the job did not finish")


def calls(provider, job_id):
    text = (provider.root / job_id / "calls.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def run(tmp_path, cli, mode=None, monkeypatch=None, **kw):
    if mode:
        monkeypatch.setenv("FAKE_GSK_MODE", mode)
    p = make_provider(tmp_path, cli, **kw)
    job = p.create_presentation(request())
    return p, job.job_id, wait_done(p, job.job_id)


# ------------------------------------------------------------------ availability
def test_not_configured_without_the_cli(tmp_path):
    p = make_provider(tmp_path, str(tmp_path / "no-such-gsk"))
    av = p.availability()
    assert av.configured is False and "gsk" in av.message and "npm install -g @genspark/cli" in av.message
    with pytest.raises(PresentationProviderUnavailable) as e:
        p.create_presentation(request())
    assert e.value.message == av.message
    assert not (tmp_path / "jobs").exists()  # nothing was started


def test_not_configured_without_a_key(tmp_path, fake_cli):
    for key in (None, ""):
        av = make_provider(tmp_path, fake_cli, api_key=key).availability()
        assert av.configured is False and "GENSPARK_API_KEY" in av.message and "gsk login" in av.message


def test_a_gsk_login_is_enough_without_any_key(tmp_path, fake_cli, monkeypatch):
    """`gsk login` saves the credentials in the CLI's own config file; the CLI finds them itself."""
    login = tmp_path / "home" / "config.json"
    login.parent.mkdir()
    login.write_text("{}", encoding="utf-8")  # only the existence of the file matters; it is never opened
    monkeypatch.setenv("GSK_CONFIG", str(login))
    monkeypatch.setenv("GSK_API_KEY", "a-key-from-the-server-environment")  # not ours: must not be forwarded
    monkeypatch.delenv("FAKE_GSK_EXPECT_KEY")
    p = make_provider(tmp_path, fake_cli, api_key=None)
    assert p.availability().configured is True
    job = p.create_presentation(request())
    assert wait_done(p, job.job_id).state == JobState.completed
    cs = calls(p, job.job_id)
    assert cs and all(c["key_set"] is False and c["key_ok"] is True for c in cs)
    assert cs[0]["argv"][:4] == ["--no-input", "task", "create", "slides"]
    assert p.availability().configured is True
    (tmp_path / "no-login").mkdir()
    login.unlink()
    assert make_provider(tmp_path, fake_cli, api_key=None).availability().configured is False


def test_the_login_file_is_found_at_the_documented_default_location(tmp_path, fake_cli, monkeypatch):
    monkeypatch.delenv("GSK_CONFIG")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fakehome"))
    p = make_provider(tmp_path, fake_cli, api_key=None)
    assert p.availability().configured is False
    cfg = tmp_path / "fakehome" / ".genspark-tool-cli" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}", encoding="utf-8")
    assert p.availability().configured is True


def test_not_configured_with_a_malformed_url(tmp_path, fake_cli):
    for url in ("javascript:alert(1)", "--base-url", "ftp://x", "www.example.com"):
        av = make_provider(tmp_path, fake_cli, api_url=url).availability()
        assert av.configured is False and "GENSPARK_API_URL" in av.message


def test_configured_with_cli_and_key(tmp_path, fake_cli):
    assert make_provider(tmp_path, fake_cli).availability().configured is True
    assert make_provider(tmp_path, fake_cli, api_url="https://example.test").availability().configured is True


def test_the_key_is_never_part_of_a_repr(tmp_path, fake_cli):
    p = make_provider(tmp_path, fake_cli)
    assert KEY not in repr(p) and KEY not in str(p)
    s = Settings(data_dir=tmp_path, max_upload_mb=1, cors_origins=(), genspark_api_key=KEY)
    assert KEY not in repr(s)


# ------------------------------------------------------------------ a successful job
def test_a_job_runs_the_documented_command_and_delivers_the_deck(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "some-other-secret")  # must not reach the CLI
    p = make_provider(tmp_path, fake_cli)
    job = p.create_presentation(request())
    assert re.fullmatch(r"gsk-[0-9a-f]{32}", job.job_id) and job.state == JobState.queued
    st = wait_done(p, job.job_id)
    assert st.state == JobState.completed and st.progress == 100 and st.message is None

    res = p.get_result(job.job_id)
    assert res.slide_count == 3 and res.file_name.endswith(".pptx") and "presentationml" in res.content_type
    data = p.download(job.job_id)
    assert res.size_bytes == len(data.data) > 0
    assert len(Presentation(io.BytesIO(data.data)).slides) == 3

    cs = calls(p, job.job_id)
    assert cs[0]["argv"] == ["--no-input", "task", "create", "slides", "--args-file", "args.json"]
    assert cs[0]["key_ok"] is True and cs[0]["no_update"] == "1" and cs[0]["llm_keys_visible"] is False
    assert any(a[1:3] == ["task", "status"] for a in (c["argv"] for c in cs))
    assert cs[-1]["argv"] == ["--no-input", "task", "export", "proj-123", "--format", "pptx", "-o", "output.pptx"]
    assert all("--follow" not in c["argv"] for c in cs)


def test_the_key_is_only_ever_in_the_environment(tmp_path, fake_cli):
    p, job_id, st = run(tmp_path, fake_cli)
    assert st.state == JobState.completed
    for f in (tmp_path / "jobs" / job_id).iterdir():
        assert KEY.encode() not in f.read_bytes(), f.name  # not on a command line, not in a file


def test_a_job_survives_a_restart(tmp_path, fake_cli):
    p, job_id, _ = run(tmp_path, fake_cli)
    again = make_provider(tmp_path, fake_cli)  # a new process
    assert again.get_status(job_id).state == JobState.completed
    assert again.download(job_id).data
    # ...and it does not even need the CLI or the key any more to be read
    bare = GensparkProvider(tmp_path / "jobs", cli_command=str(tmp_path / "gone"), api_key=None)
    assert bare.get_status(job_id).state == JobState.completed


def test_the_base_url_is_passed_as_the_documented_global_flag(tmp_path, fake_cli):
    p, job_id, st = run(tmp_path, fake_cli, api_url="https://example.test/base")
    assert st.state == JobState.completed
    argv = calls(p, job_id)[0]["argv"]
    assert argv[:3] == ["--no-input", "--base-url", "https://example.test/base"] and argv[3:5] == ["task", "create"]


def test_the_parameters_travel_in_an_args_file_not_on_the_command_line(tmp_path, fake_cli):
    prompt = "# " + "긴 프롬프트 $HOME `x` \"q\"\n" * 8000  # ~150k characters, shell-hostile
    p = make_provider(tmp_path, fake_cli)
    job = p.create_presentation(request(5, prompt))
    wait_done(p, job.job_id)
    args = json.loads((tmp_path / "jobs" / job.job_id / "args.json").read_text(encoding="utf-8"))
    assert set(args) == {"task_name", "query", "instructions"}
    assert args["task_name"] == "MQTT 입문"
    assert args["query"].startswith(prompt) and args["query"].rstrip().endswith("정확히 5장의 프레젠테이션(슬라이드 덱)을 만들어 주세요.")
    argv = calls(p, job.job_id)[0]["argv"]
    assert max(len(a) for a in argv) < 50  # only fixed tokens


def test_the_instructions_give_genspark_only_the_rendering_role():
    text = build_instructions(request(4))
    assert "정확히 4장" in text and "추가, 삭제, 병합, 분할하지 않습니다" in text
    for do in ("시각 레이아웃", "슬라이드 렌더링", "이미지 선택", "프레젠테이션 스타일링"):
        assert do in text
    for dont in ("강의 구조", "중요 개념", "섹션 시간", "난이도"):
        assert dont in text
    assert text.index("1. 제목 1") < text.index("2. 제목 2") < text.index("4. 제목 4")
    assert "새로운 사실" in text
    assert "빈 상자" in text
    assert build_query(request(4, "PROMPT")).startswith("PROMPT\n")


def test_results_and_downloads_are_refused_while_running(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_GSK_DELAY", "4")
    p = make_provider(tmp_path, fake_cli)
    job = p.create_presentation(request())
    assert p.get_status(job.job_id).state in (JobState.queued, JobState.running)
    for call in (p.get_result, p.download):
        with pytest.raises(PresentationGenerationFailed):
            call(job.job_id)
    assert wait_done(p, job.job_id).state == JobState.completed


# ------------------------------------------------------------------ when the CLI's `-o` does not export
def test_a_submitted_task_is_polled_then_exported(tmp_path, fake_cli, monkeypatch):
    p, job_id, st = run(tmp_path, fake_cli, "ok_no_export", monkeypatch)
    assert st.state == JobState.completed
    first, *middle, last = calls(p, job_id)
    assert first["argv"][:4] == ["--no-input", "task", "create", "slides"]
    assert last["argv"] == ["--no-input", "task", "export", "proj-123", "--format", "pptx", "-o", "output.pptx"]
    assert p.get_result(job_id).slide_count == 3


def test_a_valid_deck_counts_even_if_the_cli_crashes_on_exit(tmp_path, fake_cli, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="ailecturegen")
    p, job_id, st = run(tmp_path, fake_cli, "ok_exit_crash", monkeypatch)
    assert st.state == JobState.completed and p.get_result(job_id).slide_count == 3
    assert "delivered a valid PPTX" in caplog.text
    assert any(c["argv"][1:3] == ["task", "export"] for c in calls(p, job_id))


def test_the_export_fallback_also_runs_after_a_crash(tmp_path, fake_cli, monkeypatch):
    p, job_id, st = run(tmp_path, fake_cli, "crash_after_json", monkeypatch)
    assert st.state == JobState.completed
    assert any(c["argv"][:4] == ["--no-input", "task", "export", "proj-123"] for c in calls(p, job_id))


# ------------------------------------------------------------------ failures
@pytest.mark.parametrize("mode, needle", [
    ("fail", "종료 코드 1"),
    ("error_json", "작업 실패를 알려"),
    ("no_project", "PPTX 파일을 받지 못했"),
    ("export_fails", "PPTX 파일을 받지 못했"),
    ("bad_pptx", "올바른 PPTX가 아닙니다"),
])
def test_every_way_of_not_getting_a_deck_is_a_failure(tmp_path, fake_cli, monkeypatch, mode, needle):
    p, job_id, st = run(tmp_path, fake_cli, mode, monkeypatch)
    assert st.state == JobState.failed and needle in st.message
    for call in (p.get_result, p.download):
        with pytest.raises(PresentationGenerationFailed) as e:
            call(job_id)
        assert needle in e.value.message


def test_a_wrong_key_is_a_failure_without_details(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_GSK_EXPECT_KEY", "some-other-key")
    p, job_id, st = run(tmp_path, fake_cli)
    assert st.state == JobState.failed and "API 키" in st.message and "종료 코드 1" in st.message
    assert "required" not in st.message  # the CLI's own words are not passed on


def test_cli_output_never_reaches_the_client_and_the_key_is_redacted_in_logs(tmp_path, fake_cli, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="ailecturegen")
    p, job_id, st = run(tmp_path, fake_cli, "fail", monkeypatch)
    assert "boom" not in st.message and KEY not in st.message
    assert "boom" in caplog.text  # the server log has the CLI's message...
    assert KEY not in caplog.text and "***" in caplog.text  # ...with the key removed


def test_a_job_that_takes_too_long_is_stopped(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("FAKE_GSK_MODE", "hang")
    p = make_provider(tmp_path, fake_cli, timeout_seconds=3)
    job = p.create_presentation(request())
    st = wait_done(p, job.job_id)
    assert st.state == JobState.failed and "제한 시간" in st.message


def test_a_dead_runner_is_detected_by_its_missing_heartbeat(tmp_path, fake_cli):
    p = make_provider(tmp_path, fake_cli)
    job_id = "gsk-" + "1" * 32
    d = tmp_path / "jobs" / job_id
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({"job_id": job_id, "created_at": time.time() - 3600, "timeout_seconds": 60}))
    st = p.get_status(job_id)
    assert st.state == JobState.failed and "중단" in st.message
    (d / "job.json").write_text(json.dumps({"job_id": job_id, "created_at": time.time(), "timeout_seconds": 60}))
    assert p.get_status(job_id).state == JobState.queued  # just started
    (d / "heartbeat.txt").write_text(str(time.time()))
    assert p.get_status(job_id).state == JobState.running  # a live runner


@pytest.mark.parametrize("job_id", ["../x", "gsk-../../etc", "gsk-123", "", "mock-" + "0" * 32, "gsk-" + "g" * 32])
def test_bad_job_ids_are_rejected(tmp_path, fake_cli, job_id):
    p = make_provider(tmp_path, fake_cli)
    for call in (p.get_status, p.get_result, p.download):
        with pytest.raises(PresentationGenerationFailed):
            call(job_id)


def test_an_unknown_job_is_a_failure_not_a_crash(tmp_path, fake_cli):
    with pytest.raises(PresentationGenerationFailed):
        make_provider(tmp_path, fake_cli).get_status("gsk-" + "0" * 32)


# ------------------------------------------------------------------ settings and selection
def test_settings_read_the_genspark_variables(monkeypatch):
    monkeypatch.setenv("GENSPARK_API_KEY", KEY)
    monkeypatch.setenv("GENSPARK_API_URL", "https://example.test")
    monkeypatch.setenv("GENSPARK_CLI_PATH", "/opt/gsk")
    monkeypatch.setenv("GENSPARK_TIMEOUT_SECONDS", "999999")
    s = load_settings()
    assert (s.genspark_api_key, s.genspark_api_url, s.genspark_cli_path) == (KEY, "https://example.test", "/opt/gsk")
    assert s.genspark_timeout_seconds == 7200  # clamped
    for name in ("GENSPARK_API_KEY", "GENSPARK_API_URL", "GENSPARK_CLI_PATH", "GENSPARK_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name)
    s = load_settings()
    assert (s.genspark_api_key, s.genspark_api_url, s.genspark_cli_path, s.genspark_timeout_seconds) == (None, None, None, 2100)


def test_the_factory_passes_the_settings_on(tmp_path, fake_cli):
    from app.providers.factory import create_provider

    s = Settings(data_dir=tmp_path, max_upload_mb=1, cors_origins=(), genspark_mode="genspark",
                 genspark_api_key=KEY, genspark_cli_path=fake_cli, genspark_timeout_seconds=123)
    p = create_provider(s)
    assert isinstance(p, GensparkProvider) and p.availability().configured is True
    assert p.timeout_seconds == 123 and p.root == tmp_path / "genspark_jobs"


# ------------------------------------------------------------------ nothing invented
def test_the_module_talks_to_genspark_only_through_the_cli_and_invents_no_endpoint():
    src = (APP_DIR / "providers" / "genspark.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level:
            imports.add(node.module)
    assert not {"httpx", "requests", "urllib", "urllib.request", "socket", "openai", "aiohttp", "http.client"} & imports
    # no URL literal at all (an endpoint would be a guess): the only URL pattern is the validation regex
    urls = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value != ast.get_docstring(tree, clean=False) and re.search(r"https?://\w", n.value)]
    assert urls == []


# ------------------------------------------------------------------ through the service and the API
def api_client(tmp_path, cli, **kw):
    s = Settings(data_dir=tmp_path, max_upload_mb=5, cors_origins=(), enrichment_provider="mock",
                 genspark_mode="genspark", genspark_api_key=KEY, genspark_cli_path=cli,
                 genspark_poll_seconds=0.05, **kw)
    return TestClient(create_app(s, content_client=MockLLMClient()), raise_server_exceptions=False)


def ready(c):
    pid = approved(c, **CASE_B)
    assert c.post(f"/projects/{pid}/prompt").status_code == 200
    return pid


def poll(c, pid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = c.get(f"/projects/{pid}/presentation/status").json()
        if s["state"] in ("completed", "failed"):
            return s
        time.sleep(0.3)
    raise AssertionError("no result")


def test_end_to_end_through_the_api(tmp_path, fake_cli, monkeypatch):
    c = api_client(tmp_path, fake_cli)
    pid = ready(c)
    n = c.get(f"/projects/{pid}/slides").json()["slide_count"]
    monkeypatch.setenv("FAKE_GSK_SLIDES", str(n))
    monkeypatch.setenv("FAKE_GSK_DELAY", "1")

    s = c.post(f"/projects/{pid}/presentation").json()
    assert s["provider"] == "genspark" and s["is_mock"] is False and s["provider_configured"] is True
    assert s["state"] in ("queued", "running") and s["project_status"] == "generating" and s["has_result"] is False
    assert c.get(f"/projects/{pid}/presentation/download").status_code == 409  # not yet

    done = poll(c, pid)
    assert done["state"] == "completed" and done["project_status"] == "completed" and done["has_result"] is True
    res = c.get(f"/projects/{pid}/presentation/result").json()
    assert res["provider"] == "genspark" and res["is_mock"] is False and res["slide_count"] == n
    assert res["note"] is None and "MOCK" not in res["file_name"] and res["file_name"].endswith(".pptx")
    dl = c.get(f"/projects/{pid}/presentation/download")
    assert dl.status_code == 200 and len(Presentation(io.BytesIO(dl.content)).slides) == n
    assert project(c, pid)["presentation_provider"] == "genspark"
    assert KEY not in c.get(f"/projects/{pid}").text and KEY not in c.get(f"/projects/{pid}/presentation/status").text
    assert not (tmp_path / "mock_presentations").exists()  # the mock was never involved
    assert KEY not in (tmp_path / "projects" / pid / "project.json").read_text(encoding="utf-8")


def test_a_slide_count_different_from_the_plan_is_reported(tmp_path, fake_cli, monkeypatch):
    c = api_client(tmp_path, fake_cli)
    pid = ready(c)
    n = c.get(f"/projects/{pid}/slides").json()["slide_count"]
    monkeypatch.setenv("FAKE_GSK_SLIDES", str(n + 2))
    c.post(f"/projects/{pid}/presentation")
    assert poll(c, pid)["state"] == "completed"
    note = c.get(f"/projects/{pid}/presentation/result").json()["note"]
    assert str(n + 2) in note and str(n) in note and "다릅니다" in note


def test_a_failed_genspark_job_leaves_the_lecture_intact_and_can_be_retried(tmp_path, fake_cli, monkeypatch):
    c = api_client(tmp_path, fake_cli)
    pid = ready(c)
    prompt = c.get(f"/projects/{pid}/prompt").json()
    monkeypatch.setenv("FAKE_GSK_MODE", "fail")
    assert c.post(f"/projects/{pid}/presentation").status_code == 200
    s = poll(c, pid)
    assert s["state"] == "failed" and s["project_status"] == "failed" and "종료 코드 1" in s["message"]
    assert KEY not in json.dumps(s) and "boom" not in json.dumps(s)
    assert c.get(f"/projects/{pid}/prompt").json() == prompt and project(c, pid)["has_presentation"] is False

    monkeypatch.setenv("FAKE_GSK_MODE", "ok")
    monkeypatch.setenv("FAKE_GSK_SLIDES", str(prompt["slide_count"]))
    assert c.post(f"/projects/{pid}/presentation").status_code == 200
    assert poll(c, pid)["state"] == "completed"


def test_without_the_cli_the_api_answers_not_configured_and_changes_nothing(tmp_path):
    c = api_client(tmp_path, str(tmp_path / "no-such-gsk"))
    pid = ready(c)
    st = c.get(f"/projects/{pid}/presentation/status").json()
    assert st["provider_configured"] is False and "gsk" in st["provider_message"]
    r = c.post(f"/projects/{pid}/presentation")
    assert r.status_code == 503 and r.json()["error"]["code"] == "PresentationProviderUnavailable"
    assert project(c, pid)["presentation_status"] == "ready_for_presentation"


def test_without_a_key_the_api_names_the_missing_setting(tmp_path, fake_cli):
    s = Settings(data_dir=tmp_path, max_upload_mb=5, cors_origins=(), enrichment_provider="mock",
                 genspark_mode="genspark", genspark_cli_path=fake_cli)
    c = TestClient(create_app(s, content_client=MockLLMClient()), raise_server_exceptions=False)
    pid = ready(c)
    r = c.post(f"/projects/{pid}/presentation")
    assert r.status_code == 503 and "GENSPARK_API_KEY" in r.json()["error"]["message"]
    assert not (tmp_path / "genspark_jobs").exists()
