"""Structured-output schema support for DeepSpace workflow child agents.

A workflow child may be asked for one JSON object that matches a schema the
script declares.  The schema is checked against the supported subset before
any child runs, so a script that asks for an unsupported shape fails with a
readable message instead of producing an unpredictable answer.

The supported subset is deliberately small: ``type``, ``properties``,
``required``, ``additionalProperties``, ``items``, ``enum``, ``const``, and
``oneOf``, plus the annotation keywords that only carry documentation.  The
root must be an object, because a structured child answer is one object the
script reads as a whole.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The JSON Schema keywords a workflow child schema may declare.
SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "oneOf",
    }
)

#: The JSON Schema keywords that only carry documentation.
ANNOTATION_KEYWORDS = frozenset({"description", "title", "$comment", "default", "examples"})

#: The JSON Schema types the subset understands.
SCHEMA_TYPES = ("string", "number", "integer", "boolean", "null", "object", "array")

#: The types a ``enum`` or a ``const`` may hold.
SCALAR_TYPES = ("string", "number", "integer", "boolean", "null")

#: The schema types each keyword may sit beside.
_KEYWORDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "properties": ("object",),
    "required": ("object",),
    "additionalProperties": ("object",),
    "items": ("array",),
    "enum": ("string", "number", "integer", "boolean", "null"),
    "const": ("string", "number", "integer", "boolean", "null"),
}

_FENCED_JSON = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


class WorkflowSchemaError(ValueError):
    """Raised when a workflow child schema is outside the supported subset."""


def validate_workflow_schema(schema: Any) -> dict[str, Any]:
    """Check one schema against the supported subset.

    Args:
        schema: The untrusted schema the script declared.

    Returns:
        The schema, unchanged.

    Raises:
        WorkflowSchemaError: When the schema is outside the supported subset.
    """
    violations: list[str] = []
    _check_node(schema, "schema", violations, set())
    if not violations and not _is_object_rooted(schema):
        violations.append('schema.type must be "object" (structured output is object-rooted)')
    if violations:
        raise WorkflowSchemaError("; ".join(violations))
    if not isinstance(schema, dict):
        raise WorkflowSchemaError("schema must be a schema object")
    return schema


def structured_output_instruction(schema: dict[str, Any]) -> str:
    """Build the instruction that asks a child for one schema-shaped object.

    Args:
        schema: The validated schema the child must satisfy.

    Returns:
        The instruction appended to the child's task.
    """
    rendered = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "Return only one JSON object that matches the JSON Schema below. "
        "Do not add a preamble, an explanation, or a code fence around it; "
        "the object must be the whole answer.\n"
        f"JSON Schema:\n{rendered}"
    )


def parse_structured_answer(text: str, schema: dict[str, Any]) -> tuple[Any | None, str | None]:
    """Validate one child answer against the schema it was asked for.

    The answer is read as JSON, so a child that wrapped its object in a code
    fence or surrounded it with prose still yields the object.  A candidate
    that parses but does not satisfy the schema does not end the search; the
    next candidate is tried, because a child may put its answer after an
    example object.

    Args:
        text: The child's answer.
        schema: The validated schema the child was asked for.

    Returns:
        The validated value and None, or None and the reason it failed.
    """
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    failure: str | None = None
    for candidate in _json_candidates(text):
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if not errors:
            return value, None
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path)
        failure = f"{first.message} (at {location})" if location else first.message
    return None, failure or "The answer did not contain a JSON object."


def _json_candidates(text: str) -> list[str]:
    """List the JSON payloads one answer may carry, most likely first."""
    stripped = str(text or "").strip()
    candidates: list[str] = []
    fenced = _FENCED_JSON.search(stripped)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(stripped)
    balanced = _first_balanced_object(stripped)
    if balanced and balanced not in candidates:
        candidates.append(balanced)
    return candidates


def _first_balanced_object(text: str) -> str | None:
    """Read the first balanced ``{...}`` span in one answer."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _is_object_rooted(schema: Any) -> bool:
    """Report whether one schema declares an object root."""
    return isinstance(schema, dict) and schema.get("type") == "object"


