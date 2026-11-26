from __future__ import annotations

from typing import Any

import pytest

from app.providers.services.openai_compatible_config import (
    OpenAICompatibleEndpointSettings,
    OpenAICompatibleReasoningSettings,
    openai_compatible_settings,
    provider_model_listing_enabled,
    reasoning_efforts_from_metadata,
    set_json_path,
)


class TestOpenAICompatibleReasoningSettings:
    """Tests for the reasoning settings model validation."""

    def test_validate_parameter_path_accepts_valid_path(self) -> None:
        settings = OpenAICompatibleReasoningSettings(
            effort_parameter="reasoning.effort",
            supported_efforts=["low", "medium", "high"],
        )
        assert settings.effort_parameter == "reasoning.effort"

    def test_validate_parameter_path_rejects_whitespace_only(self) -> None:
        # Whitespace-only string becomes None after strip
        # This triggers the model validator which requires effort_parameter when supported_efforts is set
        with pytest.raises(ValueError, match="effort_parameter is required"):
            OpenAICompatibleReasoningSettings(
                effort_parameter="   ",
                supported_efforts=["low"],
                effort_values={"low": "low"},
            )

    def test_validate_parameter_path_rejects_too_long(self) -> None:
        with pytest.raises(ValueError, match="reasoning parameter must be a safe"):
            OpenAICompatibleReasoningSettings(effort_parameter="a" * 129)

    def test_validate_parameter_path_rejects_too_deep(self) -> None:
        with pytest.raises(ValueError, match="reasoning parameter must be a safe"):
            OpenAICompatibleReasoningSettings(effort_parameter="a.b.c.d.e.f.g.h.i")

    def test_validate_parameter_path_rejects_reserved_root(self) -> None:
        with pytest.raises(ValueError, match="reasoning parameter must be a safe"):
            OpenAICompatibleReasoningSettings(effort_parameter="model.effort")

    def test_validate_parameter_path_rejects_invalid_chars(self) -> None:
        with pytest.raises(ValueError, match="reasoning parameter must be a safe"):
            OpenAICompatibleReasoningSettings(effort_parameter="invalid-path")

    def test_deduplicate_efforts_normalizes_and_dedupes(self) -> None:
        settings = OpenAICompatibleReasoningSettings(
            supported_efforts=["Low", "LOW", "Medium", "high"],
            effort_parameter="effort",
        )
        assert settings.supported_efforts == ["low", "medium", "high"]

    def test_deduplicate_efforts_rejects_invalid(self) -> None:
        # normalize_reasoning_effort_value returns None for invalid efforts
        with pytest.raises(ValueError, match="supported_efforts contains an invalid"):
            OpenAICompatibleReasoningSettings(
                supported_efforts=["not_a_real_effort_option"],
                effort_parameter="effort",
                effort_values={"not_a_real_effort_option": "value"},
            )

    def test_validate_effort_values_normalizes_keys(self) -> None:
        settings = OpenAICompatibleReasoningSettings(
            supported_efforts=["low", "high"],
            effort_parameter="effort",
            effort_values={"LOW": "low", "HIGH": "max"},
        )
        assert settings.effort_values == {"low": "low", "high": "max"}

    def test_validate_effort_values_rejects_empty_value(self) -> None:
        with pytest.raises(ValueError, match="reasoning effort values must be non-empty"):
            OpenAICompatibleReasoningSettings(
                supported_efforts=["low"],
                effort_parameter="effort",
                effort_values={"low": ""},
            )

    def test_validate_effort_values_rejects_control_chars(self) -> None:
        with pytest.raises(ValueError, match="reasoning effort values must be non-empty"):
            OpenAICompatibleReasoningSettings(
                supported_efforts=["low"],
                effort_parameter="effort",
                effort_values={"low": "low\x00"},
            )

    def test_validate_effort_values_rejects_too_long(self) -> None:
        with pytest.raises(ValueError, match="reasoning effort values must be non-empty"):
            OpenAICompatibleReasoningSettings(
                supported_efforts=["low"],
                effort_parameter="effort",
                effort_values={"low": "x" * 129},
            )

    def test_validate_effort_values_rejects_too_many(self) -> None:
        with pytest.raises(ValueError, match="effort_values contains too many"):
            OpenAICompatibleReasoningSettings(
                supported_efforts=[f"effort_{i}" for i in range(40)],
                effort_parameter="effort",
                effort_values={f"effort_{i}": str(i) for i in range(40)},
            )

    def test_validate_reasoning_mapping_requires_effort_parameter(self) -> None:
        with pytest.raises(ValueError, match="effort_parameter is required"):
            OpenAICompatibleReasoningSettings(
                supported_efforts=["low", "medium"],
                effort_parameter=None,
            )

    def test_validate_reasoning_mapping_requires_effort_values(self) -> None:
        with pytest.raises(ValueError, match="every enabled reasoning level needs an effort value"):
            OpenAICompatibleReasoningSettings(
                supported_efforts=["low", "medium"],
                effort_parameter="effort",
                effort_values={"low": "low"},
            )

    def test_validate_reasoning_mapping_rejects_overlapping_paths(self) -> None:
        with pytest.raises(ValueError, match="effort and toggle fields must not overwrite"):
            OpenAICompatibleReasoningSettings(
                supported_efforts=["low"],
                effort_parameter="reasoning.effort",
                effort_values={"low": "low"},
                toggle_parameter="reasoning.effort",
            )


