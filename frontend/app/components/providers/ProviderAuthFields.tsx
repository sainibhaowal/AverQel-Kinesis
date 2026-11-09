"use client";

import { Globe, Info } from "lucide-react";
import React from "react";

import ProviderDropdown from "./ProviderDropdown";
import ProviderSecretField from "./ProviderSecretField";

interface ProviderAuthFieldsProps {
  providerType: string;
  catalogEntry?: {
    auth_modes: string[];
    is_local?: boolean;
  };
  authMode: string;
  onAuthModeChange: (mode: string) => void;
  apiBaseUrl: string;
  onApiBaseUrlChange: (url: string) => void;
  secretValue: string;
  onSecretValueChange: (value: string) => void;
  maskedSummary: string | null;
  disabled?: boolean;
}

export default function ProviderAuthFields({
  providerType,
  catalogEntry,
  authMode,
  onAuthModeChange,
  apiBaseUrl,
  onApiBaseUrlChange,
  secretValue,
  onSecretValueChange,
  maskedSummary,
  disabled = false,
}: ProviderAuthFieldsProps) {
  const authModes = catalogEntry?.auth_modes || ["api_key", "none"];
  // Only hide URL for fully built-in providers (auth_mode "none") — local runtimes like LM Studio still need their URL
  const requiresBaseUrl =
    authMode !== "none" || providerType === "searxng" || providerType === "custom";

  const authOptions = authModes.map((m) => ({
    value: m,
    label:
      m === "api_key" ? "API Key" : m === "none" ? "No Authentication" : m.replaceAll("_", " "),
  }));

  return (
    <div className="space-y-8">
      <div className="grid gap-6 md:grid-cols-2">
        <div className="space-y-1.5">
          <label className="text-slate-600 dark:text-slate-300 block px-0.5 font-mono text-[9px] tracking-[0.2em] uppercase">
            Authentication Protocol
          </label>
          <ProviderDropdown
            value={authMode}
            onChange={onAuthModeChange}
            options={authOptions}
            placeholder="Select Auth Mode"
            disabled={disabled}
            className="bg-surface-1 border-glass-border h-10 opacity-90"
          />
        </div>

        <div className="space-y-1.5">
          <label className="text-muted-foreground/60 block px-0.5 font-mono text-[9px] tracking-[0.2em] uppercase">
            {providerType === "searxng" ? "SearXNG Endpoint" : "API / Runtime URL"}
          </label>
          {requiresBaseUrl ? (
            <div className="group relative">
              <input
                type="text"
                value={apiBaseUrl}
                onChange={(e) => onApiBaseUrlChange(e.target.value)}
                disabled={disabled}
                placeholder="https://api.provider.ai/v1"
                className="h-10 w-full rounded-lg border border-slate-400 bg-white px-4 pr-10 text-xs text-slate-900 shadow-sm outline-none ring-1 ring-transparent transition-all placeholder:text-slate-500 focus:border-primary/70 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-500 dark:bg-[#131b15] dark:text-slate-100 dark:placeholder:text-slate-400"
              />
              <Globe className="pointer-events-none absolute top-[11px] right-4 h-3.5 w-3.5 text-slate-500 transition-colors group-focus-within:text-primary dark:text-slate-400" />
            </div>
          ) : (
            <div className="border-glass-border bg-muted/30 text-muted-foreground/50 flex h-10 items-center gap-3 rounded-xl border border-dashed px-4 text-[10px]">
              <Info size={13} className="text-primary/40" />
              Managed runtime; no URL needed.
            </div>
          )}
        </div>
      </div>

      {authMode === "api_key" && (
        <div className="space-y-1.5">
          <label className="text-slate-600 dark:text-slate-300 block px-0.5 font-mono text-[9px] tracking-[0.2em] uppercase">
            Security Token / Secret
          </label>
          <ProviderSecretField
            value={secretValue}
            onChange={onSecretValueChange}
            maskedSummary={maskedSummary}
            disabled={disabled}
            className="h-10"
          />
        </div>
      )}
    </div>
  );
}
