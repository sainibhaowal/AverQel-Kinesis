# 57. Signed remote skill catalog

A remote catalog is a deployment-level trust decision.  The bundle is a JSON
envelope carrying skill definitions and an Ed25519 signature over their
canonical encoding.  Nothing is loaded unless the signature verifies with the
deployment's configured public key.

## Use case

- A central skill registry publishes signed skill bundles for multiple deployments.
- A deployment configures the HTTPS URL and Ed25519 public key; skills appear automatically.
- Skills can be updated centrally without redeploying the application.
- The signature ensures the bundle has not been tampered with.

## Why it matters

- Remote skills are not loaded unless the signature verifies — a compromised server cannot inject arbitrary instructions.
- The fetcher is injectable for tests; the default fetcher refuses non-HTTPS, credentials in URLs, and bodies larger than 2 MB.
- Remote skills have rank 350 (between custom roots and shared user roots).
- The catalog carries `incomplete: true` if the remote fetch fails, so the model knows some skills could not be confirmed.

## How it works

### Bundle format

```json
{
  "version": 1,
  "skills": [
    {
      "name": "remote-skill",
      "description": "A signed remote skill.",
      "when_to_use": "A task needs the remote procedure.",
      "content": "Do the remote thing carefully.",
      "model_invocable": true,
      "user_invocable": true
    }
  ],
  "signature": "base64-ed25519-signature"
}
```

- `version`: must be 1 (highest supported).
- `skills`: array of skill objects (at least one).
- `signature`: base64-encoded Ed25519 signature over the canonical JSON of `skills`.

### Canonical encoding

The signature covers:
```json
[
  {"content": "...", "description": "...", "name": "...", "when_to_use": "..."}
]
```
- Sorted by name (case-insensitive).
- `JSON.stringify` with `ensure_ascii=False`, `sort_keys=True`, `separators=(",", ":")`.
- UTF-8 encoded.

### Verification

`verify_signed_bundle(raw_bytes, public_key, max_bytes=2_000_000)`:

1. Parses JSON.
2. Checks `version == 1`.
3. Checks `skills` is non-empty array.
4. Decodes base64 signature (32 bytes raw).
5. Verifies Ed25519 signature over canonical skills array.
6. Parses each skill with `parse_skill_document` (strict names).
7. Returns `SkillBundle` with `SkillDefinition` list.

### Remote provider

`RemoteSkillProvider(url, public_key, fetcher?, max_bytes?)`:

- Rank: 350 (`REMOTE_SKILL_RANK`).
- Fetcher is injectable (default: HTTPS GET with 5s timeout, 2 MB limit).
- Refuses non-HTTPS, credentials in URL, empty URL, empty public key.
- On fetch failure: raises `SkillBundleError`, registry marks observation `ok=false`, catalog carries `incomplete=true`.
- Skills returned with `source="remote"`, `provider="remote"`.

### Public key formats

- Raw base64 (32 bytes): `base64(ed25519_public_key_raw)`
- PEM: `-----BEGIN PUBLIC KEY-----...`

### Settings

| Setting | Type | Default | Effect |
|---|---|---|---|
| `deepspace_skill_remote_url` | `str` | `""` | Optional HTTPS URL for signed remote skill catalog bundle. |
| `deepspace_skill_remote_public_key` | `str` | `""` | Base64-encoded Ed25519 public key (raw or PEM) to verify the remote catalog. |

Remote provider is only mounted when **both** settings are non-empty.

### Fetcher injection (for tests)

```python
def fake_fetcher(url: str) -> bytes:
    return signed_bundle_bytes

provider = RemoteSkillProvider("https://skills.example.test/bundle.json", public_key, fetcher=fake_fetcher)
```

The default fetcher (`_default_fetcher`) validates HTTPS before making any request.

## What to watch for

- The remote catalog is a deployment trust decision.  The public key must be distributed securely (e.g., baked into the desktop app, configured via deployment automation).
- A failed remote fetch does not break the turn; the catalog is marked `incomplete` and the model sees a notice.
- The fetcher has a 5-second timeout and 2 MB body limit.  Large catalogs must stay within this limit.
- Only Ed25519 signatures are supported.  Key rotation requires updating the deployment's public key.
- Remote skills use `strict_names=True` — skill names in the bundle must be kebab-case.

## Related chapters

- [Bundled Skills](33-bundled-skills.md) — skill discovery, providers, and Office workflows.
- [Per-turn Skill Catalog Injection](54-skill-catalog-and-injection.md) — how the model learns available skills.
- [Configuration Reference](22-configuration-reference.md) — all `deepspace_skill_*` settings.
