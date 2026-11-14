"""Signed remote skill bundles.

A remote catalog is a deployment-level trust decision.  The bundle is a JSON
envelope carrying skill definitions and an Ed25519 signature over their
canonical encoding.  Nothing is loaded unless the signature verifies with the
deployment's configured public key.

The fetcher is injectable so tests exercise the verifier without a network
call.  The default fetcher refuses bodies larger than the configured bound and
applies a socket timeout.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from app.deepspace.services.skill_registry import (
    SkillDefinition,
    parse_skill_document,
)

#: The largest bundle body accepted, in bytes.
MAX_BUNDLE_BYTES = 2_000_000

#: The highest bundle format version this module understands.
SUPPORTED_BUNDLE_VERSION = 1

#: The rank remote skills receive: below project roots, above user roots.
REMOTE_SKILL_RANK = 350

Fetcher = Callable[[str], bytes]


class SkillBundleError(ValueError):
    """A bundle is malformed, unsigned, or fails verification."""


@dataclass(frozen=True, slots=True)
class SignedSkillBundle:
    """One verified bundle."""

    version: int
    skills: tuple[SkillDefinition, ...]
    signature: str


def _canonical(skills: list[dict[str, Any]]) -> bytes:
    """Return the exact bytes the signature covers."""
    return json.dumps(
        skills,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _public_key(value: str) -> Any:
    """Load an Ed25519 public key from raw base64 or PEM text."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    text = str(value or "").strip()
    if not text:
        raise SkillBundleError("No bundle public key is configured.")
    if "BEGIN PUBLIC KEY" in text:
        key = load_pem_public_key(text.encode("utf-8"))
        if not isinstance(key, Ed25519PublicKey):
            raise SkillBundleError("The bundle public key is not Ed25519.")
        return key
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same trust failure.
        raise SkillBundleError("The bundle public key is not valid base64.") from exc
    if len(raw) != 32:
        raise SkillBundleError("An Ed25519 public key must be 32 bytes.")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_signed_bundle(
    raw: bytes | str,
    public_key: str,
    *,
    max_bytes: int = MAX_BUNDLE_BYTES,
) -> SignedSkillBundle:
    """Verify one bundle and return its skill definitions.

    Args:
        raw: The JSON envelope.
        public_key: The deployment's Ed25519 public key, as raw base64 or PEM.
        max_bytes: The largest body accepted.

    Returns:
        The verified bundle.

    Raises:
        SkillBundleError: The body is too large, malformed, unsigned, or the
            signature does not match the configured key.
    """
    from cryptography.exceptions import InvalidSignature

    body = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(body) > max_bytes:
        raise SkillBundleError(f"The bundle exceeds the {max_bytes} byte limit.")
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillBundleError("The bundle is not valid JSON.") from exc
    if not isinstance(envelope, dict):
        raise SkillBundleError("The bundle must be a JSON object.")
    version = envelope.get("version", SUPPORTED_BUNDLE_VERSION)
    if version != SUPPORTED_BUNDLE_VERSION:
        raise SkillBundleError(f"Unsupported bundle version {version!r}.")
    skills = envelope.get("skills")
    signature = str(envelope.get("signature") or "").strip()
    if not isinstance(skills, list) or not skills:
        raise SkillBundleError("The bundle carries no skills.")
    if not signature:
        raise SkillBundleError("The bundle is not signed.")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SkillBundleError("The bundle signature is not valid base64.") from exc
    key = _public_key(public_key)
    try:
        key.verify(signature_bytes, _canonical(skills))
    except InvalidSignature as exc:
        raise SkillBundleError("The bundle signature does not match its contents.") from exc

    definitions: list[SkillDefinition] = []
    for index, item in enumerate(skills):
        if not isinstance(item, dict):
            raise SkillBundleError(f"Skill entry {index} is not an object.")
        content = str(item.get("content") or "")
        if not content.strip():
            raise SkillBundleError(f"Skill entry {index} has no instructions.")
        name = str(item.get("name") or "").strip()
        document = (
            content
            if content.startswith("---\n")
            else (
                "---\n"
                f"name: {name}\n"
                f"description: {json.dumps(str(item.get('description') or ''))}\n"
                f"when-to-use: {json.dumps(str(item.get('when_to_use') or item.get('when-to-use') or ''))}\n"
                f"model-invocable: {str(bool(item.get('model_invocable', True))).lower()}\n"
                f"user-invocable: {str(bool(item.get('user_invocable', True))).lower()}\n"
                f"admin-only: {str(bool(item.get('admin_only', False))).lower()}\n"
                "---\n"
                f"{content}"
            )
        )
        try:
            definitions.append(
                parse_skill_document(
                    document,
                    fallback_name=name or f"remote-skill-{index}",
                    source="remote",
                    provider="remote",
                    rank=REMOTE_SKILL_RANK,
                    strict_names=True,
                )
            )
        except ValueError as exc:
            raise SkillBundleError(f"Skill entry {index} is invalid: {exc}") from exc
    return SignedSkillBundle(version=version, skills=tuple(definitions), signature=signature)


def _default_fetcher(url: str) -> bytes:
    """Fetch one bundle over HTTPS with a bounded body."""
    parsed = urllib.parse.urlsplit(str(url or ""))
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SkillBundleError("Remote skill bundles must use an HTTPS URL without credentials.")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(
        request, timeout=5.0
    ) as response:  # nosec B310 - HTTPS and redirect scheme are validated above/below.
        if urllib.parse.urlsplit(response.geturl()).scheme.casefold() != "https":
            raise SkillBundleError("Remote skill bundle redirects must remain on HTTPS.")
        body = cast(bytes, response.read(MAX_BUNDLE_BYTES + 1))
    if len(body) > MAX_BUNDLE_BYTES:
        raise SkillBundleError("The remote bundle exceeds the size limit.")
    return body


class RemoteSkillProvider:
    """Discover signed skills from one remote bundle."""

    name = "remote"

    def __init__(
        self,
        url: str,
        public_key: str,
        *,
        fetcher: Fetcher | None = None,
        max_bytes: int = MAX_BUNDLE_BYTES,
    ) -> None:
        if not str(url or "").strip():
            raise SkillBundleError("A remote skill bundle needs a URL.")
        if not str(public_key or "").strip():
            raise SkillBundleError("A remote skill bundle needs a public key.")
        parsed = urllib.parse.urlsplit(str(url).strip())
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SkillBundleError(
                "Remote skill bundles must use an HTTPS URL without credentials."
            )
        self.url = url
        self.public_key = public_key
        self.fetcher = fetcher or _default_fetcher
        self.max_bytes = max_bytes

    def discover(self) -> list[SkillDefinition]:
        """Fetch, verify, and return the bundle's skills."""
        raw = self.fetcher(self.url)
        return list(verify_signed_bundle(raw, self.public_key, max_bytes=self.max_bytes).skills)


__all__ = [
    "MAX_BUNDLE_BYTES",
    "REMOTE_SKILL_RANK",
    "RemoteSkillProvider",
    "SUPPORTED_BUNDLE_VERSION",
    "SignedSkillBundle",
    "SkillBundleError",
    "verify_signed_bundle",
]
