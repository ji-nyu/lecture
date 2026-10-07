"""Lecture-video intro: shared folder, options, prepended mux."""

from pathlib import Path

import pytest

from app.models.lecture_profile import LectureOptionsInput, VideoIntro
from app.services.lecture_profile_service import build_profile, profile_content_changed
from app.services.lecture_video import ffmpeg_exe, prepend_intro
from app.services.slide_content_enricher import hash_profile

from plan_helpers import BASE, CASE_B
from test_lecture_video import video_client
from test_stage7_api import generated


def _tiny_mp4(path: Path, seconds: float = 0.4, color: str = "blue") -> Path:
    exe = ffmpeg_exe()
    import subprocess

    proc = subprocess.run(
        [
            exe, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={seconds}",
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100:d={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", "2", "-ar", "44100",
            "-shortest",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "ffmpeg failed")
    return path


def test_video_intros_start_empty_then_accept_an_upload(tmp_path: Path):
    c = video_client(tmp_path)
    empty = c.get("/video-intros")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    r = c.post(
        "/video-intros",
        files={"file": ("school-intro.mp4", b"not-really-video", "video/mp4")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "school-intro.mp4"
    assert [x["filename"] for x in body["items"]] == ["school-intro.mp4"]
    listed = c.get("/video-intros").json()["items"]
    assert listed[0]["filename"] == "school-intro.mp4"


def test_include_intro_requires_a_file(tmp_path: Path):
    c = video_client(tmp_path)
    pid = c.post("/projects", json={"title": "인트로"}).json()["id"]
    r = c.put(f"/projects/{pid}/profile", json={**BASE, "video_intro": "include"})
    assert r.status_code == 422
    assert "인트로" in r.json()["error"]["message"]


def test_intro_only_profile_change_does_not_wipe_the_presentation(tmp_path: Path):
    c = video_client(tmp_path)
    c.post("/video-intros", files={"file": ("open.mp4", b"clip", "video/mp4")})
    pid, _ = generated(c)
    before = c.get(f"/projects/{pid}").json()
    assert before["has_presentation"] is True
    r = c.put(
        f"/projects/{pid}/profile",
        json={**BASE, **CASE_B, "video_intro": "include", "video_intro_file": "open.mp4"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["video_intro"] == "include"
    assert r.json()["video_intro_file"] == "open.mp4"
    after = c.get(f"/projects/{pid}").json()
    assert after["has_presentation"] is True
    assert after["has_plan"] is True
    assert after["has_video"] is False


def test_video_job_prepends_the_chosen_intro(tmp_path: Path):
    c = video_client(tmp_path)
    c.post("/video-intros", files={"file": ("open.mp4", b"clip", "video/mp4")})
    pid, _ = generated(c, video_intro="include", video_intro_file="open.mp4")
    assert c.get(f"/projects/{pid}/scripts").status_code == 200
    r = c.post(f"/projects/{pid}/video")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "completed"
    assert r.json()["duration_seconds"] >= 1
    dl = c.get(f"/projects/{pid}/video/download")
    assert dl.status_code == 200
    assert b"+INTRO" in dl.content
    jobs = list((tmp_path / "video_jobs").glob("*/job.json"))
    assert jobs
    payload = jobs[0].read_text(encoding="utf-8")
    assert "open.mp4" in payload
    assert "intro_path" in payload


def test_missing_intro_file_blocks_video(tmp_path: Path):
    c = video_client(tmp_path)
    pid, _ = generated(c, video_intro="include", video_intro_file="gone.mp4")
    r = c.post(f"/projects/{pid}/video")
    assert r.status_code == 502
    assert "인트로" in r.json()["error"]["message"]


def test_hash_profile_ignores_intro_settings():
    a = build_profile("p" * 32, LectureOptionsInput(**BASE))
    b = build_profile(
        "p" * 32,
        LectureOptionsInput(**BASE, video_intro=VideoIntro.include, video_intro_file="open.mp4"),
    )
    assert a.video_intro == VideoIntro.none
    assert b.video_intro == VideoIntro.include
    assert hash_profile(a) == hash_profile(b)
    assert profile_content_changed(a, b) is False


def test_prepend_intro_plays_first(tmp_path: Path):
    try:
        ffmpeg_exe()
    except RuntimeError:
        pytest.skip("ffmpeg가 없습니다")
    from app.services.lecture_video import audio_seconds

    intro = _tiny_mp4(tmp_path / "intro.mp4", 0.5, "red")
    lecture = _tiny_mp4(tmp_path / "lecture.mp4", 0.7, "green")
    extra = prepend_intro(intro, lecture)
    assert extra >= 0.4
    assert lecture.is_file() and lecture.stat().st_size > 32
    assert audio_seconds(lecture) >= 1.0
