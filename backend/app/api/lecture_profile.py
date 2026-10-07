from __future__ import annotations

from enum import Enum

from fastapi import APIRouter, File, Request, UploadFile

from ..errors import FileTooLarge, InvalidRequest, UnsupportedFileType
from ..models import lecture_profile as lp
from ..models.lecture_profile import LectureOptionsInput, LectureProfile
from ..models.project import PresentationStatus
from ..services.intro_library import list_intros, save_intro
from ..services.lecture_profile_service import build_profile, preset_schema, profile_content_changed

router = APIRouter(tags=["lecture-profile"])

# Once a plan exists, changing the options invalidates it.
_PLAN_STATES = (
    PresentationStatus.planning,
    PresentationStatus.planned,
    PresentationStatus.ready_to_generate,
    PresentationStatus.enriching,
    PresentationStatus.enriched,
    PresentationStatus.enrichment_partial,
    PresentationStatus.ready_for_presentation,
)

_ENUMS: dict[str, type[Enum]] = {
    "preset": lp.LecturePreset,
    "audience_level": lp.AudienceLevel,
    "difficulty": lp.Difficulty,
    "lecture_type": lp.LectureType,
    "explanation_depth": lp.ExplanationDepth,
    "source_policy": lp.SourcePolicy,
    "slide_density": lp.SlideDensity,
    "visual_level": lp.VisualLevel,
    "example_level": lp.ExampleLevel,
    "practice_level": lp.PracticeLevel,
    "code_level": lp.CodeLevel,
    "quiz_mode": lp.QuizMode,
    "lecture_tone": lp.LectureTone,
    "speaker_notes": lp.SpeakerNotes,
    "video_intro": lp.VideoIntro,
}


@router.get("/lecture-options")
def get_option_schema():
    """Allowed values for every option, so the UI never hard-codes them."""
    return {
        "basic": list(lp.BASIC_OPTION_FIELDS),
        "advanced": list(lp.ADVANCED_OPTION_FIELDS),
        "duration_minutes": {
            "min": lp.MIN_DURATION_MINUTES,
            "max": lp.MAX_DURATION_MINUTES,
            "recommended": [20, 40, 60, 90],
        },
        "values": {name: [m.value for m in enum] for name, enum in _ENUMS.items()},
    }


@router.get("/lecture-presets")
def get_presets():
    """Preset definitions ({name: {basic, advanced}}). `basic` values are meant to be
    copied into the form by the UI; `advanced` values are applied by the server when
    `preset` is sent."""
    return preset_schema()


@router.get("/video-intros")
def get_video_intros(request: Request):
    """Videos in the shared intros folder. The instructor picks one on the options page."""
    return {"folder": "intros", "items": list_intros(request.app.state.settings.data_dir)}


@router.post("/video-intros")
async def upload_video_intro(request: Request, file: UploadFile = File(...)):
    settings = request.app.state.settings
    data = await file.read()
    if not data:
        raise InvalidRequest("비어 있는 인트로 영상입니다.")
    if len(data) > settings.max_upload_bytes:
        raise FileTooLarge(f"파일 크기가 {settings.max_upload_mb}MB 를 초과했습니다.")
    try:
        name = save_intro(settings.data_dir, file.filename or "intro.mp4", data)
    except ValueError as exc:
        raise UnsupportedFileType(str(exc)) from exc
    return {"filename": name, "folder": "intros", "items": list_intros(settings.data_dir)}


@router.put("/projects/{project_id}/profile", response_model=LectureProfile)
def put_profile(project_id: str, options: LectureOptionsInput, request: Request):
    store = request.app.state.store
    project = store.get(project_id)
    existing = project.lecture_profile
    profile = build_profile(project_id, options, existing=existing)
    project.lecture_profile = profile
    if existing is not None and not profile_content_changed(existing, profile):
        project.discard_video()
        store.save(project)
        return profile
    if project.input_mode == "deck" and project.source_analysis is not None:
        from ..services.deck_importer import apply_imported_deck
        from ..services.lecture_script_writer import clear_script_cache

        clear_script_cache(store, project.id)
        apply_imported_deck(store, project)
        return profile
    # Derived artifacts from an older profile are stale.
    project.lecture_plan = None
    project.slide_specification = None
    project.enriched_specification = None
    project.discard_prompt()  # prompt, presentation job and file (a finished project falls back to ready_for_presentation)
    if project.presentation_status in _PLAN_STATES:
        project.presentation_status = PresentationStatus.analyzed  # must plan again
    store.save(project)
    return profile


@router.get("/projects/{project_id}/profile", response_model=LectureProfile)
def get_profile(project_id: str, request: Request):
    project = request.app.state.store.get(project_id)
    if project.lecture_profile is None:
        raise InvalidRequest("아직 저장된 강의 옵션이 없습니다.")
    return project.lecture_profile
