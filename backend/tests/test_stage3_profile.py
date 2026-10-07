"""STAGE 3: LectureProfileService (presets, defaults, conflict checks, warnings)."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import InvalidLectureProfile
from app.main import create_app
from app.models.lecture_profile import LectureOptionsInput, LecturePreset, LectureProfile
from app.services.lecture_profile_service import PRESETS, build_profile, preset_schema
from app.services.planning_policy import derive_policy

from plan_helpers import BASE, CASE_A, CASE_B, CASE_C, profile


def build(**opts):
    return build_profile("p" * 32, LectureOptionsInput(**{**BASE, **opts}))


# ------------------------------------------------------------------ presets
def test_the_four_spec_presets_exist_with_the_spec_values():
    assert {p.value for p in PRESETS} == {
        "concept_focused", "practice_focused", "balanced", "exam_preparation"}
    cf = build(preset="concept_focused", lecture_type="theory", explanation_depth="detailed")
    assert (cf.example_level.value, cf.practice_level.value, cf.visual_level.value) == ("medium", "none", "medium")
    pf = build(preset="practice_focused", lecture_type="practice")
    assert (pf.practice_level.value, pf.code_level.value, pf.example_level.value) == ("full", "executable", "medium")
    bal = build(preset="balanced", lecture_type="mixed")
    assert (bal.practice_level.value, bal.example_level.value) == ("guided", "medium")
    ep = build(preset="exam_preparation", lecture_type="exam_preparation", explanation_depth="concise")
    assert (ep.quiz_mode.value, ep.example_level.value) == ("both", "high")


def test_precedence_is_explicit_then_preset_then_inferred():
    p = build(preset="practice_focused", lecture_type="practice", practice_level="guided")
    assert p.practice_level.value == "guided"  # explicit beats the preset (full)
    assert "practice_level" not in p.preset_fields and "practice_level" not in p.inferred_fields
    assert p.code_level.value == "executable" and "code_level" in p.preset_fields  # from the preset
    assert "code_level" not in p.inferred_fields
    assert "quiz_mode" in p.inferred_fields  # the preset says nothing about it
    assert p.preset == LecturePreset.practice_focused


def test_no_preset_keeps_the_stage1_inference_unchanged():
    p = build()
    assert p.preset is None and p.preset_fields == []
    assert set(p.inferred_fields) == {
        "slide_density", "visual_level", "example_level", "practice_level",
        "code_level", "quiz_mode", "lecture_tone", "speaker_notes", "video_intro"}


def test_preset_that_differs_from_basic_options_warns():
    p = build(preset="concept_focused", lecture_type="mixed", explanation_depth="standard")
    joined = " ".join(p.warnings)
    assert "lecture_type" in joined and "explanation_depth" in joined
    assert build(preset="balanced", lecture_type="mixed").warnings == []


def test_preset_values_that_contradict_the_lecture_type_are_rejected():
    # concept_focused sets practice_level=none, which a practice lecture cannot have
    with pytest.raises(InvalidLectureProfile) as e:
        build(preset="concept_focused", lecture_type="practice")
    assert "실습" in e.value.message


def test_preset_schema_for_the_ui():
    s = preset_schema()
    assert set(s) == {p.value for p in PRESETS}
    assert s["practice_focused"]["basic"] == {"lecture_type": "practice", "explanation_depth": "standard"}
    assert s["practice_focused"]["advanced"]["practice_level"] == "full"


# ------------------------------------------------------------- soft warnings
def test_soft_warnings_do_not_block_the_profile():
    assert any("20분" in w for w in build(duration_minutes=20, lecture_type="practice").warnings)
    assert any("20분" in w for w in build(duration_minutes=15, explanation_depth="detailed").warnings)
    assert any("입문" in w for w in build(audience_level="professional", difficulty="introductory").warnings)
    assert any("고급" in w for w in build(audience_level="general", difficulty="advanced").warnings)
    assert any("예제" in w for w in build(lecture_type="example_based", example_level="none").warnings)
    assert build(**CASE_A).warnings == [] and build(**CASE_B).warnings == []


def test_hard_conflicts_are_still_rejected():
    with pytest.raises(InvalidLectureProfile):
        build(lecture_type="practice", practice_level="none")
    with pytest.raises(InvalidLectureProfile):
        build(lecture_type="exam_preparation", quiz_mode="none")
    with pytest.raises(InvalidLectureProfile):
        build(code_level="executable", practice_level="none")


def test_profile_stored_before_stage3_still_loads():
    old = build().model_dump(mode="json")
    for k in ("preset", "preset_fields", "warnings"):
        old.pop(k)
    p = LectureProfile.model_validate(old)
    assert p.preset is None and p.warnings == []


# --------------------------------------------------------------- the policy
def test_spec_default_example_still_holds():
    """intermediate / 60 / intermediate / practice / detailed -> full, medium, medium, full"""
    p = build(lecture_type="practice", explanation_depth="detailed")
    assert (p.practice_level.value, p.example_level.value, p.visual_level.value, p.speaker_notes.value) == (
        "full", "medium", "medium", "full")


def test_policy_reflects_the_educational_rules():
    a, b, c = derive_policy(profile(**CASE_A)), derive_policy(profile(**CASE_B)), derive_policy(profile(**CASE_C))
    # Rule_Audience_01: beginners get every term defined, a prerequisite recap, examples first
    assert a.definition_ratio == 1.0 and a.prerequisite_section and a.example_first
    # Rule_Audience_02: professionals: basics reduced, industry cases, no prerequisite recap
    assert c.definition_ratio == 0.0 and c.industry_examples and not c.prerequisite_section
    # Rule_LectureType_01/02: theory has no practice, practice has blocks + steps
    assert a.practice_share == 0 and a.practice_blocks == 0
    assert b.practice_share > 0.4 and b.practice_blocks == 3 and b.steps_per_activity == 6
    # Rule_Difficulty_02: advanced -> trade-off section
    assert c.caution_section and not a.caution_section and not b.caution_section
    # depth: detailed spends more time and slides per concept than concise
    assert a.concept_seconds > b.concept_seconds > c.concept_seconds
    assert a.slides_per_concept > c.slides_per_concept
    exam = derive_policy(profile(lecture_type="exam_preparation"))
    assert exam.comparison_section and exam.caution_section and exam.checkpoint_quiz and exam.final_quiz


# --------------------------------------------------------------------- API
@pytest.fixture()
def client(tmp_path):
    s = Settings(data_dir=tmp_path, max_upload_mb=5, cors_origins=())
    return TestClient(create_app(s), raise_server_exceptions=False)


def test_presets_endpoint_and_option_schema(client):
    r = client.get("/lecture-presets")
    assert r.status_code == 200 and "balanced" in r.json()
    schema = client.get("/lecture-options").json()
    assert schema["values"]["preset"] == [p.value for p in LecturePreset]
    assert len(schema["basic"]) == 6  # presets are not a 7th basic option


def test_put_profile_with_preset_over_the_api(client):
    pid = client.post("/projects", json={}).json()["id"]
    r = client.put(f"/projects/{pid}/profile", json={**BASE, "lecture_type": "practice", "preset": "practice_focused"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["preset"] == "practice_focused" and body["practice_level"] == "full"
    assert "code_level" in body["preset_fields"] and isinstance(body["warnings"], list)
    bad = client.put(f"/projects/{pid}/profile", json={**BASE, "preset": "nope"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "InvalidLectureProfile"
