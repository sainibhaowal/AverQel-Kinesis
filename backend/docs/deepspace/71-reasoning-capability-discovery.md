# Reasoning capability discovery

## Use case

Use this chapter when selecting a model's reasoning behavior in DeepSpace or
when adding model-list metadata to a provider adapter. The **Think** menu is
per selected model: it can show the model's published effort levels, a
supported disable option, and the provider's own default.

## Why it matters

Reasoning option names and request fields differ by provider and model. Sending
a guessed effort can cause provider validation errors, while forcing an effort
when the user chose the provider default can override the model's own runtime
policy. DeepSpace therefore treats **Provider default** as an explicit choice
that sends no reasoning override.

## How it works

### Discovery and storage

Provider model discovery runs through `ProviderModelsService.refresh_models`
and the authenticated `POST /api/v1/providers/{provider_id}/models/refresh`
route. The route enforces the caller's tenant and provider permissions before
resolving provider credentials or contacting the model catalog.

For a discovered chat model, the provider adapter returns a bounded
`ProviderModelInfo.capabilities` object. The refresh service normalizes its
reasoning fields and stores them in the existing
`provider_model_cache.capabilities_json` row, scoped to tenant, provider
configuration, model name, and model kind. No credential or raw catalog
response is written to this cache. `GET /api/v1/providers/{provider_id}/models`
returns the stored capabilities to the authorized caller.

The DeepSpace model selector reads that response and passes the selected
model's capabilities to the composer. On model selection, it refreshes missing
or expired catalog data when the provider supports model listing. Successful
discovery is durable in the same application database used by the current
runtime, including the standalone desktop profile; no separate cache file or
schema migration is required.

### Provider metadata contract

Adapters should publish a model-specific reasoning descriptor under
`capabilities.reasoning` (or the equivalent fields at the capabilities root):

```json
{
  "reasoning_capabilities_known": true,
  "supports_reasoning": true,
  "supported_reasoning_efforts": ["low", "high", "very_high"],
  "reasoning_effort_values": {
    "low": "short",
    "high": "deep",
    "very_high": "maximum"
  },
  "supports_thinking_toggle": true,
  "request_controls_on": ["reasoning_effort"],
  "request_controls_off": ["reasoning_effort_none"]
}
```

The shared normalizer also accepts `supported_efforts` and `effort_values` as
aliases. It validates effort identifiers and native values, limits list and
map sizes, and accepts only the request-control names implemented by the
shared adapter. This lets a newly added OpenAI-compatible provider use the
same discovery and serializer when its catalog publishes this contract; a
provider with a different wire protocol still needs its adapter to implement
that protocol safely.

### Choosing and sending a setting

| Composer choice | Request behavior |
|---|---|
| **Provider default** | Sends no reasoning override. The selected model and provider decide dynamically. This is the initial choice; the chat request carries an internal `auto` sentinel so it stays distinct from legacy Off requests, and the provider adapter omits reasoning controls. |
| Advertised effort | Sends the selected model's mapped native effort through the provider's supported request control. |
| **Off** | Appears only when that model publishes a supported disable control; sends the provider's explicit disable request. |
| **On** | Appears for a model that exposes a binary enable control without effort levels. |

The selected effort is normalized again by the backend against the selected
model's cached capabilities before a provider request is created. Provider
adapters use `reasoning_mode=auto` to omit reasoning controls, `off` for an
explicit disable, and `explicit` for a selected model effort. Legacy requests
that omit the internal Auto marker keep their previous null-means-Off behavior
when the model capabilities are known.

If a provider does not publish reasoning metadata and no verified model-family
profile applies, DeepSpace shows **Auto** only and lets the provider decide.
It does not guess effort choices from unverified model-name matches or assume
that a model supports a disable switch. A verified provider profile remains a fallback for known
families whose catalog omits these fields; explicit model metadata takes
precedence.

## What to watch for

- A provider cannot be discovered through a universal endpoint if its catalog
  does not expose model reasoning metadata. In that case **Provider default**
  remains available; the interface cannot prove hidden provider capabilities.
- A non-OpenAI provider with a different request format must map its validated
  metadata through its own adapter. Catalog metadata alone must never create an
  arbitrary request field or JSON path.
- A provider may change capabilities after a catalog refresh window. Refresh
  the provider's model list to pick up changes; previously cached capabilities
  remain scoped to that provider model row.
- Local DB-free coverage verifies normalization, storage response shaping,
  composer selection, and request serialization. It does not prove every
  external provider accepts every advertised setting; provider-specific live
  checks require an isolated provider endpoint and credentials.
