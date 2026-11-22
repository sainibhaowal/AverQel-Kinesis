"""Ask the user structured questions for DeepSpace.

The model-facing half of the user-question capability. The tool validates the
request and returns it as an ``awaiting_user`` payload; the turn loop owns the
durable pause, the live SSE card, and the resumed tool result. The tool itself
writes nothing and waits for nothing.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.deepspace.services.tool_registry import ToolArguments
from app.deepspace.services.tools._base import NativeToolSpec, define_native_tool

MAX_QUESTIONS_PER_CALL = 100
MAX_TIMEOUT_SECONDS = 2_147_483
DEFAULT_TIMED_TIMEOUT_SECONDS = 120

SKIPPED_ANSWER_GUIDANCE = (
    "An answer item with empty selected and no custom means the user skipped that question: "
    "continue without it, do not ask it again, and do not treat the skip as permission."
)

PENDING_ANSWER_NOTICE = (
    "No answer batch arrived before the timeout. This is pending, not a skipped answer. "
    "Continue useful independent work. The user can still answer; their reply will be a user "
    "message identified as answer_to_pending_question with this callId and the original "
    "questions. Do not treat this as permission."
)

ASK_USER_QUESTION_DESCRIPTION = (
    "Ask brief, direct, self-contained questions about missing information, preferences, or "
    "decisions. Use user-facing terms; assume no knowledge of background work or internal names. "
    f"Use distinct stable question IDs. {SKIPPED_ANSWER_GUIDANCE} "
    "A pending result instead means no answer batch arrived before the timeout and the user can "
    "still answer."
)


class AskUserOption(ToolArguments):
    """One selectable answer offered to the user."""

    label: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class AskUserQuestionItem(ToolArguments):
    """One question in a single ask call."""

    id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    header: str | None = Field(default=None, max_length=200)
    options: list[AskUserOption] | None = Field(default=None, max_length=20)
    multi_select: bool = False


class AskUserQuestionArguments(ToolArguments):
    """Arguments for one ask_user_question call."""

    questions: list[AskUserQuestionItem] = Field(
        min_length=1,
        max_length=MAX_QUESTIONS_PER_CALL,
    )
    timeout: int | None = Field(
        default=None,
        ge=-1,
        le=MAX_TIMEOUT_SECONDS,
        description=(
            "Wait seconds for the entire batch; omit for a blocking wait. "
            "-1 waits until the user answers."
        ),
    )


def validate_question_ids(questions: list[AskUserQuestionItem]) -> None:
    """Reject duplicate question ids within one call.

    Args:
        questions: The validated questions of one call.

    Raises:
        ValueError: When two questions share an id.
    """
    seen: set[str] = set()
    for question in questions:
        if question.id in seen:
            raise ValueError(f"question id {question.id!r} must be unique within this call")
        seen.add(question.id)


def normalize_questions(value: Any) -> list[dict[str, Any]]:
    """Return the durable wire form of the model-supplied questions.

    Args:
        value: A list of question mappings, or mappings already validated.

    Returns:
        One dictionary per question with only the declared fields.

    Raises:
        ValueError: When the value is not a list or carries an invalid item.
    """
    if not isinstance(value, list) or not value:
        raise ValueError("ask_user_question requires at least one question.")
    questions: list[AskUserQuestionItem] = []
    for item in value:
        if isinstance(item, AskUserQuestionItem):
            questions.append(item)
        elif isinstance(item, dict):
            questions.append(AskUserQuestionItem.model_validate(item))
        else:
            raise ValueError("Each question must be an object.")
    validate_question_ids(questions)
    return [question.model_dump(exclude_none=True, exclude_defaults=True) for question in questions]


def normalize_timeout(value: Any) -> int | None:
    """Validate the optional batch wait.

    Args:
        value: The model-supplied timeout.

    Returns:
        ``None`` for a blocking wait, otherwise the validated seconds.

    Raises:
        ValueError: When the value is not ``-1`` or ``1..2_147_483``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timeout must be -1 or a positive integer up to 2147483 seconds")
    if value == -1:
        return -1
    if value < 1 or value > MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be -1 or a positive integer up to 2147483 seconds")
    return int(value)


async def _ask_user_question(
    arguments: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    """Return the validated question request for the turn loop to pause on.

    Args:
        arguments: The validated tool arguments.
        context: The calling turn's tool context; unused because the pause is
            owned by the turn loop.

    Returns:
        The ``awaiting_user`` payload the turn loop turns into a durable pause.

    Raises:
        ValueError: When the question ids or timeout are invalid.
    """
    questions = normalize_questions(arguments.get("questions"))
    timeout = normalize_timeout(arguments.get("timeout"))
    return {
        "awaiting_user": True,
        "kind": "ask_user_question",
        "questions": questions,
        "timeout": timeout,
        "timed": timeout is not None and timeout != -1,
    }


def summarize_answers(
    questions: list[Any],
    answers: list[Any] | None,
) -> str:
    """Render a structured answer batch as readable user text.

    Args:
        questions: The original question dictionaries.
        answers: The submitted answers, in question order when available.

    Returns:
        One line per answered question, suitable for the persisted user
        message and the provider conversation.
    """
    by_id = {str(item.get("id") or ""): item for item in (answers or []) if isinstance(item, dict)}
    lines: list[str] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or "")
        answer = by_id.get(question_id)
        if answer is None:
            continue
        selected = [str(value) for value in (answer.get("selected") or [])]
        custom = str(answer.get("custom") or "").strip()
        parts = [*selected]
        if custom:
            parts.append(custom)
        label = str(question.get("header") or question.get("question") or question_id)
        lines.append(f"{label}: {', '.join(parts) if parts else '(skipped)'}")
    return "\n".join(lines)