class TestOpenAICompatibleEndpointSettings:
    """Tests for the endpoint settings model validation."""

    def test_validate_endpoint_path_accepts_valid(self) -> None:
        settings = OpenAICompatibleEndpointSettings(
            chat_completions_path="/chat/completions",
            responses_path="/responses",
            models_path="/models",
        )
        assert settings.chat_completions_path == "/chat/completions"

    def test_validate_endpoint_path_rejects_empty(self) -> None:
        # Empty string becomes None, which is valid
        settings = OpenAICompatibleEndpointSettings(chat_completions_path="")
        assert settings.chat_completions_path is None

    def test_validate_endpoint_path_rejects_query(self) -> None:
        with pytest.raises(ValueError, match="endpoint paths must be absolute"):
            OpenAICompatibleEndpointSettings(chat_completions_path="/path?query=1")

    def test_validate_endpoint_path_rejects_fragment(self) -> None:
        with pytest.raises(ValueError, match="endpoint paths must be absolute"):
            OpenAICompatibleEndpointSettings(chat_completions_path="/path#fragment")

    def test_validate_endpoint_path_rejects_traversal(self) -> None:
        with pytest.raises(ValueError, match="endpoint paths must be absolute"):
            OpenAICompatibleEndpointSettings(chat_completions_path="/../path")

    def test_validate_endpoint_path_rejects_backslash(self) -> None:
        with pytest.raises(ValueError, match="endpoint paths must be absolute"):
            OpenAICompatibleEndpointSettings(chat_completions_path="/path\\bad")

    def test_validate_endpoint_path_rejects_too_long(self) -> None:
        with pytest.raises(ValueError, match="endpoint paths must be absolute"):
            OpenAICompatibleEndpointSettings(chat_completions_path="/" + "a" * 512)

    def test_validate_endpoint_path_rejects_unsupported_chars(self) -> None:
        with pytest.raises(ValueError, match="endpoint path contains unsupported"):
            OpenAICompatibleEndpointSettings(chat_completions_path="/path with spaces")

    def test_validate_endpoint_path_rejects_multiple_model_placeholders(self) -> None:
        with pytest.raises(ValueError, match="endpoint path contains unsupported"):
            OpenAICompatibleEndpointSettings(chat_completions_path="/{model}/{model}")

    def test_validate_endpoint_path_accepts_single_model_placeholder(self) -> None:
        settings = OpenAICompatibleEndpointSettings(chat_completions_path="/chat/{model}")
        assert settings.chat_completions_path == "/chat/{model}"

    def test_validate_endpoint_path_strips_trailing_slash(self) -> None:
        settings = OpenAICompatibleEndpointSettings(chat_completions_path="/chat/completions/")
        assert settings.chat_completions_path == "/chat/completions"

    def test_validate_auth_header_name_accepts_valid(self) -> None:
        settings = OpenAICompatibleEndpointSettings(auth_header_name="X-Custom-Auth")
        assert settings.auth_header_name == "X-Custom-Auth"

    def test_validate_auth_header_name_rejects_invalid_chars(self) -> None:
        with pytest.raises(ValueError, match="auth_header_name must be a valid"):
            OpenAICompatibleEndpointSettings(auth_header_name="Invalid Header")

    def test_validate_auth_header_name_rejects_reserved(self) -> None:
        with pytest.raises(ValueError, match="auth_header_name must be a valid"):
            OpenAICompatibleEndpointSettings(auth_header_name="Connection")

    def test_validate_models_route_rejects_model_placeholder(self) -> None:
        with pytest.raises(ValueError, match="models_path cannot contain a model placeholder"):
            OpenAICompatibleEndpointSettings(models_path="/models/{model}")


