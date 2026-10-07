"""PresentationService (STAGE 7): runs the approved prompt through a PresentationProvider.

    POST   -> start()    prompt -> provider job, status `generating`
    GET    -> status()   polls the provider; on completion fetches the file, stores it with the
                         project and moves the project to `completed` (or `failed`)
    result() / download_path()   read the stored result

The service only knows the `PresentationProvider` interface (`providers.base`); it has no idea
whether a mock or a real system is behind it. It never changes the lecture (plan, slides,
enrichment, prompt): a provider outage or failure leaves all of them as they were, and the project
stays retryable. Provider messages shown to the user are the provider's own user-facing texts; raw
exceptions are logged on the server only.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from ..errors import (
    AppError,
    PresentationGenerationFailed,
    PresentationInProgress,
    PresentationNotAllowed,
    PresentationNotReady,
    PresentationProviderUnavailable,
    PresentationStale,
    PromptNotReady,
)
from ..models.presentation import (
    JobState,
    PresentationJob,
    PresentationRequest,
    PresentationResult,
    PresentationStatusResponse,
    ProviderStatus,
    SlideBrief,
)
from ..models.project import PresentationStatus, Project
from ..providers.base import PresentationProvider
from ..storage.project_store import ProjectStore, utcnow

logger = logging.getLogger("ailecturegen")

# What may be stored and offered for download. STAGE 8 extends this once the real output is known.
ALLOWED_EXTENSIONS = (".pptx", ".pdf")
MOCK_RESULT_NOTE = "Mock 공급자가 만든 자리표시 파일입니다. 실제로 디자인된 프레젠테이션이 아닙니다."
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _download_name(title: str | None, extension: str, is_mock: bool) -> str:
    base = _UNSAFE_NAME.sub("_", title or "").strip(" ._")[:80] or "presentation"
    return f"{base}{'_MOCK' if is_mock else ''}{extension}"


class PresentationService:
    def __init__(self, provider: PresentationProvider, store: ProjectStore):
        self.provider = provider
        self.store = store

    # ------------------------------------------------------------------ start
    def start(self, project_id: str) -> PresentationStatusResponse:
        project = self.store.get(project_id)
        generating = project.presentation_status == PresentationStatus.generating
        job = project.presentation_job
        if generating and job is not None and job.state in (JobState.queued, JobState.running):
            raise PresentationInProgress()
        # `generating` without a running job: an earlier start was interrupted (e.g. server stopped).
        interrupted = generating
        prompt = project.presentation_prompt
        if prompt is None or project.final_prompt is None:
            raise PromptNotReady()
        if not (
            project.presentation_status == PresentationStatus.ready_for_presentation
            or project.presentation_finished
            or interrupted
        ) or not prompt.validation.passed:
            raise PresentationNotAllowed()

        availability = self.provider.availability()
        if not availability.configured:  # nothing is touched: the project stays as it is
            raise PresentationProviderUnavailable(availability.message)

        request = PresentationRequest(
            project_id=project.id,
            title=prompt.title or project.title or "강의",
            final_prompt=project.final_prompt,
            prompt_hash=prompt.prompt_hash,
            slide_count=prompt.slide_count,
            slides=[
                SlideBrief(slide_number=s.slide_number, title=s.title, slide_type=s.slide_type)
                for s in prompt.slides
            ],
            duration_minutes=project.lecture_profile.duration_minutes if project.lecture_profile else None,
        )

        previous_status = (
            PresentationStatus.ready_for_presentation if interrupted else project.presentation_status
        )
        project.presentation_status = PresentationStatus.generating
        project.error_message = None
        self.store.save(project)
        try:
            status = self.provider.create_presentation(request)
        except Exception as exc:
            message = exc.message if isinstance(exc, AppError) else PresentationGenerationFailed.message
            fresh = self.store.get(project_id)
            if fresh.presentation_status == PresentationStatus.generating:  # give the state back
                fresh.presentation_status = previous_status
                fresh.error_message = message
                self.store.save(fresh)
            if isinstance(exc, AppError):
                raise
            logger.exception("Presentation provider failed to start", exc_info=exc)  # server log only
            raise PresentationGenerationFailed() from exc

        fresh = self.store.get(project_id)
        if fresh.presentation_prompt is None or fresh.presentation_prompt.prompt_hash != request.prompt_hash:
            raise PresentationStale()  # the lecture changed meanwhile; the invalidation already reset the state
        now = utcnow()
        fresh.presentation_provider = self.provider.name
        fresh.presentation_job = PresentationJob(
            provider=self.provider.name, is_mock=self.provider.is_mock, job_id=status.job_id,
            state=JobState.queued, prompt_hash=request.prompt_hash, created_at=now, updated_at=now,
        )
        fresh.presentation_result = None  # a new run replaces the earlier file (removed on save)
        fresh.discard_video()
        fresh.presentation_status = PresentationStatus.generating
        self.store.save(fresh)
        return self.status(project_id, _known=status)

    # ----------------------------------------------------------------- status
    def status(self, project_id: str, _known: ProviderStatus | None = None) -> PresentationStatusResponse:
        project = self.store.get(project_id)
        job = project.presentation_job
        if job is not None and job.state in (JobState.queued, JobState.running):
            project = self._advance(project, job, _known)
        return self._response(project)

    def _advance(self, project: Project, job: PresentationJob, known: ProviderStatus | None) -> Project:
        """Ask the provider once and record the answer. Returns the up-to-date project."""
        if job.provider != self.provider.name:
            return self._fail(
                project, job,
                f"이 작업은 다른 공급자({job.provider})가 시작했습니다. 현재 설정과 달라 이어갈 수 없으니 다시 생성해 주세요.",
            )
        try:
            status = known or self.provider.get_status(job.job_id)
        except PresentationProviderUnavailable:
            raise  # a temporary problem: keep the job, the caller can poll again
        except AppError as exc:
            return self._fail(project, job, exc.message)
        except Exception as exc:
            logger.exception("Presentation status check failed", exc_info=exc)  # server log only
            return self._fail(project, job, PresentationGenerationFailed.message)

        if status.state == JobState.not_configured:
            raise PresentationProviderUnavailable(status.message)
        if status.state == JobState.failed:
            return self._fail(project, job, status.message or PresentationGenerationFailed.message)
        if status.state == JobState.completed:
            return self._finish(project, job, status)
        return self._record(project, job, status)

    def _current(self, project: Project, job: PresentationJob) -> Project | None:
        """The stored project, unless a newer run or a lecture change replaced this job meanwhile."""
        fresh = self.store.get(project.id)
        if fresh.presentation_job is None or fresh.presentation_job.job_id != job.job_id:
            return None
        return fresh

    def _record(self, project: Project, job: PresentationJob, status: ProviderStatus) -> Project:
        fresh = self._current(project, job)
        if fresh is None:
            return self.store.get(project.id)
        fresh.presentation_job = fresh.presentation_job.model_copy(update=dict(
            state=status.state, progress=status.progress, stage=status.stage,
            message=status.message, updated_at=utcnow(),
        ))
        self.store.save(fresh)
        return fresh

    def _fail(self, project: Project, job: PresentationJob, message: str) -> Project:
        fresh = self._current(project, job)
        if fresh is None:
            return self.store.get(project.id)
        fresh.presentation_job = fresh.presentation_job.model_copy(update=dict(
            state=JobState.failed, message=message, updated_at=utcnow(),
        ))
        fresh.presentation_result = None
        fresh.presentation_status = PresentationStatus.failed
        fresh.error_message = message
        self.store.save(fresh)
        return fresh

    def _finish(self, project: Project, job: PresentationJob, status: ProviderStatus) -> Project:
        try:
            info = self.provider.get_result(job.job_id)
            downloaded = self.provider.download(job.job_id)
        except PresentationProviderUnavailable:
            raise
        except AppError as exc:
            return self._fail(project, job, exc.message)
        except Exception as exc:
            logger.exception("Presentation download failed", exc_info=exc)  # server log only
            return self._fail(project, job, PresentationGenerationFailed.message)

        extension = Path(downloaded.file_name).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            return self._fail(project, job, "프레젠테이션 공급자가 지원하지 않는 형식의 파일을 돌려주었습니다.")
        if not downloaded.data:
            return self._fail(project, job, "프레젠테이션 공급자가 빈 파일을 돌려주었습니다.")

        fresh = self._current(project, job)
        if fresh is None:
            return self.store.get(project.id)
        prompt = fresh.presentation_prompt
        expected = prompt.slide_count if prompt else None
        note = MOCK_RESULT_NOTE if job.is_mock else None
        if info.slide_count is not None and expected is not None and info.slide_count != expected:
            warn = f"공급자가 알려준 슬라이드 수({info.slide_count})가 계획({expected})과 다릅니다."
            note = f"{note} {warn}" if note else warn

        directory = self.store.presentation_dir(fresh.id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"presentation{extension}"
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(downloaded.data)
        os.replace(tmp, target)

        fresh.presentation_result = PresentationResult(
            provider=job.provider, is_mock=job.is_mock, job_id=job.job_id, prompt_hash=job.prompt_hash,
            file_name=_download_name(prompt.title if prompt else fresh.title, extension, job.is_mock),
            content_type=downloaded.content_type,
            size_bytes=len(downloaded.data),
            sha256=hashlib.sha256(downloaded.data).hexdigest(),
            slide_count=info.slide_count,
            completed_at=utcnow(),
            note=note,
        )
        fresh.presentation_job = fresh.presentation_job.model_copy(update=dict(
            state=JobState.completed, progress=100, stage=status.stage, message=None, updated_at=utcnow(),
        ))
        fresh.presentation_status = PresentationStatus.completed
        fresh.error_message = None
        self.store.save(fresh)
        return fresh

    # ------------------------------------------------------------------ reads
    def result(self, project_id: str) -> PresentationResult:
        project = self.store.get(project_id)
        if project.presentation_result is None:
            raise PresentationNotReady()
        return project.presentation_result

    def download_path(self, project_id: str) -> tuple[Path, PresentationResult]:
        result = self.result(project_id)
        path = self.store.presentation_dir(project_id) / f"presentation{Path(result.file_name).suffix.lower()}"
        if not path.is_file():
            raise PresentationNotReady("프레젠테이션 파일을 찾을 수 없습니다. 다시 생성해 주세요.")
        return path, result

    def _response(self, project: Project) -> PresentationStatusResponse:
        job = project.presentation_job
        availability = self.provider.availability()
        return PresentationStatusResponse(
            state=job.state.value if job else "not_started",
            project_status=project.presentation_status.value if project.presentation_status else None,
            provider=job.provider if job else self.provider.name,
            is_mock=job.is_mock if job else self.provider.is_mock,
            provider_configured=availability.configured,
            provider_message=availability.message,
            job_id=job.job_id if job else None,
            progress=job.progress if job else 0,
            stage=job.stage if job else None,
            message=job.message if job else None,
            has_result=project.presentation_result is not None,
            prompt_hash=job.prompt_hash if job else None,
            created_at=job.created_at if job else None,
            updated_at=job.updated_at if job else None,
        )