def build_answer_result(
    pending: dict[str, Any],
    answers: list[Any] | None,
    answer_text: str,
) -> dict[str, Any]:
    """Build the model-facing result of a resumed ask_user_question call.

    Args:
        pending: The durable pending question checkpoint.
        answers: Structured answers supplied by the client, when present.
        answer_text: The user's free-text reply, used when no structured
            answers were supplied.

    Returns:
        Either the answer batch or the timed pending result.
    """
    questions = pending.get("questions")
    if not isinstance(questions, list):
        questions = []
    cleaned = _clean_answers(answers, questions)
    if not cleaned and answer_text:
        cleaned = _answers_from_text(questions, answer_text)
    if cleaned:
        return {"answers": cleaned}
    return {
        "pending": True,
        "callId": str(pending.get("call_id") or ""),
        "message": PENDING_ANSWER_NOTICE,
    }


def _clean_answers(
    answers: list[Any] | None,
    questions: list[Any],
) -> list[dict[str, Any]]:
    if not isinstance(answers, list):
        return []
    known_ids = {
        str(item.get("id") or "") for item in questions if isinstance(item, dict) and item.get("id")
    }
    cleaned: list[dict[str, Any]] = []
    for item in answers:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("id") or "").strip()
        if not question_id or (known_ids and question_id not in known_ids):
            continue
        selected_value = item.get("selected")
        selected = (
            [str(entry) for entry in selected_value if str(entry).strip()]
            if isinstance(selected_value, list)
            else []
        )
        custom_value = item.get("custom")
        custom = str(custom_value).strip() if custom_value is not None else ""
        result: dict[str, Any] = {"id": question_id, "selected": selected}
        if custom:
            result["custom"] = custom[:4000]
        cleaned.append(result)
    return cleaned


def _answers_from_text(
    questions: list[Any],
    answer_text: str,
) -> list[dict[str, Any]]:
    """Map one free-text reply onto the first question's declared options."""
    text = answer_text.strip()
    if not text:
        return []
    first = questions[0] if questions and isinstance(questions[0], dict) else {}
    question_id = str(first.get("id") or "").strip()
    if not question_id:
        return []
    labels = [
        str(option.get("label") or "").strip()
        for option in (first.get("options") or [])
        if isinstance(option, dict)
    ]
    selected = [label for label in labels if label and label.casefold() == text.casefold()]
    result: dict[str, Any] = {"id": question_id, "selected": selected}
    if not selected:
        result["custom"] = text[:4000]
    return [result]


ASK_USER_QUESTION_TOOL: NativeToolSpec = define_native_tool(
    name="ask_user_question",
    description=ASK_USER_QUESTION_DESCRIPTION,
    properties={
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_QUESTIONS_PER_CALL,
            "description": "Questions to ask the user.",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Stable id for this question; echoed in the answer.",
                    },
                    "question": {
                        "type": "string",
                        "description": "The specific question to ask the user.",
                    },
                    "header": {
                        "type": "string",
                        "description": (
                            'Optional short heading for the question, such as "Confirm" '
                            'or "Choose Mode".'
                        ),
                    },
                    "options": {
                        "type": "array",
                        "description": (
                            "Optional choices to show the user. If you recommend one, put it "
                            'first and append "(Recommended)" to that label.'
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Short user-facing option label.",
                                },
                                "description": {
                                    "type": "string",
                                    "description": (
                                        "One sentence explaining the tradeoff or impact."
                                    ),
                                },
                            },
                            "required": ["label"],
                        },
                    },
                    "multi_select": {
                        "type": "boolean",
                        "description": (
                            "Whether the user may select more than one option. "
                            "Defaults to false."
                        ),
                    },
                },
                "required": ["id", "question"],
            },
        },
        "timeout": {
            "type": "integer",
            "minimum": -1,
            "maximum": MAX_TIMEOUT_SECONDS,
            "description": (
                "Wait seconds for the entire batch; omit for a blocking wait. "
                "-1 waits until the user answers."
            ),
        },
    },
    required=["questions"],
    arguments_model=AskUserQuestionArguments,
    mode="interactive",
    handler=_ask_user_question,
)

__all__ = [
    "ASK_USER_QUESTION_DESCRIPTION",
    "ASK_USER_QUESTION_TOOL",
    "AskUserQuestionArguments",
    "AskUserOption",
    "AskUserQuestionItem",
    "DEFAULT_TIMED_TIMEOUT_SECONDS",
    "MAX_QUESTIONS_PER_CALL",
    "MAX_TIMEOUT_SECONDS",
    "PENDING_ANSWER_NOTICE",
    "SKIPPED_ANSWER_GUIDANCE",
    "build_answer_result",
    "normalize_questions",
    "normalize_timeout",
    "summarize_answers",
    "validate_question_ids",
]
