"""JSON schema of the content LLM's answer (provider structured output).

Written to satisfy OpenAI's `strict` structured outputs: every object lists all of its
properties as required and forbids others, optional values are nullable. The server does
not rely on the provider honouring it: the answer is validated again (LLMSlideOutput) and
by the GroundingValidator. `FieldProvenance` values and field names come from the models,
so the schema cannot drift away from them.
"""

from __future__ import annotations

from typing import Any

from ..models.enriched_slide_spec import CONTENT_FIELDS, FieldProvenance

_NULL_STR: dict[str, Any] = {"type": ["string", "null"]}
_STR_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}


def _object(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


_PRACTICE = _object(
    {"goal": _NULL_STR, "prerequisites": _STR_LIST, "steps": _STR_LIST, "expected_result": _NULL_STR, "cautions": _STR_LIST}
)
_CODE = _object({"purpose": _NULL_STR, "key_lines": _STR_LIST, "execution_flow": _STR_LIST, "cautions": _STR_LIST})
_QUIZ = _object({"question": _NULL_STR, "choices": _STR_LIST, "answer": _NULL_STR, "explanation": _NULL_STR})
_PROVENANCE = _object(
    {f: {"type": ["string", "null"], "enum": [*(p.value for p in FieldProvenance), None]} for f in CONTENT_FIELDS}
)


def slide_schema() -> dict[str, Any]:
    props: dict[str, Any] = {
        "slide_number": {"type": "integer"},
        "display_title": _NULL_STR,
        "key_message": _NULL_STR,
        "body_points": _STR_LIST,
        "explanation": _NULL_STR,
        "example": _NULL_STR,
        "analogy": _NULL_STR,
        "practice_instruction": _nullable(_PRACTICE),
        "code_explanation": _nullable(_CODE),
        "visual_instruction": _NULL_STR,
        "presenter_notes": _NULL_STR,
        "quiz_content": _nullable(_QUIZ),
        "summary_message": _NULL_STR,
        "provenance": _PROVENANCE,
    }
    return _object(props)


def batch_schema() -> dict[str, Any]:
    return _object({"slides": {"type": "array", "items": slide_schema()}})