class TestOpenAICompatibleSettingsResolution:
    """Tests for the openai_compatible_settings resolution function."""

    def test_cline_disables_embeddings(self) -> None:
        settings = openai_compatible_settings({}, provider_type="cline")
        assert settings.embeddings_path is None
        assert settings.models_path == "/models"

    def test_moonshot_disables_embeddings(self) -> None:
        settings = openai_compatible_settings({}, provider_type="moonshot")
        assert settings.embeddings_path is None

    def test_zai_disables_models_and_embeddings(self) -> None:
        settings = openai_compatible_settings({}, provider_type="zai")
        assert settings.models_path is None
        assert settings.embeddings_path is None

    def test_meta_uses_responses_and_disables_embeddings(self) -> None:
        settings = openai_compatible_settings({}, provider_type="meta")
        assert settings.api_mode == "responses"
        assert settings.embeddings_path is None
        assert settings.send_temperature is False

    def test_custom_uses_metadata(self) -> None:
        metadata = {"openai_compatible": {"chat_completions_path": "/custom/chat"}}
        settings = openai_compatible_settings(metadata, provider_type="custom")
        assert settings.chat_completions_path == "/custom/chat"

    def test_custom_rejects_non_object_metadata(self) -> None:
        with pytest.raises(ValueError, match="metadata_json.openai_compatible must be an object"):
            openai_compatible_settings(
                {"openai_compatible": "not_an_object"}, provider_type="custom"
            )


class TestProviderModelListingEnabled:
    """Tests for the provider_model_listing_enabled function."""

    def test_cline_returns_true_when_configured_false(self) -> None:
        assert provider_model_listing_enabled("cline", False) is True

    def test_other_provider_returns_configured_value(self) -> None:
        assert provider_model_listing_enabled("custom", True) is True
        assert provider_model_listing_enabled("custom", False) is False


class TestReasoningEffortsFromMetadata:
    """Tests for the reasoning_efforts_from_metadata function."""

    def test_returns_empty_for_no_reasoning(self) -> None:
        efforts = reasoning_efforts_from_metadata({})
        assert efforts == []

    def test_returns_supported_efforts(self) -> None:
        metadata = {
            "openai_compatible": {
                "reasoning": {
                    "supported_efforts": ["low", "high"],
                    "effort_parameter": "effort",
                    "effort_values": {"low": "low", "high": "high"},
                }
            }
        }
        efforts = reasoning_efforts_from_metadata(metadata)
        assert efforts == ["low", "high"]


class TestSetJsonPath:
    """Tests for the set_json_path utility function."""

    def test_sets_nested_path(self) -> None:
        payload: dict[str, Any] = {}
        set_json_path(payload, "a.b.c", "value")
        assert payload == {"a": {"b": {"c": "value"}}}

    def test_overwrites_existing_value(self) -> None:
        payload = {"a": {"b": {"c": "old"}}}
        set_json_path(payload, "a.b.c", "new")
        assert payload == {"a": {"b": {"c": "new"}}}

    def test_replaces_non_dict_intermediate(self) -> None:
        payload = {"a": {"b": "not_a_dict"}}
        set_json_path(payload, "a.b.c", "value")
        assert payload == {"a": {"b": {"c": "value"}}}

    def test_creates_missing_intermediates(self) -> None:
        payload = {}
        set_json_path(payload, "x.y.z", 42)
        assert payload == {"x": {"y": {"z": 42}}}
