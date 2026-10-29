"use client";

import { AnimatePresence, motion } from "framer-motion";
import { Activity, Link2, Link2Off } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  type CreateProviderInput,
  type ProviderCatalogEntry,
  type ProviderConfig,
  type UpdateProviderInput,
  type ProviderModel,
  previewProviderModels,
} from "@/lib/providers-api";

import ProviderAuthFields from "./ProviderAuthFields";
import ProviderModelPicker from "./ProviderModelPicker";
import OpenAICompatibleSettingsPanel, {
  buildOpenAICompatibleMetadata,
  DEFAULT_OPENAI_COMPATIBLE_SETTINGS,
  readOpenAICompatibleSettings,
  type OpenAICompatibleFormSettings,
} from "./OpenAICompatibleSettingsPanel";
import OpenAICodexLoginPanel from "./OpenAICodexLoginPanel";
import ProviderLocalRuntimeHelp from "./ProviderLocalRuntimeHelp";
import HuggingFaceEmbeddingHub from "./HuggingFaceEmbeddingHub";
import HuggingFaceRerankerHub from "./HuggingFaceRerankerHub";

interface ProviderFormProps {
  catalogEntry: ProviderCatalogEntry | null;
  provider: ProviderConfig | null;
  models: ProviderModel[];
  loadingModels?: boolean;
  kind: "chat" | "embedding" | "reranker" | "web" | "all";
  saving: boolean;
  busyAction: string | null;
  onCancel: () => void;
  onSubmit: (payload: CreateProviderInput | UpdateProviderInput) => Promise<void>;
  onTest?: (provider: ProviderConfig) => Promise<void>;
  onDelete?: (provider: ProviderConfig) => void;
  onDisconnect?: (provider: ProviderConfig) => void;
  onReconnect?: (provider: ProviderConfig) => void;
  onRefreshModels?: () => void;
  onPullModel?: () => void;
  pullModelName?: string;
  onPullModelNameChange?: (v: string) => void;
  pullingModel?: boolean;
}

const PROVIDER_BASE_URL_PRESETS: Record<string, string> = {
  openai: "https://api.openai.com/v1",
  deepseek: "https://api.deepseek.com",
  anthropic: "https://api.anthropic.com/v1",
  google: "https://generativelanguage.googleapis.com/v1beta",
  groq: "https://api.groq.com/openai/v1",
  mistral: "https://api.mistral.ai/v1",
  meta: "https://api.meta.ai/v1",
  cline: "https://api.cline.bot/api/v1",
  moonshot: "https://api.moonshot.ai/v1",
  zai: "https://api.z.ai/api/paas/v4",
  "nvidia-nim": "https://integrate.api.nvidia.com/v1",
  manus: "https://api.manus.ai",
  lmstudio: "http://localhost:1234/v1",
  ollama: "http://localhost:11434",
  together: "https://api.together.xyz/v1",
  fireworks: "https://api.fireworks.ai/inference/v1",
  perplexity: "https://api.perplexity.ai",
  openrouter: "https://openrouter.ai/api/v1",
  larprouter: "https://larprouter.com/v1",
  beam: "https://beam.ai/api/v1",
  "opencode-zen": "https://opencode.ai/zen/v1",
  cohere: "https://api.cohere.ai/v1",
  tavily: "https://api.tavily.com",
  searxng: "http://127.0.0.1:8888",
};

const INVISIBLE_INPUT_CHARS = /[\u200B-\u200D\u2060\uFEFF]/g;