def _check_node(node: Any, path: str, violations: list[str], stack: set[int]) -> None:
    """Check one schema node and the nodes it holds."""
    if not isinstance(node, dict):
        violations.append(f"{path} must be a schema object")
        return
    identity = id(node)
    if identity in stack:
        violations.append(f"{path} is circular")
        return
    stack.add(identity)
    try:
        for key in node:
            if key in SUPPORTED_KEYWORDS or key in ANNOTATION_KEYWORDS:
                continue
            violations.append(
                f"{path}.{key} is not a supported keyword "
                "(subset: type/properties/required/additionalProperties/items/enum/const/oneOf)"
            )
        if "description" in node and not isinstance(node["description"], str):
            violations.append(f"{path}.description must be a string")
        if "title" in node and not isinstance(node["title"], str):
            violations.append(f"{path}.title must be a string")

        has_type = "type" in node
        has_one_of = "oneOf" in node
        if has_type and has_one_of:
            violations.append(f"{path} cannot declare both type and oneOf")
            return
        if not has_type and not has_one_of:
            return

        if has_one_of:
            alternatives = node["oneOf"]
            if not isinstance(alternatives, list) or len(alternatives) < 2:
                violations.append(f"{path}.oneOf must be an array of at least two schemas")
                return
            for index, alternative in enumerate(alternatives):
                _check_node(alternative, f"{path}.oneOf[{index}]", violations, stack)
            return

        schema_type = node["type"]
        if not isinstance(schema_type, str) or schema_type not in SCHEMA_TYPES:
            violations.append(f"{path}.type must be one of {'/'.join(SCHEMA_TYPES)}")
            return

        for keyword, allowed_types in _KEYWORDS_BY_TYPE.items():
            if keyword in node and schema_type not in allowed_types:
                violations.append(f'{path}.{keyword} is not supported on type "{schema_type}"')

        if schema_type == "object":
            _check_object_node(node, path, violations, stack)
        elif schema_type == "array":
            if "items" in node:
                _check_node(node["items"], f"{path}.items", violations, stack)
        else:
            _check_scalar_node(node, path, schema_type, violations)
    finally:
        stack.discard(identity)


def _check_object_node(
    node: dict[str, Any], path: str, violations: list[str], stack: set[int]
) -> None:
    """Check the keywords an object node may carry."""
    properties = node.get("properties")
    known: set[str] = set()
    if "properties" in node:
        if not isinstance(properties, dict):
            violations.append(f"{path}.properties must be an object of schemas")
        else:
            known = {str(key) for key in properties}
            for key, value in properties.items():
                _check_node(value, f"{path}.properties.{key}", violations, stack)
    if "required" in node:
        required = node["required"]
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            violations.append(f"{path}.required must be an array of property names")
        else:
            for name in required:
                if known and name not in known:
                    violations.append(f"{path}.required names unknown property '{name}'")
    if "additionalProperties" in node and not isinstance(node["additionalProperties"], bool):
        violations.append(f"{path}.additionalProperties must be a boolean")


def _check_scalar_node(
    node: dict[str, Any], path: str, schema_type: str, violations: list[str]
) -> None:
    """Check the keywords a scalar node may carry."""
    allowed = node.get("enum")
    enum_values = allowed if isinstance(allowed, list) else []
    enum_valid = (
        isinstance(allowed, list)
        and len(allowed) > 0
        and all(_scalar_matches(schema_type, item) for item in allowed)
    )
    if "enum" in node and not enum_valid:
        violations.append(f"{path}.enum must be a non-empty array of {schema_type} values")
    if "const" in node:
        declared = node["const"]
        if not _scalar_matches(schema_type, declared):
            violations.append(f"{path}.const must be a {schema_type} value")
        elif enum_valid and declared not in enum_values:
            violations.append(f"{path}.const must be one of {path}.enum when both are declared")


def _scalar_matches(schema_type: str, value: Any) -> bool:
    """Report whether one value matches one scalar schema type."""
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None
    if schema_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return False


__all__ = [
    "ANNOTATION_KEYWORDS",
    "SCHEMA_TYPES",
    "SUPPORTED_KEYWORDS",
    "WorkflowSchemaError",
    "parse_structured_answer",
    "structured_output_instruction",
    "validate_workflow_schema",
]
