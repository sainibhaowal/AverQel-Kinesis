"""Tests for signed remote skill bundles."""

from __future__ import annotations

import base64
import json
import urllib.request

import pytest

from app.deepspace.services.skill_bundle import (
    MAX_BUNDLE_BYTES,
    REMOTE_SKILL_RANK,
    RemoteSkillProvider,
    SkillBundleError,
    _default_fetcher,
    verify_signed_bundle,
)

pytestmark = pytest.mark.unit_no_db

pytest.importorskip("cryptography")


def _canonical(skills: list[dict[str, object]]) -> bytes:
    return json.dumps(skills, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, base64.b64encode(raw).decode("ascii")


def _bundle(
    *,
    skills: list[dict[str, object]] | None = None,
    version: int = 1,
    signature: str | None = None,
    private=None,
) -> str:
    entries = skills or [
        {
            "name": "remote-demo",
            "description": "A signed remote skill.",
            "when_to_use": "A task needs the remote procedure.",
            "content": "Do the remote thing carefully.",
        }
    ]
    if private is None:
        private, _public = _keypair()
    if signature is None:
        signature = base64.b64encode(private.sign(_canonical(entries))).decode("ascii")
    return json.dumps({"version": version, "skills": entries, "signature": signature})


def test_verifies_a_signed_bundle_and_builds_definitions() -> None:
    private, public = _keypair()
    bundle = verify_signed_bundle(_bundle(private=private), public)

    assert bundle.version == 1
    assert len(bundle.skills) == 1
    skill = bundle.skills[0]
    assert skill.name == "remote-demo"
    assert skill.provider == "remote"
    assert skill.source == "remote"
    assert skill.rank == REMOTE_SKILL_RANK
    assert skill.description == "A signed remote skill."
    assert skill.when_to_use == "A task needs the remote procedure."
    assert skill.content == "Do the remote thing carefully."
    assert skill.content_sha256


def test_document_content_with_frontmatter_is_used_verbatim() -> None:
    private, public = _keypair()
    document = (
        "---\nname: remote-full\ndescription: Full document\n"
        "user-invocable: false\n---\nFull instructions here."
    )
    bundle = verify_signed_bundle(
        _bundle(skills=[{"name": "remote-full", "content": document}], private=private),
        public,
    )
    skill = bundle.skills[0]
    assert skill.name == "remote-full"
    assert skill.description == "Full document"
    assert skill.invocation.user_invocable is False
    assert skill.content == "Full instructions here."


def test_pem_public_key_is_supported() -> None:
    from cryptography.hazmat.primitives import serialization

    private, _public = _keypair()
    pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    bundle = verify_signed_bundle(_bundle(private=private), pem)
    assert bundle.skills[0].name == "remote-demo"


def test_pem_public_key_with_another_algorithm_is_rejected() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private, _public = _keypair()
    pem = (
        rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )

    with pytest.raises(SkillBundleError, match="not Ed25519"):
        verify_signed_bundle(_bundle(private=private), pem)


def test_tampering_fails_verification() -> None:
    private, public = _keypair()
    body = json.loads(_bundle(private=private))
    body["skills"][0]["content"] = "Tampered instructions."
    with pytest.raises(SkillBundleError, match="signature"):
        verify_signed_bundle(json.dumps(body), public)


def test_wrong_key_is_rejected() -> None:
    private, _public = _keypair()
    _other_private, other_public = _keypair()
    with pytest.raises(SkillBundleError, match="signature"):
        verify_signed_bundle(_bundle(private=private), other_public)


def test_unsigned_bundle_is_rejected() -> None:
    _private, public = _keypair()
    body = json.loads(_bundle(signature=""))
    with pytest.raises(SkillBundleError, match="not signed"):
        verify_signed_bundle(json.dumps(body), public)


def test_malformed_envelopes_are_rejected() -> None:
    _private, public = _keypair()
    with pytest.raises(SkillBundleError, match="valid JSON"):
        verify_signed_bundle("{", public)
    with pytest.raises(SkillBundleError, match="JSON object"):
        verify_signed_bundle("[]", public)
    with pytest.raises(SkillBundleError, match="version"):
        verify_signed_bundle(_bundle(version=99), public)
    with pytest.raises(SkillBundleError, match="no skills"):
        verify_signed_bundle(json.dumps({"version": 1, "skills": [], "signature": "abc"}), public)
    with pytest.raises(SkillBundleError, match="base64"):
        verify_signed_bundle(_bundle(signature="not base64"), public)


def test_oversized_bundle_is_rejected() -> None:
    _private, public = _keypair()
    with pytest.raises(SkillBundleError, match="byte limit"):
        verify_signed_bundle(_bundle(), public, max_bytes=10)


def test_bad_public_keys_are_rejected() -> None:
    private, _public = _keypair()
    body = _bundle(private=private)
    with pytest.raises(SkillBundleError, match="No bundle public key"):
        verify_signed_bundle(body, "")
    with pytest.raises(SkillBundleError, match="not valid base64"):
        verify_signed_bundle(body, "!!!!")
    with pytest.raises(SkillBundleError, match="32 bytes"):
        verify_signed_bundle(body, base64.b64encode(b"short").decode("ascii"))


def test_invalid_skill_entries_are_rejected() -> None:
    private, public = _keypair()

    def signed(entries: list[dict[str, object]]) -> str:
        signature = base64.b64encode(private.sign(_canonical(entries))).decode("ascii")
        return json.dumps({"version": 1, "skills": entries, "signature": signature})

    with pytest.raises(SkillBundleError, match="not an object"):
        verify_signed_bundle(signed(["not-an-object"]), public)  # type: ignore[list-item]

    # A signed but empty entry fails content validation.
    empty = [{"name": "remote-demo", "content": ""}]
    with pytest.raises(SkillBundleError, match="no instructions"):
        verify_signed_bundle(signed(empty), public)

    bad_name = [{"name": "Bad Name", "content": "Instructions"}]
    with pytest.raises(SkillBundleError, match="invalid"):
        verify_signed_bundle(signed(bad_name), public)


def test_remote_provider_uses_the_injected_fetcher() -> None:
    private, public = _keypair()
    seen: list[str] = []

    def fetcher(url: str) -> bytes:
        seen.append(url)
        return _bundle(private=private).encode("utf-8")

    provider = RemoteSkillProvider(
        "https://skills.example.test/bundle.json", public, fetcher=fetcher
    )
    skills = provider.discover()

    assert seen == ["https://skills.example.test/bundle.json"]
    assert [skill.name for skill in skills] == ["remote-demo"]


def test_remote_provider_requires_an_https_url_without_credentials() -> None:
    _private, public = _keypair()
    with pytest.raises(SkillBundleError, match="HTTPS"):
        RemoteSkillProvider("http://skills.example.test/bundle.json", public)
    with pytest.raises(SkillBundleError, match="credentials"):
        RemoteSkillProvider("https://user:pass@skills.example.test/bundle.json", public)
    with pytest.raises(SkillBundleError, match="needs a URL"):
        RemoteSkillProvider("", public)
    with pytest.raises(SkillBundleError, match="public key"):
        RemoteSkillProvider("https://skills.example.test/bundle.json", "")


def test_default_fetcher_refuses_non_https_without_a_request() -> None:
    with pytest.raises(SkillBundleError, match="HTTPS"):
        _default_fetcher("http://127.0.0.1:1/bundle.json")
    with pytest.raises(SkillBundleError, match="HTTPS"):
        _default_fetcher("file:///tmp/bundle.json")


def test_default_fetcher_bounds_responses_and_rejects_http_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self, body: bytes, final_url: str) -> None:
            self.body = body
            self.final_url = final_url

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def geturl(self) -> str:
            return self.final_url

        def read(self, limit: int) -> bytes:
            assert limit == MAX_BUNDLE_BYTES + 1
            return self.body

    response = Response(b"signed bundle", "https://skills.example.test/final.json")
    monkeypatch.setattr(urllib.request, "urlopen", lambda _request, timeout: response)
    assert _default_fetcher("https://skills.example.test/bundle.json") == b"signed bundle"

    response = Response(b"x" * (MAX_BUNDLE_BYTES + 1), "https://skills.example.test/final.json")
    with pytest.raises(SkillBundleError, match="size limit"):
        _default_fetcher("https://skills.example.test/bundle.json")

    response = Response(b"signed bundle", "http://skills.example.test/final.json")
    with pytest.raises(SkillBundleError, match="redirects"):
        _default_fetcher("https://skills.example.test/bundle.json")


def test_provider_verification_failure_propagates() -> None:
    signing_private, _signing_public = _keypair()
    _other_private, verifying_public = _keypair()
    provider = RemoteSkillProvider(
        "https://skills.example.test/bundle.json",
        verifying_public,
        fetcher=lambda _url: _bundle(private=signing_private).encode("utf-8"),
    )
    with pytest.raises(SkillBundleError, match="signature"):
        provider.discover()
