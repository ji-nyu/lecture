"""Start / poll / download a lecture video built from the finished PPT and scripts."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from ..errors import (
    PresentationNotReady,
    VideoGenerationFailed,
    VideoInProgress,
    VideoNotReady,
)
from ..models.lecture_script import LectureScript
from ..models.lecture_video import VideoJob, VideoResult, VideoState, VideoStatusResponse
from ..models.source import SourceMaterial
from ..storage.project_store import ProjectStore, utcnow
from .lecture_script_writer import SCRIPT_CACHE, LectureScriptWriter, script_fingerprint
from .lecture_video import (
    JOB_NAME,
    OUTPUT_NAME,
    RESULT_NAME,
    VideoPipeline,
    launch_thread,
    read_progress,
    video_fingerprint,
)
from .llm_client import LLMClient, create_llm_client
from .presentation_service import PresentationService, _download_name

logger = logging.getLogger("ailecturegen")


class VideoService:
    def __init__(
        self,
        store: ProjectStore,
        *,
        jobs_root: Path,
        presentation: PresentationService,
        pipeline: VideoPipeline | None = None,
        launcher=None,
        llm: LLMClient | None = None,
        settings=None,
    ):
        self.store = store
        self.jobs_root = Path(jobs_root)
        self.presentation = presentation
        self.pipeline = pipeline or VideoPipeline()
        self.launcher = launcher or (lambda d: launch_thread(d, self.pipeline))
        self.llm = llm
        self.settings = settings

    def start(self, project_id: str, *, force: bool = False) -> VideoStatusResponse:
        project = self.store.get(project_id)
        job = project.video_job
        if job is not None and job.state in (VideoState.queued, VideoState.running):
            raise VideoInProgress()
        pptx, _ = self.presentation.download_path(project_id)
        if not pptx.is_file():
            raise PresentationNotReady("완성된 PPT 파일을 찾을 수 없습니다. 먼저 PPT를 만들어 주세요.")
        script = self._scripts(project_id)
        if not script.slides:
            raise VideoGenerationFailed("읽을 대본이 없습니다.")
        intro_path = self._intro_path(project)
        fp = video_fingerprint(pptx, script, intro_path)
        if (
            not force
            and project.video_result is not None
            and project.video_result.fingerprint == fp
            and (self.store.video_dir(project_id) / OUTPUT_NAME).is_file()
        ):
            return self.status(project_id)

        job_id = f"vid-{uuid.uuid4().hex}"
        job_dir = self.jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "job_id": job_id,
            "project_id": project.id,
            "pptx_path": str(pptx.resolve()),
            "title": project.title or script.title,
            "fingerprint": fp,
            "scripts": [
                {
                    "slide_number": s.slide_number,
                    "title": s.title,
                    "text": s.text,
                    "estimated_seconds": s.estimated_seconds,
                }
                for s in script.slides
            ],
            "intro_path": str(intro_path.resolve()) if intro_path else None,
        }
        (job_dir / JOB_NAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        now = utcnow()
        fresh = self.store.get(project_id)
        fresh.video_job = VideoJob(
            job_id=job_id, state=VideoState.running, progress=1,
            stage="강의 영상을 시작합니다.", fingerprint=fp, created_at=now, updated_at=now,
        )
        fresh.video_result = None
        self.store.save(fresh)
        try:
            self.launcher(job_dir)
        except Exception as exc:
            logger.exception("Video job failed to start")
            failed = self.store.get(project_id)
            if failed.video_job and failed.video_job.job_id == job_id:
                failed.video_job = failed.video_job.model_copy(
                    update={"state": VideoState.failed, "message": "강의 영상을 시작하지 못했습니다.", "updated_at": utcnow()}
                )
                self.store.save(failed)
            raise VideoGenerationFailed() from exc
        return self.status(project_id)

    def status(self, project_id: str) -> VideoStatusResponse:
        project = self.store.get(project_id)
        job = project.video_job
        if job is None:
            return VideoStatusResponse(state="not_started", has_result=project.video_result is not None)
        if job.state in (VideoState.queued, VideoState.running):
            project = self._advance(project, job)
            job = project.video_job
        result = project.video_result
        return VideoStatusResponse(
            state=job.state.value if job else "not_started",
            progress=job.progress if job else 0,
            stage=job.stage if job else None,
            message=job.message if job else None,
            has_result=result is not None,
            duration_seconds=result.duration_seconds if result else None,
            slide_count=result.slide_count if result else None,
            job_id=job.job_id if job else None,
            created_at=job.created_at if job else None,
            updated_at=job.updated_at if job else None,
        )

    def download_path(self, project_id: str) -> tuple[Path, VideoResult]:
        project = self.store.get(project_id)
        result = project.video_result
        if result is None:
            raise VideoNotReady()
        path = self.store.video_dir(project_id) / OUTPUT_NAME
        if not path.is_file():
            raise VideoNotReady("영상 파일을 찾을 수 없습니다. 다시 만들어 주세요.")
        return path, result

    def _advance(self, project, job: VideoJob):
        job_dir = self.jobs_root / job.job_id
        report = _read_json(job_dir / RESULT_NAME)
        if report is None:
            progress = read_progress(job_dir)
            stale = _heartbeat_stale(job_dir)
            fresh = self.store.get(project.id)
            if fresh.video_job is None or fresh.video_job.job_id != job.job_id:
                return fresh
            if stale:
                fresh.video_job = fresh.video_job.model_copy(update={
                    "state": VideoState.failed,
                    "message": "영상 작업이 중단되었습니다. 다시 만들어 주세요.",
                    "updated_at": utcnow(),
                })
                fresh.video_result = None
                return self.store.save(fresh)
            fresh.video_job = fresh.video_job.model_copy(update={
                "progress": int(progress.get("progress") or job.progress),
                "stage": progress.get("stage") or job.stage,
                "updated_at": utcnow(),
            })
            return self.store.save(fresh)
        if not report.get("ok"):
            fresh = self.store.get(project.id)
            if fresh.video_job is None or fresh.video_job.job_id != job.job_id:
                return fresh
            fresh.video_job = fresh.video_job.model_copy(update={
                "state": VideoState.failed,
                "message": report.get("error") or VideoGenerationFailed.message,
                "updated_at": utcnow(),
            })
            fresh.video_result = None
            return self.store.save(fresh)
        return self._finish(project, job, job_dir, report)

    def _finish(self, project, job: VideoJob, job_dir: Path, report: dict):
        src = job_dir / OUTPUT_NAME
        if not src.is_file():
            fresh = self.store.get(project.id)
            if fresh.video_job and fresh.video_job.job_id == job.job_id:
                fresh.video_job = fresh.video_job.model_copy(update={
                    "state": VideoState.failed,
                    "message": "영상 파일이 만들어지지 않았습니다.",
                    "updated_at": utcnow(),
                })
                self.store.save(fresh)
            return self.store.get(project.id)
        fresh = self.store.get(project.id)
        if fresh.video_job is None or fresh.video_job.job_id != job.job_id:
            return fresh
        directory = self.store.video_dir(fresh.id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / OUTPUT_NAME
        tmp = target.with_suffix(".tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
        name = _download_name(fresh.title, ".mp4", False).removesuffix(".mp4") + ".mp4"
        fresh.video_result = VideoResult(
            job_id=job.job_id,
            file_name=name,
            size_bytes=target.stat().st_size,
            duration_seconds=int(report.get("duration_seconds") or 0),
            slide_count=int(report.get("slide_count") or 1),
            fingerprint=job.fingerprint,
            completed_at=utcnow(),
            note=report.get("note"),
        )
        fresh.video_job = fresh.video_job.model_copy(update={
            "state": VideoState.completed, "progress": 100, "stage": "완료",
            "message": None, "updated_at": utcnow(),
        })
        return self.store.save(fresh)

    def _intro_path(self, project) -> Path | None:
        from ..models.lecture_profile import VideoIntro
        from .intro_library import resolve_intro

        profile = project.lecture_profile
        if profile is None or profile.video_intro != VideoIntro.include:
            return None
        root = self.settings.data_dir if self.settings is not None else None
        if root is None:
            raise VideoGenerationFailed("인트로 폴더를 찾지 못했습니다.")
        path = resolve_intro(root, profile.video_intro_file)
        if path is None:
            raise VideoGenerationFailed(
                "인트로를 넣도록 설정했지만 선택한 영상을 찾을 수 없습니다. 옵션에서 인트로 파일을 다시 골라 주세요."
            )
        return path

    def _scripts(self, project_id: str) -> LectureScript:
        project = self.store.get(project_id)
        if project.lecture_plan is None or project.slide_specification is None:
            raise PresentationNotReady("강의 대본을 만들 슬라이드가 없습니다.")
        llm = self.llm
        if llm is None and self.settings is not None:
            llm = create_llm_client(self.settings)
        fp = script_fingerprint(
            project.lecture_plan,
            project.slide_specification,
            project.lecture_profile,
            project.source_analysis,
            llm,
        )
        path = self.store.project_dir(project_id) / SCRIPT_CACHE
        if path.is_file():
            try:
                cached = LectureScript.model_validate(json.loads(path.read_text(encoding="utf-8")))
                if cached.fingerprint == fp:
                    return cached
            except (OSError, ValueError):
                logger.warning("lecture_script.json could not be read; rebuilding")
        material = None
        raw = self.store.project_dir(project_id) / "source_material.json"
        if raw.is_file():
            try:
                material = SourceMaterial.model_validate(json.loads(raw.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                material = None
        result = LectureScriptWriter().build(
            project_id=project.id,
            plan=project.lecture_plan,
            spec=project.slide_specification,
            profile=project.lecture_profile,
            analysis=project.source_analysis,
            material=material,
            enriched=project.enriched_specification,
            llm=llm,
        )
        result.fingerprint = fp
        self.store.write_artifact(project.id, SCRIPT_CACHE, result)
        return result


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _heartbeat_stale(job_dir: Path) -> bool:
    beat = job_dir / "heartbeat.txt"
    try:
        last = beat.stat().st_mtime if beat.is_file() else 0.0
    except OSError:
        last = 0.0
    return last > 0 and (time.time() - last) > 180
