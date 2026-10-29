import base64
import json
import stat

from app.core import averqel_home


def test_local_provider_secret_keyring_is_stable_and_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVERQEL_HOME", str(tmp_path))
    averqel_home.get_averqel_home.cache_clear()
    try:
        active_kid, raw_keyring = averqel_home.ensure_local_provider_secret_keyring()
        payload = json.loads(raw_keyring)
        key_path = tmp_path / "secrets" / "provider-secret-keyring.json"

        assert active_kid == "local"
        assert len(base64.urlsafe_b64decode(payload[active_kid])) == 32
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert averqel_home.ensure_local_provider_secret_keyring() == (
            active_kid,
            json.dumps(payload, separators=(",", ":")),
        )
    finally:
        averqel_home.get_averqel_home.cache_clear()
