from tests.support.live_provider import LiveProviderSettings


def test_authless_local_provider_is_configured_without_an_api_key() -> None:
    settings = LiveProviderSettings.from_environment(
        {
            "AVERQEL_LIVE_PROVIDER_TYPE": "lmstudio",
            "AVERQEL_LIVE_BASE_URL": "http://127.0.0.1:1234/v1",
            "AVERQEL_LIVE_MODEL": "local-chat-model",
        }
    )

    assert settings.is_configured
    assert settings.provider_type == "lmstudio"
    assert settings.api_key is None
    assert settings.effective_second_model == "local-chat-model"


def test_generic_api_provider_accepts_an_optional_credential() -> None:
    settings = LiveProviderSettings.from_environment(
        {
            "AVERQEL_LIVE_PROVIDER_TYPE": "custom",
            "AVERQEL_LIVE_BASE_URL": "https://provider.example/v1",
            "AVERQEL_LIVE_MODEL": "model-one",
            "AVERQEL_LIVE_SECOND_MODEL": "model-two",
            "AVERQEL_LIVE_API_KEY": "test-credential",
        }
    )

    assert settings.is_configured
    assert settings.api_key == "test-credential"
    assert settings.effective_second_model == "model-two"


def test_missing_required_route_values_stays_unconfigured_without_key_errors() -> None:
    settings = LiveProviderSettings.from_environment(
        {"AVERQEL_LIVE_API_KEY": "optional-credential"}
    )

    assert not settings.is_configured
    assert "AVERQEL_LIVE_API_KEY is optional" in settings.skip_reason
