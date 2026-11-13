from __future__ import annotations

import ast
from pathlib import Path

from app.system.schemas.errors import (
    WEEK2_ERROR_CODES,
    Week2ErrorCode,
    is_week2_error_code,
)


def test_week2_error_code_catalog_contains_required_contract_examples() -> None:
    required = {
        "TENANT_REQUIRED",
        "INVALID_UPLOAD_TYPE",
        "DOC_TOO_LARGE",
        "IDEMPOTENCY_CONFLICT",
        "MALWARE_SCAN_FAILED",
        "CONNECTOR_OAUTH_UNSUPPORTED",
        "CONNECTOR_OAUTH_INVALID",
        "CONNECTOR_OAUTH_TOKEN_EXCHANGE_FAILED",
    }
    assert required.issubset(WEEK2_ERROR_CODES)


def test_week2_error_code_enum_and_predicate_are_consistent() -> None:
    enum_values = {entry.value for entry in Week2ErrorCode}
    assert enum_values == set(WEEK2_ERROR_CODES)

    for code in enum_values:
        assert is_week2_error_code(code) is True

    assert is_week2_error_code("NOT_A_REAL_WEEK2_CODE") is False


def test_literal_api_error_codes_are_registered() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    unregistered: dict[str, list[str]] = {}

    for source_path in app_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "ApiError":
                continue
            code = next(
                (
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "code"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                None,
            )
            if code is not None and code not in WEEK2_ERROR_CODES:
                unregistered.setdefault(code, []).append(
                    f"{source_path.relative_to(app_root)}:{node.lineno}"
                )

    assert not unregistered, f"Unregistered ApiError codes: {unregistered}"