function sanitizeInput(value: string): string {
  return value.replace(INVISIBLE_INPUT_CHARS, "").replace(/`+/g, "").trim();
}

function isValidHttpUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

function previewCanOmitBaseUrl(providerType: string): boolean {
  return providerType === "sentence-transformers";
}

export default function ProviderForm({
  catalogEntry,
  provider,
  models,
  kind,
  saving,
  onSubmit,
  onTest,
  onDisconnect,
  onReconnect,
}: ProviderFormProps) {
  const activeProviderType = provider?.provider_type || catalogEntry?.provider_type || "";
  const isOpenAIProvider = activeProviderType === "openai";
  const isCustomCompatible = activeProviderType === "custom";
  const isLocalRuntimeProvider =
    activeProviderType === "lmstudio" || activeProviderType === "ollama";
  const supportsManualChatModel =
    isCustomCompatible || ["cline", "zai"].includes(activeProviderType);
  const catalogFamilyRef = useRef(activeProviderType);
  const [electronAvailable, setElectronAvailable] = useState(false);
  const [runtimeTarget, setRuntimeTarget] = useState<"server" | "desktop">(
    isLocalRuntimeProvider || provider?.metadata_json?.runtime_target === "desktop"
      ? "desktop"
      : "server",
  );

  const [apiBaseUrl, setApiBaseUrl] = useState(
    provider?.api_base_url || PROVIDER_BASE_URL_PRESETS[activeProviderType] || "",
  );
  const [authMode, setAuthMode] = useState(
    provider?.auth_mode || catalogEntry?.auth_modes[0] || "api_key",
  );
  const [codexAccountId, setCodexAccountId] = useState<string | null>(() => {
    const codex = provider?.metadata_json?.codex_desktop;
    return codex &&
      typeof codex === "object" &&
      "account_id" in codex &&
      typeof codex.account_id === "string"
      ? codex.account_id
      : null;
  });
  const [codexModels, setCodexModels] = useState<ElectronCodexModel[]>(() => {
    const codex = provider?.metadata_json?.codex_desktop;
    if (
      !codex ||
      typeof codex !== "object" ||
      !("models" in codex) ||
      !Array.isArray(codex.models)
    ) {
      return [];
    }
    return codex.models.filter(
      (model): model is ElectronCodexModel =>
        Boolean(model) &&
        typeof model === "object" &&
        "id" in model &&
        typeof model.id === "string" &&
        "name" in model &&
        typeof model.name === "string",
    );
  });
  const [secretValue, setSecretValue] = useState("");
  const [searchLanguage, setSearchLanguage] = useState(
    typeof provider?.metadata_json?.language === "string"
      ? provider.metadata_json.language
      : "auto",
  );
  const [allowedDomains, setAllowedDomains] = useState(
    Array.isArray(provider?.metadata_json?.allowed_domains)
      ? provider.metadata_json.allowed_domains.join(", ")
      : "",
  );
  const [blockedDomains, setBlockedDomains] = useState(
    Array.isArray(provider?.metadata_json?.blocked_domains)
      ? provider.metadata_json.blocked_domains.join(", ")
      : "",
  );

  const [defaultChatModel, setDefaultChatModel] = useState(provider?.default_chat_model || "");
  const [defaultEmbeddingModel, setDefaultEmbeddingModel] = useState(
    provider?.default_embedding_model || "",
  );
  const [defaultRerankerModel, setDefaultRerankerModel] = useState(
    provider?.default_reranker_model || "",
  );
  const [compatibleSettings, setCompatibleSettings] = useState<OpenAICompatibleFormSettings>(() =>
    readOpenAICompatibleSettings(provider?.metadata_json),
  );

  const [previewModels, setPreviewModels] = useState<ProviderModel[]>(provider ? models : []);
  const [discoveringModels, setDiscoveringModels] = useState(false);
  const [discoverError, setDiscoverError] = useState<string | null>(null);

  const isEmbeddingFamily =
    kind === "embedding" ||
    (kind === "all" &&
      (activeProviderType === "sentence-transformers" ||
        Boolean(catalogEntry?.supports_embeddings ?? provider?.supports_embeddings)));
  const isRerankerFamily =
    kind === "reranker" ||
    (kind === "all" &&
      (activeProviderType === "sentence-transformers" ||
        Boolean(catalogEntry?.supports_reranking ?? provider?.supports_reranking)));
  const [showEmbeddingHub, setShowEmbeddingHub] = useState(true);
  const [showRerankerHub, setShowRerankerHub] = useState(true);

  useEffect(() => {
    queueMicrotask(() => {
      const hasElectron = Boolean(window.electron?.isElectron);
      setElectronAvailable(
        Boolean(hasElectron && (window.electron?.codex || window.electron?.localRuntime)),
      );
    });
  }, []);

  const localRuntimeUnavailable = false;

  const isCodexLogin = isOpenAIProvider && authMode === "codex_desktop";
  const showOpenAIAuthToggle = isOpenAIProvider;

  // Sync when switching to an existing provider
  useEffect(() => {
    if (provider) {
      queueMicrotask(() => {
        setApiBaseUrl(provider.api_base_url || "");
        setRuntimeTarget(
          isLocalRuntimeProvider || provider.metadata_json?.runtime_target === "desktop"
            ? "desktop"
            : "server",
        );
        setAuthMode(provider.auth_mode || "none");
        const codex = provider.metadata_json?.codex_desktop;
        setCodexAccountId(
          codex &&
            typeof codex === "object" &&
            "account_id" in codex &&
            typeof codex.account_id === "string"
            ? codex.account_id
            : null,
        );
        setCodexModels(
          codex && typeof codex === "object" && "models" in codex && Array.isArray(codex.models)
            ? codex.models.filter(
                (model): model is ElectronCodexModel =>
                  Boolean(model) &&
                  typeof model === "object" &&
                  "id" in model &&
                  typeof model.id === "string" &&
                  "name" in model &&
                  typeof model.name === "string",
              )
            : [],
        );
        setDefaultChatModel(provider.default_chat_model || "");
        setDefaultEmbeddingModel(provider.default_embedding_model || "");
        setDefaultRerankerModel(provider.default_reranker_model || "");
        setCompatibleSettings(readOpenAICompatibleSettings(provider.metadata_json));
        setPreviewModels(models);
        setSecretValue("");
        setSearchLanguage(
          typeof provider.metadata_json?.language === "string"
            ? provider.metadata_json.language
            : "auto",
        );
        setAllowedDomains(
          Array.isArray(provider.metadata_json?.allowed_domains)
            ? provider.metadata_json.allowed_domains.join(", ")
            : "",
        );
        setBlockedDomains(
          Array.isArray(provider.metadata_json?.blocked_domains)
            ? provider.metadata_json.blocked_domains.join(", ")
            : "",
        );
        setDiscoverError(null);
      });
    }
  }, [isLocalRuntimeProvider, models, provider]);

  // A catalog family is a wholly new configuration. Never retain another
  // provider's endpoint, credentials, selected model, or discovery result
  // while creating a new provider. The previous implementation only synced
  // saved providers, which made every newly selected family inherit the last
  // form's OpenCode Zen state.
  useEffect(() => {
    if (provider || !catalogEntry) return;
    if (catalogFamilyRef.current === catalogEntry.provider_type) return;
    catalogFamilyRef.current = catalogEntry.provider_type;
    queueMicrotask(() => {
      const providerType = catalogEntry.provider_type;
      setApiBaseUrl(PROVIDER_BASE_URL_PRESETS[providerType] || "");
      setRuntimeTarget(
        providerType === "lmstudio" || providerType === "ollama" ? "desktop" : "server",
      );
      setAuthMode(catalogEntry.auth_modes[0] || "api_key");
      setCodexAccountId(null);
      setCodexModels([]);
      setSecretValue("");
      setDefaultChatModel("");
      setDefaultEmbeddingModel("");
      setDefaultRerankerModel("");
      setCompatibleSettings({
        ...DEFAULT_OPENAI_COMPATIBLE_SETTINGS,
        reasoning: { ...DEFAULT_OPENAI_COMPATIBLE_SETTINGS.reasoning },
      });
      setPreviewModels([]);
      setDiscoveringModels(false);
      setDiscoverError(null);
      setSearchLanguage("auto");
      setAllowedDomains("");
      setBlockedDomains("");
    });
  }, [catalogEntry, provider]);

  const normalizeApiBaseUrl = useCallback(
    (value: string): string => {
      const trimmed = sanitizeInput(value);
      if (!trimmed || activeProviderType !== "lmstudio") return trimmed;
      try {
        const parsed = new URL(trimmed);
        const path = parsed.pathname.replace(/\/+$/, "");
        if (path.endsWith("/v1")) return parsed.toString().replace(/\/+$/, "");
        parsed.pathname = path ? `${path}/v1` : "/v1";
        return parsed.toString().replace(/\/+$/, "");
      } catch {
        return trimmed;
      }
    },
    [activeProviderType],
  );

  const normalizedPreviewUrl = useMemo(
    () => normalizeApiBaseUrl(apiBaseUrl),
    [apiBaseUrl, normalizeApiBaseUrl],
  );
  const normalizedSecretValue = useMemo(() => sanitizeInput(secretValue), [secretValue]);

  const handleApiBaseUrlChange = (value: string) => {
    setApiBaseUrl(value);
    const normalizedValue = normalizeApiBaseUrl(value);
    setDiscoverError(
      normalizedValue && !isValidHttpUrl(normalizedValue)
        ? "Enter a valid http(s) runtime URL to preview models."
        : null,
    );
  };

  const supportsModelListing = Boolean(
    activeProviderType === "cline" ||
    (isCustomCompatible
      ? compatibleSettings.models_path.trim()
      : (catalogEntry?.supports_model_listing ?? provider?.supports_model_listing)),
  );
  const compatibleMetadata = useMemo(
    () =>
      isCustomCompatible
        ? buildOpenAICompatibleMetadata(provider?.metadata_json, compatibleSettings)
        : undefined,
    [compatibleSettings, isCustomCompatible, provider?.metadata_json],
  );

  const handleCodexAccountChange = useCallback(
    (accountId: string | null, accountModels: ElectronCodexModel[]) => {
      setCodexAccountId(accountId);
      setCodexModels(accountModels);
      const providerModels: ProviderModel[] = accountModels.map((model) => ({
        id: null,
        provider_config_id: provider?.id || null,
        model_name: model.id,
        model_kind: "chat",
        display_name: model.name,
        context_window: null,
        capabilities_json: {
          ...(model.supportedReasoningEfforts
            ? {
                supported_reasoning_efforts: model.supportedReasoningEfforts,
                supports_reasoning: model.supportedReasoningEfforts.length > 0,
                reasoning_effort_values: model.reasoningEffortValues,
              }
            : {}),
        },
        is_available: true,
        last_seen_at: null,
      }));
      setPreviewModels(providerModels);
      if (defaultChatModel && !accountModels.some((model) => model.id === defaultChatModel)) {
        setDefaultChatModel("");
      }
    },
    [defaultChatModel, provider?.id],
  );

  // Model discovery
  useEffect(() => {
    if (!activeProviderType || !supportsModelListing) return;
    if (localRuntimeUnavailable) return;
    if (isCodexLogin) return;
    // Cline's public catalog can be previewed before the API key is entered.
    if (
      authMode === "api_key" &&
      !normalizedSecretValue &&
      !provider &&
      activeProviderType !== "cline"
    ) {
      return;
    }
    // For editing existing provider with no new secret, skip (models already loaded)
    if (authMode === "api_key" && !normalizedSecretValue && provider) return;
    if (!authMode) return;

    if (authMode !== "api_key" && normalizedSecretValue) {
      queueMicrotask(() => {
        setDiscoverError("Clear the secret value for the selected auth mode.");
        setDiscoveringModels(false);
      });
      return;
    }

    if (!normalizedPreviewUrl && !previewCanOmitBaseUrl(activeProviderType)) {
      return;
    }

    if (normalizedPreviewUrl && !isValidHttpUrl(normalizedPreviewUrl)) {
      return;
    }

    let cancelled = false;
    const timeoutId = setTimeout(async () => {
      setDiscoveringModels(true);
      setDiscoverError(null);
      try {
        const items = await previewProviderModels({
          provider_type: activeProviderType,
          api_base_url: normalizedPreviewUrl || null,
          auth_mode: authMode,
          api_key: normalizedSecretValue || null,
          supports_chat: catalogEntry?.supports_chat ?? provider?.supports_chat ?? false,
          supports_embeddings:
            catalogEntry?.supports_embeddings ?? provider?.supports_embeddings ?? false,
          supports_reranking:
            catalogEntry?.supports_reranking ?? provider?.supports_reranking ?? false,
          supports_model_listing: supportsModelListing,
          ...(isLocalRuntimeProvider
            ? {
                metadata_json: { runtime_target: runtimeTarget, protocol: "desktop_local_runtime" },
              }
            : compatibleMetadata
              ? { metadata_json: compatibleMetadata }
              : {}),
        });
        if (!cancelled) {
          setPreviewModels(items);
          setDiscoverError(items.length === 0 ? "No models detected." : null);
        }
      } catch (error) {
        if (!cancelled) {
          setDiscoverError(error instanceof Error ? error.message : "Discovery failed.");
        }
      } finally {
        if (!cancelled) setDiscoveringModels(false);
      }
    }, 600);

    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, [
    activeProviderType,
    authMode,
    isCodexLogin,
    normalizedPreviewUrl,
    normalizedSecretValue,
    supportsModelListing,
    compatibleMetadata,
    isLocalRuntimeProvider,
    localRuntimeUnavailable,
    runtimeTarget,
    catalogEntry?.supports_chat,
    catalogEntry?.supports_embeddings,
    catalogEntry?.supports_reranking,
    provider,
  ]);

  const handleLocalSubmit = async () => {
    if (localRuntimeUnavailable) {
      setDiscoverError(
        "LM Studio and Ollama connections are available only in the AverQel Desktop app.",
      );
      return;
    }
    if (isCodexLogin) {
      if (!codexAccountId || codexModels.length === 0) {
        setDiscoverError("Sign in with ChatGPT and load this account's models first.");
        return;
      }
      if (!defaultChatModel || !codexModels.some((model) => model.id === defaultChatModel)) {
        setDiscoverError("Choose a model from the connected ChatGPT account.");
        return;
      }
    }
    if (
      isOpenAIProvider &&
      authMode === "api_key" &&
      provider?.auth_mode !== "api_key" &&
      !normalizedSecretValue
    ) {
      setDiscoverError("Enter an OpenAI API key before switching this connection to OpenAI API.");
      return;
    }
    if (
      activeProviderType === "cline" &&
      authMode === "api_key" &&
      !normalizedSecretValue &&
      !provider
    ) {
      setDiscoverError("Enter a Cline API key before linking this provider.");
      return;
    }

    const searchMetadata =
      activeProviderType === "searxng"
        ? {
            language: sanitizeInput(searchLanguage) || "auto",
            allowed_domains: allowedDomains
              .split(",")
              .map((item) => sanitizeInput(item).toLowerCase())
              .filter(Boolean)
              .slice(0, 50),
            blocked_domains: blockedDomains
              .split(",")
              .map((item) => sanitizeInput(item).toLowerCase())
              .filter(Boolean)
              .slice(0, 50),
          }
        : undefined;
    const codexMetadata = isCodexLogin
      ? {
          codex_desktop: {
            account_id: codexAccountId,
            models: codexModels.map((model) => ({
              id: model.id,
              name: model.name,
              ...(model.supportedReasoningEfforts
                ? {
                    supported_reasoning_efforts: model.supportedReasoningEfforts,
                    reasoning_effort_values: model.reasoningEffortValues,
                  }
                : {}),
            })),
          },
        }
      : undefined;
    const switchingAwayFromCodex = provider?.auth_mode === "codex_desktop" && !isCodexLogin;
    const localRuntimeMetadata = isLocalRuntimeProvider
      ? { runtime_target: "desktop", protocol: "desktop_local_runtime" }
      : undefined;
    const basePayload: CreateProviderInput = {
      display_name: isCodexLogin
        ? provider?.display_name === "OpenAI API"
          ? "OpenAI Codex"
          : provider?.display_name || "OpenAI Codex"
        : provider?.display_name || catalogEntry?.display_name || "",
      provider_type: activeProviderType,
      api_base_url: isCodexLogin ? null : normalizeApiBaseUrl(apiBaseUrl) || null,
      auth_mode: authMode,
      enabled: provider?.enabled ?? true,
      supports_chat: isCodexLogin
        ? true
        : (catalogEntry?.supports_chat ?? provider?.supports_chat ?? false),
      supports_embeddings: isCodexLogin
        ? false
        : (catalogEntry?.supports_embeddings ?? provider?.supports_embeddings ?? false),
      supports_reranking: isCodexLogin
        ? false
        : (catalogEntry?.supports_reranking ?? provider?.supports_reranking ?? false),
      supports_web_search:
        catalogEntry?.supports_web_search ?? provider?.supports_web_search ?? false,
      supports_model_listing: isCodexLogin
        ? true
        : activeProviderType === "cline" ||
          (catalogEntry?.supports_model_listing ?? provider?.supports_model_listing ?? false),
      supports_model_install: isCodexLogin
        ? false
        : (catalogEntry?.supports_model_install ?? provider?.supports_model_install ?? false),
      is_local: isCodexLogin ? false : (catalogEntry?.is_local ?? provider?.is_local ?? false),
      ...(codexMetadata
        ? { metadata_json: codexMetadata }
        : switchingAwayFromCodex
          ? { metadata_json: {} }
          : compatibleMetadata
            ? { metadata_json: compatibleMetadata }
            : searchMetadata
              ? { metadata_json: searchMetadata }
              : localRuntimeMetadata
                ? { metadata_json: localRuntimeMetadata }
                : {}),
    };

    if (normalizedSecretValue) basePayload.api_key = normalizedSecretValue;
    if (kind === "chat" || kind === "all")
      basePayload.default_chat_model = defaultChatModel || null;
    if (kind === "embedding" || kind === "all")
      basePayload.default_embedding_model = defaultEmbeddingModel || null;
    if (kind === "reranker" || kind === "all")
      basePayload.default_reranker_model = defaultRerankerModel || null;

    if (provider) {
      const updatePayload: UpdateProviderInput = {
        display_name: basePayload.display_name,
        api_base_url: basePayload.api_base_url,
        auth_mode: basePayload.auth_mode,
        enabled: basePayload.enabled,
        default_chat_model: basePayload.default_chat_model,
        default_embedding_model: basePayload.default_embedding_model,
        default_reranker_model: basePayload.default_reranker_model,
        ...(basePayload.metadata_json ? { metadata_json: basePayload.metadata_json } : {}),
      };
      if (basePayload.api_key) updatePayload.api_key = basePayload.api_key;
      await onSubmit(updatePayload);
      return;
    }

    await onSubmit(basePayload);
  };

  const activeModelName =
    kind === "chat"
      ? defaultChatModel
      : kind === "embedding"
        ? defaultEmbeddingModel
        : kind === "reranker"
          ? defaultRerankerModel
          : "";

  const filteredModels = useMemo(() => {
    const visibleModels = previewModels.length > 0 ? previewModels : provider ? models : [];
    if (kind === "all") return visibleModels;
    return visibleModels.filter((m) => {
      if (kind === "chat") return m.model_kind === "chat" || m.model_kind === "other";
      if (kind === "embedding") return m.model_kind === "embedding";
      if (kind === "reranker") return m.model_kind === "reranker";
      if (kind === "web") return false;
      return true;
    });
  }, [kind, models, previewModels, provider]);

  const providerAuthCatalogEntry = catalogEntry
    ? {
        ...catalogEntry,
        auth_modes: isOpenAIProvider
          ? ["api_key"]
          : catalogEntry.auth_modes.filter((mode) => electronAvailable || mode !== "codex_desktop"),
      }
    : undefined;

  const handleOpenAIModeChange = (mode: string) => {
    setAuthMode(mode);
    setSecretValue("");
    setDiscoverError(null);
    if (mode === "api_key" && !apiBaseUrl) {
      setApiBaseUrl(PROVIDER_BASE_URL_PRESETS.openai);
    }
    if (mode === "codex_desktop") setDiscoveringModels(false);
  };

  const isHealthy = provider?.latest_health?.status === "healthy";
  const healthStatus = provider?.latest_health?.status;
  const requiresApiKey = authMode === "api_key";
  const waitingForToken =
    requiresApiKey && !normalizedSecretValue && (!provider || provider.auth_mode !== "api_key");
  const waitingForCodex =
    isCodexLogin &&
    (!codexAccountId ||
      codexModels.length === 0 ||
      !defaultChatModel ||
      !codexModels.some((model) => model.id === defaultChatModel));

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex min-h-0 flex-1 flex-col"
    >
      <div className="scrollbar-thin min-h-0 flex-1 space-y-6 overflow-y-auto pb-2">
        {/* scrollable content */}
        {/* Header */}
        <div>
          <p className="text-muted-foreground/60 font-mono text-[10px] font-bold tracking-[0.24em] uppercase">
            {kind.toUpperCase()} Setup
          </p>
          <h2 className="text-foreground mt-1 text-3xl font-black tracking-tight">
            {isCodexLogin && provider?.display_name === "OpenAI API"
              ? "OpenAI Codex"
              : provider?.display_name || catalogEntry?.display_name || "New Connection"}
          </h2>
          <p className="text-muted-foreground mt-1.5 max-w-xl text-sm">
            {isCodexLogin
              ? "Connect a ChatGPT account and choose one of its available models for DeepSpace."
              : `Connect this ${kind === "all" ? "provider" : kind} provider, enter the API key, then link it.`}
          </p>
          <div className="settings-divider mt-3" style={{ maxWidth: "50%" }} />
        </div>

        {/* Fields */}
        <div className="space-y-6">
          {showOpenAIAuthToggle ? (
            <div
              aria-label="OpenAI connection type"
              className="inline-flex rounded-xl border border-slate-200 dark:border-white/10 bg-slate-100 dark:bg-slate-900/90 p-1 shadow-inner"
              role="tablist"
            >
              {(
                [
                  ["api_key", "OpenAI API"],
                  ["codex_desktop", "Codex Login"],
                ] as const
              ).map(([mode, label]) => {
                const isSelected = authMode === mode;
                return (
                  <button
                    key={mode}
                    type="button"
                    role="tab"
                    aria-selected={isSelected}
                    onClick={() => handleOpenAIModeChange(mode)}
                    className={`relative rounded-lg px-4 py-2 text-xs font-bold transition-all duration-200 ${
                      isSelected
                        ? "text-white dark:text-slate-950 font-black"
                        : "text-slate-600 dark:text-slate-400 hover:text-slate-950 dark:hover:text-white font-semibold"
                    }`}
                  >
                    {isSelected && (
                      <motion.div
                        layoutId="openai-auth-mode-active-pill"
                        className="bg-gradient-to-r from-emerald-600 to-teal-600 dark:from-emerald-400 dark:to-teal-400 absolute inset-0 rounded-lg shadow-sm"
                        transition={{ type: "spring", stiffness: 450, damping: 32 }}
                      />
                    )}
                    <span className="relative z-10">{label}</span>
                  </button>
                );
              })}
            </div>
          ) : null}

          <AnimatePresence mode="wait" initial={false}>
            {isCodexLogin ? (
              <motion.div
                key="codex-login-panel"
                initial={{ opacity: 0, x: 12 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -12 }}
                transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
              >
                <OpenAICodexLoginPanel
                  key={provider?.id || "new-openai-codex"}
                  initialAccountId={codexAccountId}
                  onChange={handleCodexAccountChange}
                />
              </motion.div>
            ) : (
              <motion.div
                key="openai-api-panel"
                initial={{ opacity: 0, x: -12 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 12 }}
                transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
              >
                <ProviderAuthFields
                  providerType={activeProviderType}
                  catalogEntry={providerAuthCatalogEntry}
                  authMode={authMode}
                  onAuthModeChange={showOpenAIAuthToggle ? handleOpenAIModeChange : setAuthMode}
                  apiBaseUrl={apiBaseUrl}
                  onApiBaseUrlChange={handleApiBaseUrlChange}
                  secretValue={secretValue}
                  onSecretValueChange={setSecretValue}
                  maskedSummary={provider?.secrets?.[0]?.masked_value || null}
                  disabled={localRuntimeUnavailable}
                />
              </motion.div>
            )}
          </AnimatePresence>

          {isLocalRuntimeProvider ? (
            localRuntimeUnavailable ? (
              <div
                role="status"
                className="rounded-2xl border border-slate-400/20 bg-slate-400/5 p-5 text-sm text-slate-300"
              >
                <p className="font-semibold text-slate-200">
                  {activeProviderType === "ollama" ? "Ollama" : "LM Studio"} is a desktop-only local
                  runtime
                </p>
                <p className="mt-1 text-xs leading-5 text-slate-400">
                  Install or open AverQel Desktop to connect to a model running on this computer.
                  Local LM Studio and Ollama connections are disabled in the web app.
                </p>
              </div>
            ) : (
              <div className="theme-panel-muted space-y-4 rounded-2xl p-5 border border-glass-border">
                <div>
                  <p className="text-foreground text-sm font-semibold">This computer</p>
                  <p className="text-muted-foreground mt-1 text-xs">
                    AverQel Desktop connects to LM Studio or Ollama on this computer through its
                    local runtime bridge.
                  </p>
                </div>
                <ProviderLocalRuntimeHelp providerType={activeProviderType} />
              </div>
            )
          ) : null}

          {isCustomCompatible ? (
            <OpenAICompatibleSettingsPanel
              value={compatibleSettings}
              onChange={setCompatibleSettings}
            />
          ) : null}

          {activeProviderType === "searxng" && (
            <div className="theme-panel-muted grid gap-4 rounded-2xl p-5 border border-glass-border md:grid-cols-3">
              <div className="space-y-1.5">
                <label className="text-muted-foreground/60 px-0.5 font-mono text-[9px] tracking-[0.2em] uppercase">
                  Search language
                </label>
                <input
                  value={searchLanguage}
                  onChange={(event) => setSearchLanguage(event.target.value)}
                  placeholder="auto"
                  className="border-glass-border bg-surface-1 text-foreground h-10 w-full rounded-xl border px-3 text-xs outline-none"
                />
              </div>
              <div className="space-y-1.5">
                <label className="text-muted-foreground/60 px-0.5 font-mono text-[9px] tracking-[0.2em] uppercase">
                  Allowed domains
                </label>
                <input
                  value={allowedDomains}
                  onChange={(event) => setAllowedDomains(event.target.value)}
                  placeholder="example.com, docs.example.org"
                  className="border-glass-border bg-surface-1 text-foreground h-10 w-full rounded-xl border px-3 text-xs outline-none"
                />
              </div>
              <div className="space-y-1.5">
                <label className="text-muted-foreground/60 px-0.5 font-mono text-[9px] tracking-[0.2em] uppercase">
                  Blocked domains
                </label>
                <input
                  value={blockedDomains}
                  onChange={(event) => setBlockedDomains(event.target.value)}
                  placeholder="ads.example.com"
                  className="border-glass-border bg-surface-1 text-foreground h-10 w-full rounded-xl border px-3 text-xs outline-none"
                />
              </div>
            </div>
          )}

          {kind !== "web" ? (
            <div className="space-y-2">
              <label className="text-muted-foreground/60 px-1 font-mono text-[10px] font-bold tracking-[0.2em] uppercase">
                {kind.toUpperCase()} Model
              </label>
              {supportsManualChatModel ? (
                <div className={isCustomCompatible ? "grid gap-3 md:grid-cols-2" : "grid gap-3"}>
                  {(kind === "chat" || kind === "all") && (
                    <ProviderModelPicker
                      label="Chat model ID"
                      value={defaultChatModel}
                      models={filteredModels}
                      onChange={setDefaultChatModel}
                      kinds={["chat", "other"]}
                      allowClear
                      clearLabel="No chat model selected"
                      allowCustomValue
                      placeholder="Search or enter chat model ID"
                      helperText={
                        activeProviderType === "cline"
                          ? "Search Cline's model catalog by name or ID. Exact provider/model IDs can also be entered manually."
                          : activeProviderType === "zai"
                            ? "Enter the GLM model ID. Z.AI's API reference does not publish a model-list route."
                            : "Search discovered names, IDs, context, or capabilities, or enter an exact model ID."
                      }
                    />
                  )}
                  {isCustomCompatible && (kind === "embedding" || kind === "all") && (
                    <ProviderModelPicker
                      label="Embedding model ID"
                      value={defaultEmbeddingModel}
                      models={filteredModels}
                      onChange={setDefaultEmbeddingModel}
                      kinds={["embedding"]}
                      allowClear
                      clearLabel="No embedding model selected"
                      allowCustomValue
                      placeholder="Search or enter embedding model ID"
                      helperText="Search discovered entries or enter an exact embedding model ID."
                    />
                  )}
                </div>
              ) : (
                <ProviderModelPicker
                  label=""
                  value={activeModelName}
                  models={filteredModels}
                  onChange={(v) => {
                    if (kind === "chat" || kind === "all") setDefaultChatModel(v);
                    if (kind === "embedding" || kind === "all") setDefaultEmbeddingModel(v);
                    if (kind === "reranker" || kind === "all") setDefaultRerankerModel(v);
                  }}
                  kinds={kind === "all" ? ["chat", "embedding", "reranker"] : [kind]}
                  allowClear
                />
              )}
            </div>
          ) : null}
        </div>

        {/* Model Discovery Status */}
        {kind !== "web" ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between px-1">
              <span className="text-slate-600 dark:text-slate-300 font-mono text-[10px] font-black tracking-[0.2em] uppercase">
                Model Discovery
              </span>
              {waitingForToken ? (
                <span className="waiting-token-sweep rounded-full border border-amber-400/60 bg-amber-50 px-3 py-1 font-mono text-[9px] font-black tracking-widest text-amber-900 uppercase dark:border-amber-500/40 dark:bg-amber-950/60 dark:text-amber-300 shadow-2xs">
                  ⊙ Waiting for token
                </span>
              ) : waitingForCodex ? (
                <span className="rounded-full border border-amber-400/60 bg-amber-50 px-3 py-1 font-mono text-[9px] font-black tracking-widest text-amber-900 uppercase dark:border-amber-500/40 dark:bg-amber-950/60 dark:text-amber-300 shadow-2xs">
                  Connect account and choose model
                </span>
              ) : discoveringModels ? (
                <span className="rounded-full border border-teal-400/60 bg-teal-50 px-3 py-1 font-mono text-[9px] font-black tracking-widest text-teal-800 uppercase dark:border-teal-500/40 dark:bg-teal-950/60 dark:text-teal-300 shadow-2xs">
                  Discovery active
                </span>
              ) : previewModels.length > 0 ? (
                <span className="rounded-full border border-emerald-400/60 bg-emerald-50 px-3 py-1 font-mono text-[9px] font-black tracking-widest text-emerald-800 uppercase dark:border-emerald-500/40 dark:bg-emerald-950/60 dark:text-emerald-300 shadow-2xs">
                  {previewModels.length} models found
                </span>
              ) : null}
            </div>
            <div className="text-slate-700 dark:text-slate-300 rounded-2xl border border-slate-200 dark:border-white/10 bg-white/95 dark:bg-slate-900/60 p-4 text-xs font-medium shadow-2xs">
              {discoverError || (normalizedPreviewUrl && !isValidHttpUrl(normalizedPreviewUrl)) ? (
                <p className="text-red-400/80">
                  {discoverError || "Enter a valid http(s) runtime URL to preview models."}
                </p>
              ) : discoveringModels ? (
                <p>
                  {activeProviderType === "cline"
                    ? "Loading Cline's model catalog. This does not test chat access."
                    : "Inspecting endpoint and validating credentials..."}
                </p>
              ) : localRuntimeUnavailable ? (
                <p>Open AverQel Desktop to discover models from this local runtime.</p>
              ) : isCodexLogin ? (
                <p>
                  Models come from the signed-in ChatGPT account. DeepSpace uses the selected model
                  through the connected AverQel Desktop runtime.
                </p>
              ) : (
                <p>
                  {activeProviderType === "cline" ? (
                    "Cline's public model catalog loads automatically. Enter an API key to link the provider; model access is checked only by a chat request."
                  ) : (
                    <>
                      When you enter the token, supported {kind === "all" ? "provider" : kind}{" "}
                      models are fetched automatically for this provider.
                    </>
                  )}
                </p>
              )}
            </div>
          </div>
        ) : null}

        {/* Hugging Face Open-Source Embedding Hub & Local Downloader */}
        {isEmbeddingFamily && (
          <div className="space-y-4 pt-1">
            <div className="flex items-center justify-between px-1">
              <span className="text-slate-600 dark:text-slate-300 font-mono text-[10px] font-black tracking-[0.2em] uppercase">
                Open-Source Embedding Hub (~/.averqel)
              </span>
              <button
                type="button"
                onClick={() => setShowEmbeddingHub(!showEmbeddingHub)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-800 px-3 py-1 text-[11px] font-black tracking-wider text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-700 uppercase transition-all shadow-2xs"
              >
                <span>🤗</span>
                <span>{showEmbeddingHub ? "Hide Hub" : "Explore HuggingFace Models"}</span>
              </button>
            </div>

            {showEmbeddingHub && (
              <HuggingFaceEmbeddingHub
                activeModelName={defaultEmbeddingModel}
                onSelectModel={(modelName) => {
                  setDefaultEmbeddingModel(modelName);
                  setPreviewModels((prev) => {
                    if (prev.some((p) => p.model_name === modelName)) return prev;
                    return [
                      {
                        id: null,
                        provider_config_id: provider?.id || null,
                        model_name: modelName,
                        model_kind: "embedding",
                        display_name: modelName,
                        context_window: 512,
                        capabilities_json: {
                          supports_embeddings: true,
                          preinstalled: true,
                        },
                        is_available: true,
                        last_seen_at: new Date().toISOString(),
                      },
                      ...prev,
                    ];
                  });
                }}
              />
            )}
          </div>
        )}

        {/* Hugging Face Open-Source Reranker Hub & Local Downloader */}
        {isRerankerFamily && (
          <div className="space-y-4 pt-1">
            <div className="flex items-center justify-between px-1">
              <span className="text-slate-600 dark:text-slate-300 font-mono text-[10px] font-black tracking-[0.2em] uppercase">
                Open-Source Reranker Hub (~/.averqel)
              </span>
              <button
                type="button"
                onClick={() => setShowRerankerHub(!showRerankerHub)}
                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-800 px-3 py-1 text-[11px] font-black tracking-wider text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-700 uppercase transition-all shadow-2xs"
              >
                <span>🎯</span>
                <span>{showRerankerHub ? "Hide Hub" : "Explore Reranker Models"}</span>
              </button>
            </div>

            {showRerankerHub && (
              <HuggingFaceRerankerHub
                activeModelName={defaultRerankerModel}
                onSelectModel={(modelName) => {
                  setDefaultRerankerModel(modelName);
                  setPreviewModels((prev) => {
                    if (prev.some((p) => p.model_name === modelName)) return prev;
                    return [
                      {
                        id: null,
                        provider_config_id: provider?.id || null,
                        model_name: modelName,
                        model_kind: "reranker",
                        display_name: modelName,
                        context_window: 512,
                        capabilities_json: {
                          supports_reranking: true,
                          preinstalled: true,
                        },
                        is_available: true,
                        last_seen_at: new Date().toISOString(),
                      },
                      ...prev,
                    ];
                  });
                }}
              />
            )}
          </div>
        )}

        {/* 3 Status Cards */}
        <div className="grid gap-4 md:grid-cols-3">
          <StatusPanel
            label="Current Target"
            value={
              kind === "web"
                ? catalogEntry?.display_name || provider?.display_name || "Web Search"
                : activeModelName || "No model selected"
            }
            subtext={`${(catalogEntry?.is_local ?? provider?.is_local) ? "Managed" : "Hosted"} ${kind} runtime`}
          />
          <StatusPanel
            label="Runtime URL"
            value={isCodexLogin ? "AverQel Desktop bridge" : apiBaseUrl || "Not set"}
          />
          <StatusPanel
            label="Status"
            value={
              provider
                ? isHealthy
                  ? "Healthy"
                  : healthStatus === "degraded"
                    ? "Degraded"
                    : "Unknown"
                : "Not connected"
            }
            indicator={provider ? (isHealthy ? "success" : "warning") : undefined}
            subtext={
              !provider
                ? "No runtime check yet."
                : activeProviderType === "cline" && healthStatus === "degraded"
                  ? "Catalog reachable; chat access not verified."
                  : "Last runtime check."
            }
          />
        </div>
      </div>
      {/* end scrollable content */}

      {/* Sticky footer — always visible regardless of scroll position */}
      <div className="relative z-[2] mt-2 flex shrink-0 items-center justify-between border-t border-slate-200 dark:border-white/10 pt-5">
        <button
          onClick={() => provider && onTest?.(provider)}
          disabled={!provider}
          className="text-slate-700 hover:text-slate-950 dark:text-slate-300 dark:hover:text-white flex items-center gap-2 text-xs font-black tracking-widest uppercase transition-colors disabled:opacity-25"
        >
          <Activity size={14} />
          Test ping
        </button>

        {provider ? (
          <button
            onClick={() => onDisconnect?.(provider)}
            className="flex items-center gap-2 text-xs font-black tracking-widest text-rose-600 dark:text-rose-400 uppercase transition-colors hover:text-rose-700 dark:hover:text-rose-300"
          >
            <Link2Off size={14} />
            Terminate
          </button>
        ) : (
          <span className="text-slate-400 dark:text-slate-500 text-[10px] font-black tracking-[0.22em] uppercase">
            Configure and link
          </span>
        )}

        <button
          onClick={handleLocalSubmit}
          disabled={saving || waitingForToken || waitingForCodex || localRuntimeUnavailable}
          className="border-primary/35 relative z-[3] flex items-center gap-3 rounded-xl border bg-gradient-to-r from-emerald-600 to-teal-600 px-8 py-3.5 text-sm font-black !text-white shadow-lg shadow-emerald-950/20 hover:from-emerald-700 hover:to-teal-700 hover:shadow-xl transition-all hover:scale-[1.02] active:scale-[0.98] disabled:opacity-50 dark:from-emerald-500 dark:to-teal-500 dark:border-emerald-400/40 dark:!text-slate-950"
        >
          <Link2 size={16} />
          {saving ? "Saving..." : provider ? "Update Connectivity" : "Link Family"}
        </button>
      </div>

      {/* Disabled overlay */}
      {provider && !provider.enabled && (
        <div className="absolute inset-0 z-20 flex flex-col items-center justify-center rounded-2xl bg-slate-950/80 backdrop-blur-md">
          <p className="font-bold tracking-widest text-amber-400 uppercase">
            Connection Interrupted
          </p>
          <button
            onClick={() => onReconnect?.(provider)}
            className="bg-primary mt-6 flex items-center gap-2 rounded-full px-8 py-3 text-sm font-bold text-slate-950 shadow-xl transition-transform hover:scale-[1.05]"
          >
            <Link2 size={16} />
            Resume Connectivity
          </button>
        </div>
      )}
    </motion.div>
  );
}

function StatusPanel({
  label,
  value,
  subtext,
  indicator,
}: {
  label: string;
  value: string;
  subtext?: string;
  indicator?: "success" | "warning" | "error";
}) {
  return (
    <div className="flex flex-col justify-between space-y-3 rounded-2xl p-5 border border-slate-200 dark:border-white/10 bg-white/95 dark:bg-slate-900/60 shadow-2xs">
      <span className="text-slate-600 dark:text-slate-300 font-mono text-[10px] font-black tracking-widest uppercase">
        {label}
      </span>
      <div>
        <div className="flex items-center gap-2">
          {indicator && (
            <div
              className={`h-2 w-2 rounded-full ${
                indicator === "success"
                  ? "bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]"
                  : indicator === "warning"
                    ? "bg-amber-500 shadow-[0_0_8px_rgba(245,158,11,0.5)]"
                    : "bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.5)]"
              }`}
            />
          )}
          <p className="text-slate-900 dark:text-white text-[14px] font-black">{value || "None"}</p>
        </div>
        {subtext && <p className="text-slate-500 dark:text-slate-400 mt-1 text-[11px] font-semibold">{subtext}</p>}
      </div>
    </div>
  );
}
