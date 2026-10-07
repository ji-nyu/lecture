from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import analysis, deck, enrichment, lecture_plan, lecture_profile, presentation, projects, prompt, scripts, slides, upload, video
from .config import Settings, load_settings
from .errors import AppError
from .llm.base import ContentLLMClient
from .llm.factory import create_content_client
from .providers.base import PresentationProvider
from .providers.factory import create_provider
from .services.lecture_video import VideoPipeline
from .services.llm_client import LLMClient, create_llm_client
from .storage.project_store import ProjectStore

logger = logging.getLogger("ailecturegen")


def _error(status: int, code: str, message: str, details: list[str] | None = None):
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or []}},
    )


def _describe_validation_error(err: dict) -> str:
    loc = [str(x) for x in err.get("loc", []) if x not in ("body", "query", "path")]
    field = ".".join(loc)
    kind = err.get("type", "")
    if kind == "missing":
        msg = "필수 항목입니다."
    elif kind.startswith("enum") or kind == "literal_error":
        msg = "허용되지 않는 값입니다."
    elif kind == "extra_forbidden":
        msg = "알 수 없는 항목입니다."
    elif "greater" in kind or "less" in kind:
        msg = "허용 범위를 벗어났습니다."
    else:
        msg = "값의 형식이 올바르지 않습니다."
    return f"{field}: {msg}" if field else msg


def create_app(
    settings: Settings | None = None,
    llm_client: LLMClient | None = None,
    content_client: ContentLLMClient | None = None,
    presentation_provider: PresentationProvider | None = None,
    video_pipeline: VideoPipeline | None = None,
    video_launcher=None,
) -> FastAPI:
    """`llm_client` lets tests inject a fake; otherwise it is built from LLM_* settings
    when a key and model are configured. Scripts use that same client.
    `content_client` does the same for the STAGE 6A content LLM; otherwise it is built
    from ENRICHMENT_PROVIDER (default `none` = no LLM, slides keep their rule-based content).
    `presentation_provider` (STAGE 7) is the PPT generator; default from GENSPARK_MODE (`mock`)."""
    settings = settings or load_settings()
    (Path(settings.data_dir) / "intros").mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="AILectureGen API", version="0.1.0")
    app.state.settings = settings
    app.state.llm_client = llm_client if llm_client is not None else create_llm_client(settings)
    app.state.content_client = content_client if content_client is not None else create_content_client(settings)
    app.state.store = ProjectStore(Path(settings.data_dir))
    app.state.presentation_provider = (
        presentation_provider if presentation_provider is not None else create_provider(settings)
    )
    app.state.video_pipeline = video_pipeline
    app.state.video_launcher = video_launcher

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return _error(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = [_describe_validation_error(e) for e in exc.errors()]
        if request.url.path.endswith("/profile"):
            code, msg = "InvalidLectureProfile", "강의 옵션이 올바르지 않습니다."
        else:
            code, msg = "InvalidRequest", "요청 형식이 올바르지 않습니다."
        return _error(422, code, msg, details)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        # Log the trace on the server only; never expose it to the client.
        logger.exception("Unhandled error", exc_info=exc)
        return _error(
            500,
            "InternalError",
            "서버 내부 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        )

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok"}

    app.include_router(projects.router)
    app.include_router(upload.router)
    app.include_router(deck.router)
    app.include_router(analysis.router)
    app.include_router(lecture_profile.router)
    app.include_router(lecture_plan.router)
    app.include_router(slides.router)
    app.include_router(scripts.router)
    app.include_router(enrichment.router)
    app.include_router(prompt.router)
    app.include_router(presentation.router)
    app.include_router(video.router)
    return app


app = create_app()
