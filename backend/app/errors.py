"""User-facing application errors.

Every error is returned as `{"error": {"code": ..., "message": ..., "details": [...]}}`
with a human-readable (Korean) message. Raw stack traces are never sent to the client.
"""

from __future__ import annotations


class AppError(Exception):
    code = "AppError"
    status_code = 400
    message = "요청을 처리할 수 없습니다."

    def __init__(self, message: str | None = None, details: list[str] | None = None):
        self.message = message or self.message
        self.details = details or []
        super().__init__(self.message)


class ProjectNotFound(AppError):
    code = "ProjectNotFound"
    status_code = 404
    message = "해당 강의 프로젝트를 찾을 수 없습니다."


class UnsupportedFileType(AppError):
    code = "UnsupportedFileType"
    status_code = 415
    message = "지원하지 않는 파일 형식입니다."


class FileTooLarge(AppError):
    code = "FileTooLarge"
    status_code = 413
    message = "파일 크기가 허용 한도를 초과했습니다."


class EmptyDocument(AppError):
    code = "EmptyDocument"
    status_code = 400
    message = "비어 있는 파일입니다. 내용이 있는 강의자료를 업로드해 주세요."


class DeckNeedsPptx(AppError):
    code = "DeckNeedsPptx"
    status_code = 415
    message = "만든 PPT로 대본과 영상을 만들려면 PPTX 파일이 필요합니다."


class EmptyDeck(AppError):
    code = "EmptyDeck"
    status_code = 422
    message = "슬라이드가 없는 PPT입니다. 내용이 있는 PPTX를 올려 주세요."


class DocumentParsingError(AppError):
    code = "DocumentParsingError"
    status_code = 422
    message = "문서를 읽는 중 문제가 발생했습니다. 파일이 손상되지 않았는지 확인해 주세요."


class SourceNotUploaded(AppError):
    code = "SourceNotUploaded"
    status_code = 409
    message = "업로드된 강의자료가 없습니다. 먼저 파일을 업로드해 주세요."


class AnalysisNotReady(AppError):
    code = "AnalysisNotReady"
    status_code = 409
    message = "아직 강의자료 분석이 완료되지 않았습니다."


class InvalidLectureProfile(AppError):
    code = "InvalidLectureProfile"
    status_code = 422
    message = "강의 옵션이 올바르지 않습니다."


class InvalidRequest(AppError):
    code = "InvalidRequest"
    status_code = 422
    message = "요청 형식이 올바르지 않습니다."


class ProfileNotReady(AppError):
    code = "ProfileNotReady"
    status_code = 409
    message = "강의 옵션이 아직 저장되지 않았습니다. 먼저 강의 옵션을 저장해 주세요."


class PlanNotReady(AppError):
    code = "PlanNotReady"
    status_code = 409
    message = "아직 강의 계획이 만들어지지 않았습니다."


class LecturePlanningError(AppError):
    code = "LecturePlanningError"
    status_code = 422
    message = "강의 계획을 만드는 중 문제가 발생했습니다."


class InvalidDurationDistribution(AppError):
    code = "InvalidDurationDistribution"
    status_code = 422
    message = "강의 시간을 섹션에 올바르게 배분할 수 없습니다. 강의 시간이나 옵션을 조정해 주세요."


class SlidesNotReady(AppError):
    code = "SlidesNotReady"
    status_code = 409
    message = "아직 슬라이드 구성이 만들어지지 않았습니다. 먼저 강의 구조를 생성해 주세요."


class SlidePlanningError(AppError):
    code = "SlidePlanningError"
    status_code = 422
    message = "슬라이드 구성을 만드는 중 문제가 발생했습니다."


class EnrichmentNotAllowed(AppError):
    code = "EnrichmentNotAllowed"
    status_code = 409
    message = "강의 구조가 아직 승인되지 않았습니다. 미리보기에서 강의 구조를 승인한 뒤 콘텐츠를 보강해 주세요."


class EnrichmentNotReady(AppError):
    code = "EnrichmentNotReady"
    status_code = 409
    message = "아직 슬라이드 콘텐츠 보강이 실행되지 않았습니다."


class EnrichmentError(AppError):
    code = "EnrichmentError"
    status_code = 422
    message = "슬라이드 콘텐츠를 보강하는 중 문제가 발생했습니다. 기존 슬라이드 구성은 그대로 유지됩니다."


class PromptNotAllowed(AppError):
    code = "PromptNotAllowed"
    status_code = 409
    message = "강의 구조가 아직 승인되지 않았거나 콘텐츠 보강이 진행 중입니다. 미리보기에서 강의 구조를 승인한 뒤 다시 시도해 주세요."


class PromptNotReady(AppError):
    code = "PromptNotReady"
    status_code = 409
    message = "아직 프레젠테이션 프롬프트가 만들어지지 않았습니다."


class PromptBuildError(AppError):
    code = "PromptBuildError"
    status_code = 422
    message = "프레젠테이션 프롬프트를 만드는 중 문제가 발생했습니다. 기존 강의 구성은 그대로 유지됩니다."


# ---- STAGE 7: presentation providers -------------------------------------------------------
class PresentationNotAllowed(AppError):
    code = "PresentationNotAllowed"
    status_code = 409
    message = "프레젠테이션 프롬프트가 아직 준비되지 않았습니다. 강의 구조를 승인하고 프롬프트를 만든 뒤 다시 시도해 주세요."


class PresentationInProgress(AppError):
    code = "PresentationInProgress"
    status_code = 409
    message = "프레젠테이션을 이미 생성하고 있습니다. 완료될 때까지 기다려 주세요."


class PresentationNotReady(AppError):
    code = "PresentationNotReady"
    status_code = 409
    message = "아직 완성된 프레젠테이션이 없습니다."


class PresentationStale(AppError):
    code = "PresentationStale"
    status_code = 409
    message = "프레젠테이션을 생성하는 동안 강의 내용이 바뀌어 결과를 저장하지 않았습니다. 다시 생성해 주세요."


class PresentationProviderUnavailable(AppError):
    code = "PresentationProviderUnavailable"
    status_code = 503
    message = "프레젠테이션 생성 서비스를 사용할 수 없습니다."


class PresentationGenerationFailed(AppError):
    code = "PresentationGenerationFailed"
    status_code = 502
    message = "프레젠테이션을 생성하지 못했습니다. 강의 구성과 프롬프트는 그대로 유지됩니다."


class VideoInProgress(AppError):
    code = "VideoInProgress"
    status_code = 409
    message = "강의 영상을 이미 만들고 있습니다. 완료될 때까지 기다려 주세요."


class VideoNotReady(AppError):
    code = "VideoNotReady"
    status_code = 409
    message = "아직 완성된 강의 영상이 없습니다. PPT와 대본이 준비된 뒤 영상을 만들어 주세요."


class VideoGenerationFailed(AppError):
    code = "VideoGenerationFailed"
    status_code = 502
    message = "강의 영상을 만들지 못했습니다. PPT와 대본은 그대로 유지됩니다."
