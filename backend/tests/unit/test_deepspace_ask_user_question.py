from __future__ import annotations

import pytest

from app.deepspace.services.tools.ask_user import (
    ASK_USER_QUESTION_TOOL,
    PENDING_ANSWER_NOTICE,
    build_answer_result,
    normalize_questions,
    normalize_timeout,
    summarize_answers,
)

pytestmark = pytest.mark.unit_no_db


def test_tool_declares_the_structured_question_contract() -> None:
    assert ASK_USER_QUESTION_TOOL.name == "ask_user_question"
    assert ASK_USER_QUESTION_TOOL.mode == "interactive"
    function = ASK_USER_QUESTION_TOOL.definition["function"]
    schema = function["parameters"]
    assert schema["required"] == ["questions"]
    assert schema["properties"]["questions"]["maxItems"] == 10
    assert "skipped" in function["description"]


def test_normalize_questions_keeps_declared_fields_and_ids() -> None:
    questions = normalize_questions(
        [
            {
                "id": "mode",
                "question": "Which mode?",
                "header": "Choose Mode",
                "options": [{"label": "Fast", "description": "Lower cost"}],
                "multi_select": False,
            },
            {"id": "notes", "question": "Anything else?"},
        ]
    )

    assert [question["id"] for question in questions] == ["mode", "notes"]
    assert questions[0]["options"] == [{"label": "Fast", "description": "Lower cost"}]
    assert "multi_select" not in questions[1]


def test_normalize_questions_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        normalize_questions(
            [
                {"id": "same", "question": "One?"},
                {"id": "same", "question": "Two?"},
            ]
        )


def test_normalize_questions_rejects_empty_or_malformed_input() -> None:
    with pytest.raises(ValueError):
        normalize_questions([])
    with pytest.raises(ValueError):
        normalize_questions("not-a-list")
    with pytest.raises(ValueError):
        normalize_questions([{"id": "", "question": "Missing id"}])


@pytest.mark.parametrize("value", [None, -1, 1, 120, 2_147_483])
def test_normalize_timeout_accepts_the_declared_range(value: int | None) -> None:
    assert normalize_timeout(value) == value


@pytest.mark.parametrize("value", [0, -2, 2_147_484, True, 1.5, "120"])
def test_normalize_timeout_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        normalize_timeout(value)


def test_build_answer_result_returns_the_answer_batch() -> None:
    pending = {
        "call_id": "call-1",
        "questions": [
            {"id": "mode", "question": "Which mode?", "options": [{"label": "Fast"}]},
            {"id": "notes", "question": "Anything else?"},
        ],
    }

    result = build_answer_result(
        pending,
        [
            {"id": "mode", "selected": ["Fast"], "custom": ""},
            {"id": "notes", "selected": [], "custom": "Ship it"},
            {"id": "unknown", "selected": ["Dropped"]},
        ],
        "ignored because structured answers won",
    )

    assert result == {
        "answers": [
            {"id": "mode", "selected": ["Fast"]},
            {"id": "notes", "selected": [], "custom": "Ship it"},
        ]
    }


def test_build_answer_result_maps_free_text_to_the_first_question() -> None:
    pending = {
        "call_id": "call-1",
        "questions": [
            {
                "id": "mode",
                "question": "Which mode?",
                "options": [{"label": "Fast"}, {"label": "Safe"}],
            }
        ],
    }

    selected = build_answer_result(pending, None, "Fast")
    custom = build_answer_result(pending, None, "Use the staging cluster")

    assert selected == {"answers": [{"id": "mode", "selected": ["Fast"]}]}
    assert custom == {
        "answers": [{"id": "mode", "selected": [], "custom": "Use the staging cluster"}]
    }


def test_build_answer_result_reports_pending_when_nothing_arrived() -> None:
    result = build_answer_result({"call_id": "call-9", "questions": []}, None, "")

    assert result == {
        "pending": True,
        "callId": "call-9",
        "message": PENDING_ANSWER_NOTICE,
    }


def test_summarize_answers_renders_skipped_and_custom_items() -> None:
    text = summarize_answers(
        [
            {"id": "mode", "header": "Mode", "question": "Which mode?"},
            {"id": "notes", "question": "Anything else?"},
        ],
        [
            {"id": "mode", "selected": ["Fast"]},
            {"id": "notes", "selected": [], "custom": ""},
        ],
    )

    assert text == "Mode: Fast\nAnything else?: (skipped)"
