"""Tests for the structured-output schema a workflow child may declare.

The schema support mirrors the subset the DeepSeek Harness accepts: an
object root, and the keywords ``type``, ``properties``, ``required``,
``additionalProperties``, ``items``, ``enum``, ``const``, and ``oneOf``.
These tests pin both the accepted shapes and the rejected ones, because a
rejected schema must fail before any child runs.
"""

from __future__ import annotations

import pytest

from app.deepspace.services.workflow_schema import (
    WorkflowSchemaError,
    parse_structured_answer,
    structured_output_instruction,
    validate_workflow_schema,
)

pytestmark = pytest.mark.unit_no_db


def _object_schema() -> dict[str, object]:
    """The schema used by most tests: one scored verdict object."""
    return {
        "type": "object",
        "properties": {
            "score": {"type": "integer"},
            "verdict": {"type": "string"},
        },
        "required": ["score", "verdict"],
        "additionalProperties": False,
    }


def test_object_rooted_schema_is_accepted() -> None:
    """A schema inside the supported subset passes unchanged."""
    schema = _object_schema()
    assert validate_workflow_schema(schema) is schema


def test_nested_object_array_and_one_of_are_accepted() -> None:
    """Nested objects, arrays, and oneOf alternatives stay inside the subset."""
    schema = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "severity": {"type": "string", "enum": ["low", "high"]},
                    },
                    "required": ["file"],
                },
            },
            "outcome": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "null"},
                ]
            },
        },
        "required": ["findings"],
    }
    assert validate_workflow_schema(schema) is schema


def test_annotation_keywords_are_accepted() -> None:
    """A schema may carry documentation keywords that change no value."""
    schema = {
        "type": "object",
        "title": "Verdict",
        "description": "The reviewer's verdict.",
        "properties": {
            "score": {"type": "integer", "description": "Zero to one hundred."},
        },
    }
    assert validate_workflow_schema(schema) is schema


def test_unsupported_keyword_is_rejected() -> None:
    """A keyword outside the subset is refused before a child runs."""
    with pytest.raises(WorkflowSchemaError, match="minimum"):
        validate_workflow_schema(
            {
                "type": "object",
                "properties": {"score": {"type": "integer", "minimum": 0}},
            }
        )


def test_non_object_root_is_rejected() -> None:
    """Structured output is object-rooted, so an array root is refused."""
    with pytest.raises(WorkflowSchemaError, match="object"):
        validate_workflow_schema({"type": "array", "items": {"type": "string"}})


def test_type_and_one_of_together_are_rejected() -> None:
    """A node may declare a type or oneOf, never both."""
    with pytest.raises(WorkflowSchemaError, match="both type and oneOf"):
        validate_workflow_schema(
            {
                "type": "object",
                "oneOf": [{"type": "object"}, {"type": "object"}],
            }
        )


def test_unknown_required_property_is_rejected() -> None:
    """A required name that no property declares cannot be satisfied."""
    with pytest.raises(WorkflowSchemaError, match="unknown property"):
        validate_workflow_schema(
            {
                "type": "object",
                "properties": {"score": {"type": "integer"}},
                "required": ["verdict"],
            }
        )


def test_keyword_on_the_wrong_type_is_rejected() -> None:
    """``items`` belongs to arrays and not to scalars."""
    with pytest.raises(WorkflowSchemaError, match="items"):
        validate_workflow_schema(
            {
                "type": "object",
                "properties": {"score": {"type": "integer", "items": {"type": "string"}}},
            }
        )


def test_enum_values_must_match_the_declared_type() -> None:
    """An enum that holds another type cannot be validated."""
    with pytest.raises(WorkflowSchemaError, match="enum"):
        validate_workflow_schema(
            {
                "type": "object",
                "properties": {"verdict": {"type": "string", "enum": [1, 2]}},
            }
        )


def test_circular_schema_is_rejected() -> None:
    """A schema that contains itself cannot be checked and is refused."""
    schema: dict[str, object] = {"type": "object", "properties": {}}
    properties = schema["properties"]
    assert isinstance(properties, dict)
    properties["self"] = schema
    with pytest.raises(WorkflowSchemaError, match="circular"):
        validate_workflow_schema(schema)


def test_structured_output_instruction_carries_the_schema() -> None:
    """The instruction tells the child to answer with one JSON object."""
    schema = _object_schema()
    instruction = structured_output_instruction(schema)
    assert "JSON Schema" in instruction
    assert '"verdict"' in instruction
    assert "Do not add a preamble" in instruction


def test_plain_json_answer_is_validated() -> None:
    """A bare JSON object that satisfies the schema is returned as the value."""
    value, failure = parse_structured_answer('{"score": 42, "verdict": "ok"}', _object_schema())
    assert failure is None
    assert value == {"score": 42, "verdict": "ok"}


def test_fenced_json_answer_is_validated() -> None:
    """A fenced code block still yields the object it holds."""
    answer = '```json\n{"score": 7, "verdict": "ok"}\n```'
    value, failure = parse_structured_answer(answer, _object_schema())
    assert failure is None
    assert value == {"score": 7, "verdict": "ok"}


def test_json_embedded_in_prose_is_validated() -> None:
    """An object surrounded by prose still yields the object."""
    answer = 'Here is the result you asked for: {"score": 3, "verdict": "ok"} — done.'
    value, failure = parse_structured_answer(answer, _object_schema())
    assert failure is None
    assert value == {"score": 3, "verdict": "ok"}


def test_answer_that_does_not_match_the_schema_fails_with_a_reason() -> None:
    """A schema mismatch is reported instead of yielding a wrong value."""
    value, failure = parse_structured_answer('{"score": "high", "verdict": "ok"}', _object_schema())
    assert value is None
    assert failure is not None
    assert "score" in failure


def test_answer_without_json_fails_with_a_reason() -> None:
    """An answer with no JSON object at all is reported as such."""
    value, failure = parse_structured_answer("I could not decide.", _object_schema())
    assert value is None
    assert failure == "The answer did not contain a JSON object."
