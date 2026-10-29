"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Check,
  Loader2,
  ShieldCheck,
  TriangleAlert,
  Sparkles,
  SlidersHorizontal,
  ChevronDown,
  Brain,
  Bot,
  Code,
  FileText,
  Layers,
  SquareTerminal,
  X as XIcon,
  Sparkle,
  Puzzle,
  Trash2,
} from "lucide-react";
import { fetchWithAuth } from "@/lib/api";
import DashboardSectionHeader from "@/app/components/ui/DashboardSectionHeader";
import RoundedSelect from "@/app/components/ui/RoundedSelect";
import PluginInventoryTab from "./_components/PluginInventoryTab";
import { listPluginCatalogue, type PluginCatalogue } from "./_lib/plugin-api";
import {
  broadcastDefaultPresetChanged,
  subscribeDefaultPresetChanged,
} from "@/app/dashboard/deepspace/_lib/agent-mode-api";

type SandboxStatus = {
  configured?: string;
  selected?: string;
  available?: boolean;
  network?: boolean;
  platform?: string;
  enforcement?: string;
};

type RuntimeStatus = {
  sandbox?: SandboxStatus;
  model_selection_mode?: "auto" | "manual";
  model_auto_choice_enabled?: boolean;
  agent_team_enabled?: boolean;
  ssh_runtime_enabled?: boolean;
  approval_mode?: string;
  permission_mode?: string;
  shell_enabled?: boolean;
  max_concurrent_tools?: number;
  spill_threshold_chars?: number;
  ptc_enabled?: boolean;
  plan_mode_enabled?: boolean;
  acp_enabled?: boolean;
};

type Preset = {
  id: string;
  name: string;
  description: string;
  ptc_enabled: boolean;
  plan_mode_by_default: boolean;
  editable: boolean;
  tools: string[];
  sandbox_mode: string | null;
  reasoning_effort: string | null;
  subagent_max_depth: number | null;
  spill_threshold_chars: number | null;
  settings?: PresetSettings;
};

type PresetsResponse = {
  presets: Preset[];
  default: string;
};

type UserDefaultPresetResponse = {
  preset: string;
  recorded: string | null;
  settings?: PresetSettings | null;
  custom_settings?: PresetSettings | null;
  deployment_default: string;
};

type EnablementResponse = {
  enablement: { preset_id: string; enabled: boolean }[];
};

type PresetSettings = {
  name?: string;
  description?: string;
  tools?: string[] | null;
  tool_allowlist?: string[] | null;
  sandbox_mode?: string | null;
  reasoning_effort?: string | null;
  subagent_max_depth?: number | null;
  spill_threshold_chars?: number | null;
  ptc_enabled?: boolean;
  plan_mode_by_default?: boolean;
};

const PRESET_DETAILS: Record<string, { included: string; when: string }> = {
  standard: {
    included:
      "Every tool allowed by the deployment policy, with delegation available and programmatic tool calling off.",
    when: "Use it for normal coding, file, research, and workspace tasks.",
  },
  ptc: {
    included:
      "The native tool catalogue plus programmatic tool calling, with one level of delegation and medium reasoning effort.",
    when: "Use it when several independent tool calls should be coordinated in one generated program.",
  },
  minimal: {
    included: "Read-only tools, no shell, no writes, and no delegation.",
    when: "Use it for quick questions, inspection, and comparison work where changing state is not needed.",
  },
  creator: {
    included:
      "Notes, Library files, artifacts, memory, and their supporting read tools, without shell or delegation.",
    when: "Use it when the result should be written into AverQel content rather than into an operating-system workspace.",
  },
};

type SelectOption = { value: string | number | null; label: string; description?: string };

const SANDBOX_MODES: SelectOption[] = [
  { value: null, label: "Deployment default", description: "Use the deployment's sandbox policy" },
  {
    value: "read-only",
    label: "Read-only",
    description: "Filesystem reads only; no writes, no network, no shell",
  },
  {
    value: "workspace-write",
    label: "Workspace write",
    description: "Read and write workspace files; no network, no shell",
  },
  {
    value: "danger-full-access",
    label: "Full access",
    description: "No restrictions; shell, network, filesystem writes allowed",
  },
];

const REASONING_EFFORTS: SelectOption[] = [
  { value: null, label: "Default", description: "Use the model's default reasoning" },
  { value: "low", label: "Low", description: "Light reasoning; balanced speed/quality" },
  { value: "medium", label: "Medium", description: "Standard reasoning effort" },
  { value: "high", label: "High", description: "Deep reasoning; more tokens for complex problems" },
];

const DELEGATION_DEPTHS: SelectOption[] = [
  { value: null, label: "Deployment default", description: "Use the deployment's max depth" },
  { value: 0, label: "No delegation", description: "Cannot spawn subagents" },
  { value: 1, label: "Depth 1", description: "Can spawn direct children only" },
  { value: 2, label: "Depth 2", description: "Children can spawn their own children" },
  { value: 3, label: "Depth 3", description: "Three levels of delegation" },
  { value: 4, label: "Depth 4", description: "Four levels of delegation" },
  { value: 5, label: "Depth 5", description: "Five levels of delegation" },
];

/** The icon a preset's card shows. */
function presetIcon(presetId: string) {
  if (presetId === "standard") return <FileText size={18} className="text-primary" />;
  if (presetId === "ptc") return <Code size={18} className="text-primary" />;
  if (presetId === "minimal") return <SquareTerminal size={18} className="text-primary" />;
  if (presetId === "creator") return <Layers size={18} className="text-primary" />;
  return <Sparkles size={18} className="text-primary" />;
}

function ModeFlag({ label, enabled }: { label: string; enabled: boolean }) {
  return (
    <span
      className={`inline-flex items-center rounded-md border px-2 py-1 text-[10px] font-semibold tracking-[0.08em] uppercase ${
        enabled
          ? "border-primary/30 bg-primary/10 text-primary"
          : "border-glass-border bg-surface-2/70 text-muted-foreground"
      }`}
    >
      {label} {enabled ? "on" : "off"}
    </span>
  );
}

function Row({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string;
  tone?: "neutral" | "good" | "warn";
}) {
  return (
    <div className="deepspace-info-row border-border/30 flex items-start justify-between gap-4 border-b py-3 last:border-b-0">
      <span className="text-body text-sm font-medium">{label}</span>
      <span
        className={`text-right text-sm font-semibold ${
          tone === "good" ? "text-primary" : tone === "warn" ? "text-warning" : "text-foreground"
        }`}
      >
        {value}
      </span>
    </div>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
  disabled = false,
  description,
}: {
  label: string;
  value: string | number | null;
  onChange: (v: string | number | null) => void;
  options: SelectOption[];
  disabled?: boolean;
  description?: string;
}) {
  const roundedOptions = options.map((opt) => ({
    value: opt.value === null ? "" : String(opt.value),
    label: opt.label,
  }));
  if (!roundedOptions.some((opt) => opt.value === "")) {
    roundedOptions.unshift({ value: "", label: "— Select —" });
  }

  return (
    <div className="deepspace-setting-row border-border/30 flex flex-col gap-1.5 border-b py-4 last:border-b-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <label className="text-body text-sm font-medium">{label}</label>
        {description && <span className="text-muted-foreground text-xs">{description}</span>}
      </div>
      <RoundedSelect
        label={label}
        value={value === null ? "" : String(value)}
        onChange={(next) => {
          if (next === "") {
            onChange(null);
            return;
          }
          onChange(isNaN(Number(next)) ? next : Number(next));
        }}
        options={roundedOptions}
        disabled={disabled}
        triggerClassName="border-border/50 bg-background text-foreground hover:bg-surface-1 focus-visible:outline-primary/70 w-full rounded-xl border px-3 py-2.5 text-sm font-medium focus-visible:outline-offset-2"
      />
    </div>
  );
}

function Toggle({
  label,
  description,
  checked,
  onChange,
  disabled = false,
  compact = false,
}: {
  label: string;
  description?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  compact?: boolean;
}) {
  return (
    <div
      className={
        compact
          ? "flex items-center"
          : "deepspace-setting-row border-border/30 flex items-center justify-between gap-4 border-b py-4 last:border-b-0"
      }
    >
      {!compact ? (
        <div className="min-w-0 flex-1">
          {label && <label className="text-body text-sm font-medium">{label}</label>}
          {description && <p className="text-muted-foreground mt-0.5 text-xs">{description}</p>}
        </div>
      ) : null}
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label || undefined}
        onClick={() => !disabled && onChange(!checked)}
        disabled={disabled}
        className={`relative h-6 w-12 shrink-0 rounded-full border transition-colors ${
          checked
            ? "border-primary/40 bg-primary shadow-[0_0_14px_color-mix(in_srgb,hsl(var(--primary))_24%,transparent)]"
            : "border-glass-border bg-surface-2"
        } ${disabled ? "cursor-not-allowed opacity-40" : "hover:border-primary/40 hover:opacity-90"}`}
      >
        <span
          className={`absolute top-0.5 bottom-0.5 w-5 rounded-full bg-white shadow-sm transition-transform ${
            checked ? "translate-x-6" : "translate-x-0.5"
          }`}
          aria-hidden="true"
        />
      </button>
    </div>
  );
}

function AgentModeToggle({
  label,
  checked,
  onChange,
  disabled = false,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={`${label}: ${checked ? "ON" : "OFF"}`}
      data-checked={checked ? "true" : "false"}
      data-disabled={disabled ? "true" : "false"}
      disabled={disabled}
      onClick={() => !disabled && onChange(!checked)}
      className={`deepspace-agent-mode-toggle ${checked ? "is-on" : "is-off"}`}
    >
      <span className="deepspace-agent-mode-toggle__state" aria-hidden="true">
        {checked ? "ON" : "OFF"}
      </span>
      <motion.span
        className="deepspace-agent-mode-toggle__thumb"
        aria-hidden="true"
        initial={false}
        animate={{ x: checked ? 28 : 0 }}
        transition={{ type: "spring", stiffness: 520, damping: 30, mass: 0.7 }}
      >
        {checked ? <Check size={12} strokeWidth={3} /> : null}
      </motion.span>
    </button>
  );
}

function NumberInput({
  label,
  description,
  value,
  onChange,
  min,
  max,
  disabled = false,
}: {
  label: string;
  description?: string;
  value: number | null;
  onChange: (v: number | null) => void;
  min: number;
  max: number;
  disabled?: boolean;
}) {
  return (
    <div className="deepspace-setting-row border-border/30 flex flex-col gap-1.5 border-b py-4 last:border-b-0">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <label className="text-body text-sm font-medium">{label}</label>
        {description && <span className="text-muted-foreground text-xs">{description}</span>}
      </div>
      <div className="flex items-center gap-2">
        <input
          type="number"
          value={value === null ? "" : String(value)}
          placeholder="Default"
          aria-label={`${label}${value === null ? " (deployment default)" : ""}`}
          onChange={(e) =>
            onChange(
              e.target.value === "" ? null : Math.min(max, Math.max(min, Number(e.target.value))),
            )
          }
          min={min}
          max={max}
          disabled={disabled}
          className="border-border/50 bg-background text-foreground placeholder:text-muted-foreground/70 focus:border-primary/50 focus:ring-primary/20 w-32 rounded-lg border px-3 py-2.5 text-sm font-medium transition-colors focus:ring-2 focus:outline-none disabled:cursor-not-allowed disabled:opacity-40"
        />
        {value === null ? (
          <span className="text-muted-foreground text-xs">Using deployment default</span>
        ) : null}
      </div>
    </div>
  );
}

function CollapsibleSection({
  title,
  icon,
  children,
  defaultOpen = true,
  className,
}: {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  defaultOpen?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div
      className={`deepspace-collapsible border-glass-border bg-surface-1/70 overflow-hidden rounded-xl border ${className ?? ""}`}
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="hover:bg-primary/10 hover:text-primary flex w-full items-center justify-between gap-3 p-4 text-left transition-colors"
        aria-expanded={open}
      >
        <div className="text-foreground flex min-w-0 items-center gap-2 text-sm font-semibold">
          <span className="text-primary">{icon}</span>
          <span className="truncate">{title}</span>
        </div>
        <motion.span
          animate={{ rotate: open ? 0 : -90 }}
          transition={{ duration: 0.15, ease: [0.22, 1, 0.36, 1] }}
          className="shrink-0"
        >
          <ChevronDown size={16} className="text-muted-foreground" />
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="content"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.16, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <div className="px-4 pt-0 pb-4">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function ToolAllowlistEditor({
  catalogue,
  value,
  onChange,
}: {
  catalogue: PluginCatalogue | null;
  value: string[] | null | undefined;
  onChange: (tools: string[] | undefined) => void;
}) {
  const tools = useMemo(() => {
    const records = new Map<
      string,
      { name: string; description: string; mode: string; available: boolean }
    >();
    for (const plugin of catalogue?.globalPlugins ?? []) {
      for (const tool of plugin.tools) {
        const existing = records.get(tool.name);
        records.set(tool.name, {
          name: tool.name,
          description: tool.description,
          mode: tool.mode,
          available:
            (existing?.available ?? false) ||
            (plugin.deployment_enabled !== false && plugin.available !== false),
        });
      }
    }
    return [...records.values()].sort((left, right) => left.name.localeCompare(right.name));
  }, [catalogue]);
  const [query, setQuery] = useState("");
  const allNames = useMemo(
    () => tools.filter((tool) => tool.available).map((tool) => tool.name),
    [tools],
  );
  const allSelected = value === undefined || value === null;
  const selected = useMemo(() => new Set(value ?? allNames), [allNames, value]);
  const selectedAvailableCount = allSelected
    ? allNames.length
    : allNames.reduce((count, name) => count + (selected.has(name) ? 1 : 0), 0);
  const visibleTools = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return tools;
    return tools.filter(
      (tool) =>
        tool.name.toLocaleLowerCase().includes(normalized) ||
        tool.description.toLocaleLowerCase().includes(normalized),
    );
  }, [query, tools]);

  if (catalogue === null) {
    return <p className="text-muted-foreground py-3 text-xs">Loading the native tool catalogue…</p>;
  }
  if (tools.length === 0) {
    return (
      <p className="text-muted-foreground py-3 text-xs">
        No DeepSpace tools are available from the runtime catalogue.
      </p>
    );
  }

  const updateTool = (name: string, checked: boolean) => {
    const next = allSelected ? new Set(allNames) : new Set(selected);
    if (checked) next.add(name);
    else next.delete(name);
    if (next.size === allNames.length) {
      onChange(undefined);
      return;
    }
    onChange([...next].sort());
  };

  return (
    <div className="space-y-3 pt-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-foreground text-sm font-medium">Native DeepSpace tools</p>
          <p className="text-muted-foreground/70 mt-0.5 text-xs">
            Select the real registered tools this custom mode may discover and call.
          </p>
        </div>
        <span
          className="deepspace-tool-selection-count text-primary inline-flex items-center rounded-full px-2.5 py-1 text-xs font-semibold"
          aria-live="polite"
        >
          {allSelected
            ? `All ${allNames.length} available tools selected`
            : `${selectedAvailableCount} of ${allNames.length} available tools selected`}
        </span>
      </div>
      <label className="border-border/50 bg-background/40 flex items-center gap-2 rounded-lg border px-3 py-2">
        <span className="sr-only">Search native tools</span>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.currentTarget.value)}
          placeholder="Search native tools"
          className="text-foreground placeholder:text-muted-foreground/60 min-w-0 flex-1 bg-transparent text-sm outline-none"
        />
      </label>
      <label
        className={`deepspace-tool-option deepspace-tool-option--all border-border/30 bg-background/30 flex items-center gap-3 rounded-lg border px-3 py-2.5 text-sm ${allSelected ? "is-selected" : ""}`}
      >
        <input
          type="checkbox"
          checked={allSelected}
          onChange={(event) => onChange(event.currentTarget.checked ? undefined : [])}
          className="peer sr-only"
          aria-label="Use all available tools"
        />
        <span
          aria-hidden="true"
          className={`deepspace-tool-checkbox ${allSelected ? "is-checked" : ""}`}
        >
          {allSelected ? <Check size={12} strokeWidth={3} /> : null}
        </span>
        <span className="min-w-0 flex-1">
          <span className="text-foreground block font-medium">All available tools</span>
          <span className="text-muted-foreground block text-xs">
            Uses the same native handlers, policy, and permissions as Standard mode.
          </span>
        </span>
      </label>
      <div className="border-border/30 max-h-72 divide-y overflow-y-auto rounded-lg border">
        {visibleTools.map((tool) => (
          <label
            key={tool.name}
            data-selected={tool.available && selected.has(tool.name) ? "true" : "false"}
            data-available={tool.available ? "true" : "false"}
            className={`deepspace-tool-option flex items-start gap-3 px-3 py-2.5 transition-colors ${tool.available ? "hover:bg-primary/10 cursor-pointer" : "cursor-not-allowed opacity-60"}`}
          >
            <input
              type="checkbox"
              checked={tool.available && selected.has(tool.name)}
              onChange={(event) => updateTool(tool.name, event.currentTarget.checked)}
              disabled={!tool.available}
              className="peer sr-only"
              aria-label={`Use ${tool.name}`}
            />
            <span
              aria-hidden="true"
              className={`deepspace-tool-checkbox mt-0.5 ${tool.available && selected.has(tool.name) ? "is-checked" : ""} ${!tool.available ? "is-disabled" : ""}`}
            >
              {tool.available && selected.has(tool.name) ? (
                <Check size={12} strokeWidth={3} />
              ) : null}
            </span>
            <span className="min-w-0 flex-1">
              <span className="text-foreground block text-xs font-medium">{tool.name}</span>
              <span className="text-muted-foreground block truncate text-[11px]">
                {tool.mode} · {tool.description || "Registered DeepSpace tool"}
                {!tool.available ? " · Disabled by deployment" : ""}
              </span>
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

type Tab = "runtime" | "presets" | "agent-modes" | "plugins";

const TABS: { id: Tab; label: string; icon: typeof ShieldCheck }[] = [
  { id: "runtime", label: "Runtime", icon: ShieldCheck },
  { id: "presets", label: "Presets", icon: Sparkles },
  { id: "agent-modes", label: "Agent Modes", icon: Bot },
  { id: "plugins", label: "Plugins", icon: Puzzle },
];

export default function DeepSpaceSettingsPage() {
  const [activeTab, setActiveTab] = useState<Tab>("runtime");

  return (
    <div className="deepspace-settings-scope w-full space-y-6">
      <DashboardSectionHeader
        title="DeepSpace"
        subtitle="Configure the agent runtime, presets, and agent modes"
        icon={ShieldCheck}
        accentClassName="bg-emerald-500 text-emerald-500"
        accentGlowClassName="shadow-[0_0_24px_-4px_rgba(16,185,129,0.35)]"
        backHref="/dashboard/settings"
        backLabel="Back To Settings"
      />

      <div className="border-border flex flex-wrap gap-2 border-b" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 rounded-t-xl px-4 py-2.5 text-sm font-semibold tracking-[0.05em] uppercase transition-colors ${
              activeTab === tab.id
                ? "border-primary bg-primary/10 text-foreground border-b-2"
                : "text-muted-foreground hover:bg-primary/10 hover:text-primary"
            }`}
          >
            <tab.icon size={14} className="shrink-0" />
            {tab.label}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={activeTab}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.2 }}
        >
          {activeTab === "runtime" && <RuntimeTab />}
          {activeTab === "presets" && <PresetsTab />}
          {activeTab === "agent-modes" && <AgentModesTab />}
          {activeTab === "plugins" && <PluginInventoryTab />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

function RuntimeTab() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = (await fetchWithAuth("/deepspace/sandbox/runtime")) as Response;
        if (cancelled) return;
        if (!response.ok) {
          setError("The DeepSpace runtime settings could not be loaded.");
          return;
        }
        setStatus((await response.json()) as RuntimeStatus);
        setError(null);
      } catch {
        if (!cancelled) setError("The DeepSpace runtime settings could not be loaded.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const loading = status === null && error === null;
  const sandbox = status?.sandbox;
  const cageAvailable = Boolean(sandbox?.available);
  const cageTone = cageAvailable ? "good" : "warn";

  const runtimeSections = [
    {
      key: "sandbox",
      title: "Sandbox & Isolation",
      icon: ShieldCheck,
      description: "Operating-system cage, network, and backend",
    },
    {
      key: "permissions",
      title: "Permissions & Approvals",
      icon: ShieldCheck,
      description: "Permission mode, approval mode, shell access",
    },
    {
      key: "tools",
      title: "Tools & Performance",
      icon: SlidersHorizontal,
      description: "Parallel budget, spill threshold, PTC, Plan mode",
    },
    {
      key: "model",
      title: "Model Selection",
      icon: Brain,
      description: "Auto vs manual, teammate models, auto-choice",
    },
    {
      key: "features",
      title: "Advanced Features",
      icon: Sparkles,
      description: "ACP bridge, agent teams, SSH runtime",
    },
  ];

  return (
    <div className="space-y-4">
      {runtimeSections.map((section) => (
        <CollapsibleSection
          key={section.key}
          title={section.title}
          icon={<section.icon size={14} />}
        >
          <p className="text-muted-foreground mb-4 text-xs">{section.description}</p>
          <div className="divide-border/30 divide-y">
            {section.key === "sandbox" && status && (
              <>
                <Row
                  label="Sandbox backend"
                  value={`${sandbox?.selected ?? "none"} (configured: ${sandbox?.configured ?? "auto"})`}
                  tone={cageTone}
                />
                <Row
                  label="Operating-system cage"
                  value={cageAvailable ? "Active" : "Not available on this host"}
                  tone={cageTone}
                />
                <Row
                  label="Network inside the cage"
                  value={sandbox?.network ? "Allowed" : "Blocked"}
                />
                <Row label="Platform" value={sandbox?.platform ?? "linux"} />
                <Row label="Enforcement" value={sandbox?.enforcement ?? "full"} />
              </>
            )}
            {section.key === "permissions" && status && (
              <>
                <Row label="Permission mode" value={status.permission_mode ?? "workspace-write"} />
                <Row label="Approval mode" value={status.approval_mode ?? "never"} />
                <Row
                  label="Local shell tools"
                  value={status.shell_enabled ? "Enabled" : "Disabled"}
                  tone={status.shell_enabled ? "good" : "warn"}
                />
              </>
            )}
            {section.key === "tools" && status && (
              <>
                <Row
                  label="Parallel tool budget"
                  value={`${status.max_concurrent_tools ?? 8} at a time`}
                />
                <Row
                  label="Spill threshold"
                  value={`${status.spill_threshold_chars ?? 24000} characters`}
                />
                <Row
                  label="Programmatic tool calling (PTC)"
                  value={status.ptc_enabled ? "Enabled" : "Disabled"}
                />
                <Row label="Plan mode" value={status.plan_mode_enabled ? "Enabled" : "Disabled"} />
              </>
            )}
            {section.key === "model" && status && (
              <>
                <Row
                  label="Model selection"
                  value={
                    status.model_selection_mode === "auto"
                      ? "DeepSpace chooses the model"
                      : "The user chooses the model"
                  }
                />
                <Row
                  label="Auto model choice"
                  value={status.model_auto_choice_enabled ? "Enabled" : "Disabled"}
                />
                <Row
                  label="Named teammates"
                  value={status.agent_team_enabled ? "Enabled" : "Disabled"}
                />
              </>
            )}
            {section.key === "features" && status && (
              <>
                <Row
                  label="Agent Client Protocol bridge"
                  value={status.acp_enabled ? "Enabled" : "Disabled"}
                />
                <Row
                  label="Agent teams"
                  value={status.agent_team_enabled ? "Enabled" : "Disabled"}
                />
                <Row
                  label="Remote runtime (SSH)"
                  value={status.ssh_runtime_enabled ? "Enabled" : "Disabled"}
                />
              </>
            )}
          </div>
        </CollapsibleSection>
      ))}

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 size={28} className="text-muted-foreground animate-spin" />
        </div>
      ) : null}

      {error ? (
        <div
          className="border-destructive/30 bg-destructive/10 text-destructive/90 rounded-xl border p-4 text-sm"
          role="alert"
        >
          <TriangleAlert size={14} className="mr-2" />
          {error}
        </div>
      ) : null}

      {!cageAvailable && !loading && !error ? (
        <div className="border-warning/35 bg-warning/10 text-warning-foreground flex items-start gap-3 rounded-xl border p-4 text-sm leading-5">
          <TriangleAlert size={14} className="mt-0.5 shrink-0" />
          <div>
            <p className="font-medium">No operating-system cage available</p>
            <p className="text-warning-foreground/85 mt-1">
              The local shell tools run with inspection only. Install bubblewrap on Linux, or run on
              macOS, to confine them.
            </p>
          </div>
        </div>
      ) : null}

      {cageAvailable ? (
        <div className="border-success/35 bg-success/10 text-success-foreground flex items-start gap-3 rounded-xl border p-4 text-sm leading-5">
          <Check size={14} className="mt-0.5 shrink-0" />
          <div>
            <p className="font-medium">Operating-system cage active</p>
            <p className="text-success-foreground/85 mt-1">
              The local shell tools run inside the{" "}
              <code className="bg-success/15 text-success-foreground rounded px-1 py-0.5">
                {sandbox?.selected}
              </code>{" "}
              cage, so a command cannot reach the rest of the host.
            </p>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function PresetsTab() {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [defaultPreset, setDefaultPreset] = useState<string>("standard");
  const [deploymentDefault, setDeploymentDefault] = useState<string>("standard");
  const [editingPresetId, setEditingPresetId] = useState<string | null>(null);
  const [customSettings, setCustomSettings] = useState<PresetSettings>({});
  const [defaultCustomSettings, setDefaultCustomSettings] = useState<PresetSettings>({});
  const [detailsPresetId, setDetailsPresetId] = useState<string | null>(null);
  const [detailsTab, setDetailsTab] = useState<"details" | "how-to-use">("details");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [pluginCatalogue, setPluginCatalogue] = useState<PluginCatalogue | null>(null);
  const [creatingCustom, setCreatingCustom] = useState(false);
  const [newCustomName, setNewCustomName] = useState("My custom preset");
  const [newCustomDescription, setNewCustomDescription] = useState(
    "A user-configured DeepSpace preset.",
  );

  const load = useCallback(async () => {
    try {
      const [presetsRes, defaultRes] = await Promise.all([
        fetchWithAuth("/deepspace/chats/presets"),
        fetchWithAuth("/deepspace/chats/presets/default"),
      ]);
      if (!presetsRes.ok) throw new Error("presets");
      const presetsData = (await presetsRes.json()) as PresetsResponse;
      setPresets(presetsData.presets);
      if (defaultRes.ok) {
        const defaultData = (await defaultRes.json()) as UserDefaultPresetResponse;
        setDefaultPreset(defaultData.preset);
        setDefaultCustomSettings(defaultData.custom_settings ?? defaultData.settings ?? {});
        setDeploymentDefault(defaultData.deployment_default);
      } else {
        setDefaultPreset(presetsData.default);
        setDeploymentDefault(presetsData.default);
      }
      setError(null);
    } catch {
      setError("The agent presets could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(handle);
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    void listPluginCatalogue().then(
      (catalogue) => {
        if (!cancelled) setPluginCatalogue(catalogue);
      },
      () => {
        // The preset editor remains usable when only the optional inventory
        // route is unavailable; its native-tool section reports the absence.
        if (!cancelled) setPluginCatalogue(null);
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const saveUserDefault = useCallback(async (presetId: string, settings?: PresetSettings) => {
    const response = (await fetchWithAuth("/deepspace/chats/presets/default", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings ? { preset: presetId, settings } : { preset: presetId }),
    })) as Response;
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      const message =
        body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
          ? body.detail
          : "The preset could not be saved.";
      throw new Error(message);
    }
    const data = (await response.json()) as UserDefaultPresetResponse;
    setDefaultPreset(data.preset);
    setDefaultCustomSettings(data.custom_settings ?? data.settings ?? {});
    setDeploymentDefault(data.deployment_default);
    broadcastDefaultPresetChanged(data.preset, data.deployment_default);
  }, []);

  useEffect(() => {
    return subscribeDefaultPresetChanged(({ preset, deploymentDefault: depDef }) => {
      setDefaultPreset(preset);
      if (depDef) setDeploymentDefault(depDef);
    });
  }, []);

  const createCustomPreset = useCallback(async () => {
    const response = (await fetchWithAuth("/deepspace/chats/presets/custom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: newCustomName.trim(),
        description: newCustomDescription.trim(),
        settings: { ptc_enabled: false, plan_mode_by_default: false },
      }),
    })) as Response;
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      const message =
        body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
          ? body.detail
          : "The custom preset could not be created.";
      throw new Error(message);
    }
    const data = (await response.json()) as { preset: Preset };
    await load();
    setCreatingCustom(false);
    setEditingPresetId(data.preset.id);
    setCustomSettings(data.preset.settings ?? {});
    setNotice(`${data.preset.name} was created. Configure its tools and save it.`);
  }, [load, newCustomDescription, newCustomName]);

  const updateCustomPreset = useCallback(
    async (presetId: string, settingsToSave: PresetSettings) => {
      const response = (await fetchWithAuth(
        `/deepspace/chats/presets/custom/${encodeURIComponent(presetId)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ settings: settingsToSave }),
        },
      )) as Response;
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        const message =
          body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
            ? body.detail
            : "The custom preset could not be saved.";
        throw new Error(message);
      }
    },
    [],
  );

  const deleteCustomPreset = useCallback(
    async (presetId: string) => {
      const response = (await fetchWithAuth(
        `/deepspace/chats/presets/custom/${encodeURIComponent(presetId)}`,
        { method: "DELETE" },
      )) as Response;
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        const message =
          body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
            ? body.detail
            : "The custom preset could not be deleted.";
        throw new Error(message);
      }
      await load();
      setEditingPresetId(null);
      setNotice("The custom preset was deleted.");
    },
    [load],
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 size={28} className="text-muted-foreground animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div
        className="border-destructive/30 bg-destructive/10 text-destructive/90 rounded-xl border p-4 text-sm"
        role="alert"
      >
        <TriangleAlert size={14} className="mr-2" />
        {error}
      </div>
    );
  }

  const preset = presets.find((p) => p.id === editingPresetId);

  return (
    <div className="space-y-4">
      <div className="settings-card border-border/30 bg-card/50 rounded-2xl border p-6">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h2 className="text-foreground text-sm font-bold tracking-tight">Agent presets</h2>
            <button
              type="button"
              onClick={() => setCreatingCustom((current) => !current)}
              className="border-primary/30 bg-primary/10 text-primary hover:bg-primary/20 rounded-lg border px-3 py-1.5 text-xs font-semibold tracking-[0.08em] uppercase transition-colors"
            >
              New custom preset
            </button>
          </div>
          <span className="text-muted-foreground text-xs">
            New conversations start in{" "}
            <code className="border-primary/25 bg-primary/10 text-primary rounded border px-2 py-0.5">
              {presets.find((p) => p.id === defaultPreset)?.name ?? defaultPreset}
            </code>
          </span>
        </div>
        <p className="text-muted-foreground mb-6 text-xs">
          Customize how the agent works, then save the preset as the default for new conversations.
          Custom settings override the built-in values.
        </p>
        {creatingCustom ? (
          <div className="border-primary/25 bg-primary/5 mb-4 rounded-xl border p-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-body text-sm font-medium">
                Name
                <input
                  value={newCustomName}
                  onChange={(event) => setNewCustomName(event.currentTarget.value)}
                  className="border-border/50 bg-background text-foreground focus:border-primary/50 mt-1.5 w-full rounded-lg border px-3 py-2.5 text-sm outline-none"
                  placeholder="Research safe mode"
                />
              </label>
              <label className="text-body text-sm font-medium">
                Description
                <input
                  value={newCustomDescription}
                  onChange={(event) => setNewCustomDescription(event.currentTarget.value)}
                  className="border-border/50 bg-background text-foreground focus:border-primary/50 mt-1.5 w-full rounded-lg border px-3 py-2.5 text-sm outline-none"
                  placeholder="What this preset is for"
                />
              </label>
            </div>
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setCreatingCustom(false)}
                className="border-border/30 text-muted-foreground hover:border-primary/30 hover:bg-primary/10 rounded-lg border px-3 py-1.5 text-xs font-semibold uppercase"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={!newCustomName.trim()}
                onClick={async () => {
                  try {
                    await createCustomPreset();
                  } catch (cause) {
                    setNotice(
                      cause instanceof Error
                        ? cause.message
                        : "The custom preset could not be created.",
                    );
                  }
                }}
                className="bg-primary text-primary-foreground rounded-lg px-3 py-1.5 text-xs font-semibold uppercase disabled:cursor-not-allowed disabled:opacity-50"
              >
                Create preset
              </button>
            </div>
          </div>
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {presets.map((p) => (
            <PresetCard
              key={p.id}
              preset={p}
              settingsOverride={
                p.id === "custom" ? (p.settings ?? defaultCustomSettings) : undefined
              }
              isDefault={defaultPreset === p.id}
              isDeploymentDefault={deploymentDefault === p.id}
              isEditing={editingPresetId === p.id}
              onEdit={() => {
                if (!p.editable) return;
                setEditingPresetId(editingPresetId === p.id ? null : p.id);
                setDetailsPresetId(null);
                setCustomSettings(p.settings ?? (p.id === "custom" ? defaultCustomSettings : {}));
                setNotice(null);
              }}
              onDetails={(tab) => {
                setDetailsPresetId(p.id);
                setDetailsTab(tab);
                setEditingPresetId(null);
                setNotice(null);
              }}
              onSetDefault={async () => {
                try {
                  await saveUserDefault(p.id);
                  setNotice(`New conversations now start in ${p.name}.`);
                } catch (cause) {
                  setNotice(
                    cause instanceof Error ? cause.message : "The preset could not be saved.",
                  );
                }
              }}
              onDelete={
                p.id.startsWith("custom_")
                  ? async () => {
                      try {
                        await deleteCustomPreset(p.id);
                      } catch (cause) {
                        setNotice(
                          cause instanceof Error
                            ? cause.message
                            : "The custom preset could not be deleted.",
                        );
                      }
                    }
                  : undefined
              }
            />
          ))}
        </div>
      </div>

      {detailsPresetId && PRESET_DETAILS[detailsPresetId] ? (
        <div className="settings-card border-border/30 bg-card/50 rounded-2xl border p-6">
          <div className="mb-4 flex items-center justify-between gap-3">
            <h2 className="text-foreground text-sm font-bold tracking-tight">
              {presets.find((p) => p.id === detailsPresetId)?.name ?? detailsPresetId}
            </h2>
            <button
              type="button"
              onClick={() => setDetailsPresetId(null)}
              className="border-border/30 text-muted-foreground hover:border-primary/30 hover:bg-primary/10 hover:text-primary rounded-lg border px-3 py-1.5 text-xs font-semibold tracking-[0.12em] uppercase transition-colors"
            >
              <XIcon size={12} className="mr-1" /> Close
            </button>
          </div>
          <div className="bg-surface-2/50 mb-5 flex gap-1 rounded-xl p-1" role="tablist">
            {(["details", "how-to-use"] as const).map((tab) => (
              <button
                key={tab}
                type="button"
                role="tab"
                aria-selected={detailsTab === tab}
                onClick={() => setDetailsTab(tab)}
                className={`flex-1 rounded-lg px-3 py-2 text-xs font-semibold transition-colors ${
                  detailsTab === tab
                    ? "border-primary/35 bg-surface-0 text-foreground border shadow-sm"
                    : "text-muted-foreground hover:text-primary"
                }`}
              >
                {tab === "details" ? "Details" : "How to use"}
              </button>
            ))}
          </div>
          <h3 className="text-foreground mb-2 text-sm font-semibold">
            {detailsTab === "details" ? "What is included" : "When to choose it"}
          </h3>
          <p className="text-muted-foreground text-sm leading-6">
            {detailsTab === "details"
              ? PRESET_DETAILS[detailsPresetId].included
              : PRESET_DETAILS[detailsPresetId].when}
          </p>
        </div>
      ) : null}

      {notice ? (
        <div className="border-border/30 bg-card/50 text-muted-foreground rounded-xl border p-3 text-xs">
          {notice}
        </div>
      ) : null}

      {editingPresetId && preset && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -10 }}
          className="border-border/30 bg-card/50 rounded-2xl border p-6"
        >
          <div className="mb-6 flex items-center justify-between gap-3">
            <h2 className="text-foreground text-sm font-bold tracking-tight">
              Customize {preset.name}
            </h2>
            <button
              type="button"
              onClick={() => setEditingPresetId(null)}
              className="border-border/30 text-muted-foreground hover:border-primary/30 hover:bg-primary/10 hover:text-primary rounded-lg border px-3 py-1.5 text-xs font-semibold tracking-[0.12em] uppercase transition-colors"
            >
              <XIcon size={12} className="mr-1" /> Done
            </button>
          </div>

          <div className="grid gap-4 lg:grid-cols-2 xl:grid-cols-3">
            <CollapsibleSection
              title="Preset identity"
              icon={<Sparkles size={14} />}
              className="xl:col-span-2"
            >
              <div className="grid gap-3 pt-3 sm:grid-cols-2">
                <label className="text-body text-sm font-medium">
                  Name
                  <input
                    value={customSettings.name ?? preset.name}
                    onChange={(event) =>
                      setCustomSettings({ ...customSettings, name: event.currentTarget.value })
                    }
                    className="border-border/50 bg-background text-foreground focus:border-primary/50 mt-1.5 w-full rounded-lg border px-3 py-2.5 text-sm outline-none"
                  />
                </label>
                <label className="text-body text-sm font-medium">
                  Description
                  <input
                    value={customSettings.description ?? preset.description}
                    onChange={(event) =>
                      setCustomSettings({
                        ...customSettings,
                        description: event.currentTarget.value,
                      })
                    }
                    className="border-border/50 bg-background text-foreground focus:border-primary/50 mt-1.5 w-full rounded-lg border px-3 py-2.5 text-sm outline-none"
                  />
                </label>
              </div>
            </CollapsibleSection>
            <CollapsibleSection title="Sandbox & permissions" icon={<ShieldCheck size={14} />}>
              <Select
                label="Sandbox mode"
                value={customSettings.sandbox_mode ?? null}
                onChange={(v) =>
                  setCustomSettings({ ...customSettings, sandbox_mode: v as string | null })
                }
                options={SANDBOX_MODES}
                description="Filesystem, network, and shell access for the agent"
              />
            </CollapsibleSection>

            <CollapsibleSection
              title="Reasoning & delegation"
              icon={<Brain size={14} />}
              className="xl:col-span-2"
            >
              <Select
                label="Reasoning effort"
                value={customSettings.reasoning_effort ?? null}
                onChange={(v) =>
                  setCustomSettings({ ...customSettings, reasoning_effort: v as string | null })
                }
                options={REASONING_EFFORTS}
                description="How much the model reasons before answering"
              />
              <Select
                label="Delegation depth"
                value={customSettings.subagent_max_depth ?? null}
                onChange={(v) =>
                  setCustomSettings({ ...customSettings, subagent_max_depth: v as number | null })
                }
                options={DELEGATION_DEPTHS}
                description="How many levels of subagent delegation are allowed"
              />
            </CollapsibleSection>

            <CollapsibleSection title="Performance & tools" icon={<SlidersHorizontal size={14} />}>
              <NumberInput
                label="Spill threshold (chars)"
                value={customSettings.spill_threshold_chars ?? null}
                onChange={(v) => setCustomSettings({ ...customSettings, spill_threshold_chars: v })}
                min={2000}
                max={500000}
                description="Output size before spilling to storage"
              />
              <Toggle
                label="Programmatic tool calling (PTC)"
                description="Allow the agent to write and execute programs"
                checked={customSettings.ptc_enabled ?? false}
                onChange={(v) => setCustomSettings({ ...customSettings, ptc_enabled: v })}
              />
              <Toggle
                label="Plan mode by default"
                description="Start each turn in plan mode"
                checked={customSettings.plan_mode_by_default ?? false}
                onChange={(v) => setCustomSettings({ ...customSettings, plan_mode_by_default: v })}
              />
            </CollapsibleSection>

            <CollapsibleSection
              title="Native tools & plugins"
              icon={<Puzzle size={14} />}
              className="lg:col-span-2 xl:col-span-3"
            >
              <ToolAllowlistEditor
                catalogue={pluginCatalogue}
                value={customSettings.tools ?? customSettings.tool_allowlist}
                onChange={(tools) => {
                  const next = { ...customSettings };
                  delete next.tool_allowlist;
                  if (tools === undefined) delete next.tools;
                  else next.tools = tools;
                  setCustomSettings(next);
                }}
              />
            </CollapsibleSection>

            <div className="border-border/30 flex justify-end gap-3 border-t pt-4 lg:col-span-2 xl:col-span-3">
              <button
                type="button"
                onClick={() => setEditingPresetId(null)}
                className="border-border/30 text-muted-foreground hover:border-primary/30 hover:bg-primary/10 hover:text-primary rounded-lg border px-4 py-2 text-sm font-semibold tracking-[0.12em] uppercase transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={saving === preset.id}
                onClick={async () => {
                  setSaving(preset.id);
                  setNotice(null);
                  try {
                    if (preset.id.startsWith("custom_")) {
                      await updateCustomPreset(preset.id, customSettings);
                      await saveUserDefault(preset.id);
                      await load();
                    } else {
                      await saveUserDefault(preset.id, customSettings);
                    }
                    setEditingPresetId(null);
                    setNotice(`New conversations now start in ${preset.name}.`);
                  } catch (cause) {
                    setNotice(
                      cause instanceof Error ? cause.message : "The preset could not be saved.",
                    );
                  } finally {
                    setSaving(null);
                  }
                }}
                className={`bg-primary text-primary-foreground rounded-lg px-4 py-2 text-sm font-semibold tracking-[0.12em] uppercase transition-colors ${
                  saving === preset.id ? "cursor-wait opacity-60" : ""
                }`}
              >
                {saving === preset.id ? <Loader2 size={14} className="mr-2 animate-spin" /> : null}
                Save settings
              </button>
            </div>
          </div>
        </motion.div>
      )}

      <div className="settings-card border-border/30 bg-card/50 rounded-2xl border p-6">
        <h2 className="text-foreground mb-4 text-sm font-bold tracking-tight">
          Default preset for new conversations
        </h2>
        <Select
          label="Default preset"
          value={defaultPreset}
          onChange={async (v) => {
            if (v === null) return;
            try {
              await saveUserDefault(String(v));
              setNotice("The default preset was saved.");
            } catch (cause) {
              setNotice(cause instanceof Error ? cause.message : "The preset could not be saved.");
            }
          }}
          options={presets.map((p) => ({ value: p.id, label: p.name }))}
          description="Every new conversation starts in this mode"
        />
        <p className="text-muted-foreground pt-4 text-xs">
          Deployment default:{" "}
          <code className="border-primary/25 bg-primary/10 text-primary rounded border px-2 py-0.5">
            {presets.find((p) => p.id === deploymentDefault)?.name ?? deploymentDefault}
          </code>
        </p>
      </div>
    </div>
  );
}

function AgentModesTab() {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [enabledModes, setEnabledModes] = useState<Record<string, boolean>>({});
  const [defaultPreset, setDefaultPreset] = useState<string>("standard");
  const [deploymentDefault, setDeploymentDefault] = useState<string>("standard");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [presetsRes, enablementRes, defaultRes] = await Promise.all([
        fetchWithAuth("/deepspace/chats/presets"),
        fetchWithAuth("/deepspace/chats/presets/enablement"),
        fetchWithAuth("/deepspace/chats/presets/default"),
      ]);
      if (!presetsRes.ok) throw new Error("presets");
      const presetsData = (await presetsRes.json()) as PresetsResponse;
      setPresets(presetsData.presets);
      if (enablementRes.ok) {
        const enablementData = (await enablementRes.json()) as EnablementResponse;
        const states: Record<string, boolean> = {};
        enablementData.enablement.forEach((entry) => {
          states[entry.preset_id] = entry.enabled;
        });
        setEnabledModes(states);
      }
      if (defaultRes.ok) {
        const defaultData = (await defaultRes.json()) as UserDefaultPresetResponse;
        setDefaultPreset(defaultData.preset);
        setDeploymentDefault(defaultData.deployment_default);
      }
      setError(null);
    } catch {
      setError("The agent modes could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(handle);
  }, [load]);

  useEffect(() => {
    return subscribeDefaultPresetChanged(({ preset, deploymentDefault: depDef }) => {
      setDefaultPreset(preset);
      if (depDef) setDeploymentDefault(depDef);
    });
  }, []);

  const setModeEnabled = useCallback(async (presetId: string, enabled: boolean) => {
    const response = (await fetchWithAuth("/deepspace/chats/presets/enablement", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ preset_id: presetId, enabled }),
    })) as Response;
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      const message =
        body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
          ? body.detail
          : "The agent mode could not be changed.";
      throw new Error(message);
    }
    const data = (await response.json()) as EnablementResponse;
    const states: Record<string, boolean> = {};
    data.enablement.forEach((entry) => {
      states[entry.preset_id] = entry.enabled;
    });
    setEnabledModes(states);
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 size={28} className="text-muted-foreground animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div
        className="border-destructive/30 bg-destructive/10 text-destructive/90 rounded-xl border p-4 text-sm"
        role="alert"
      >
        <TriangleAlert size={14} className="mr-2" />
        {error}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="settings-card border-border/30 bg-card/50 rounded-2xl border p-6">
        <h2 className="text-foreground mb-4 text-sm font-bold tracking-tight">Available modes</h2>
        <p className="text-muted-foreground mb-6 text-xs">
          Toggle which modes the mode selector offers. A hidden mode is removed from the selector
          and cannot be recorded on a conversation. Active default modes cannot be hidden.
        </p>
        <div className="divide-border/30 divide-y">
          {presets.map((p) => {
            const isUserDefault = p.id === defaultPreset;
            const isDeploymentDefault = p.id === deploymentDefault;
            return (
              <div
                key={p.id}
                className="deepspace-mode-row flex items-center justify-between gap-4 py-4"
              >
                <div className="flex min-w-0 items-center gap-4">
                  <div
                    className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${
                      p.editable
                        ? "border border-violet-400/20 bg-violet-500/10"
                        : "border-primary/20 bg-primary/10 border"
                    }`}
                  >
                    {presetIcon(p.id)}
                  </div>
                  <div className="min-w-0">
                    <p className="text-foreground text-sm font-medium">{p.name}</p>
                    <p className="text-muted-foreground text-xs">{p.description}</p>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      {isUserDefault ? (
                        <span className="border-emerald-500/30 bg-emerald-500/10 text-emerald-400 inline-block rounded border px-2 py-0.5 text-[9px] font-medium">
                          User default
                        </span>
                      ) : null}
                      {isDeploymentDefault ? (
                        <span className="border-primary/25 bg-primary/10 text-primary inline-block rounded border px-2 py-0.5 text-[9px] font-medium">
                          Deployment default
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
                <AgentModeToggle
                  label={p.name}
                  checked={enabledModes[p.id] ?? true}
                  disabled={isDeploymentDefault || isUserDefault || pending === p.id}
                  onChange={async (v) => {
                    setPending(p.id);
                    setNotice(null);
                    try {
                      await setModeEnabled(p.id, v);
                      setNotice(`${p.name} is ${v ? "available" : "hidden"} in the mode selector.`);
                    } catch (cause) {
                      setNotice(
                        cause instanceof Error
                          ? cause.message
                          : "The agent mode could not be changed.",
                      );
                    } finally {
                      setPending(null);
                    }
                  }}
                />
              </div>
            );
          })}
        </div>
      </div>

      {notice ? (
        <div className="border-border/30 bg-card/50 text-muted-foreground rounded-xl border p-3 text-xs">
          {notice}
        </div>
      ) : null}

      <div className="settings-card border-border/30 bg-card/50 rounded-2xl border p-6">
        <h2 className="text-foreground mb-4 text-sm font-bold tracking-tight">Mode details</h2>
        <p className="text-muted-foreground mb-4 text-xs">
          Each mode carries its own tool set, sandbox, reasoning effort, and delegation depth.
          Customize a mode on the Presets tab.
        </p>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {presets.map((p) => (
            <div
              key={p.id}
              className="deepspace-mode-details-card border-glass-border bg-surface-0/65 rounded-xl border p-4"
            >
              <div className="mb-3 flex items-center gap-3">
                <div
                  className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${
                    p.editable
                      ? "border border-violet-400/20 bg-violet-500/10"
                      : "border-primary/20 bg-primary/10 border"
                  }`}
                >
                  {presetIcon(p.id)}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-foreground text-sm font-medium">{p.name}</p>
                  <p className="text-muted-foreground text-xs">{p.description}</p>
                </div>
              </div>
              <p className="text-muted-foreground mb-3 text-xs">
                Tools:{" "}
                {p.tools.length > 0
                  ? `${p.tools.slice(0, 5).join(", ")}${p.tools.length > 5 ? "…" : ""}`
                  : "All"}
              </p>
              <div className="flex flex-wrap gap-2">
                <ModeFlag label="PTC" enabled={p.ptc_enabled} />
                <ModeFlag label="Plan" enabled={p.plan_mode_by_default} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function PresetCard({
  preset,
  settingsOverride,
  isDefault,
  isDeploymentDefault,
  isEditing,
  onEdit,
  onDetails,
  onSetDefault,
  onDelete,
}: {
  preset: Preset;
  settingsOverride?: PresetSettings;
  isDefault: boolean;
  isDeploymentDefault?: boolean;
  isEditing: boolean;
  onEdit: () => void;
  onDetails: (tab: "details" | "how-to-use") => void;
  onSetDefault: () => void;
  onDelete?: () => void;
}) {
  const effectiveSettings = settingsOverride ?? preset.settings;
  const hasExplicitToolSelection =
    effectiveSettings && Object.prototype.hasOwnProperty.call(effectiveSettings, "tools");
  const toolSummary = hasExplicitToolSelection
    ? `${effectiveSettings?.tools?.length ?? 0} selected`
    : preset.tools.length > 0
      ? `${preset.tools.length} selected`
      : "All available";

  return (
    <div
      data-default={isDefault}
      className={`deepspace-preset-card flex flex-col rounded-xl border p-4 ${
        isDefault ? "border-primary/40 bg-primary/5" : "border-border/30 bg-card/50"
      } ${isEditing ? "ring-primary/50 ring-2" : ""}`}
    >
      <div className="mb-3 flex items-start gap-3">
        <div
          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${
            preset.editable ? "bg-violet-500/10" : "bg-muted/50"
          }`}
        >
          {presetIcon(preset.id)}
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-foreground text-sm font-medium">{preset.name}</p>
          <p className="text-muted-foreground mt-0.5 text-xs">{preset.description}</p>
        </div>
      </div>

      <div className="mb-3 flex min-h-5 flex-wrap items-center gap-2">
        {isDefault ? (
          <span className="bg-primary/15 text-primary rounded-full px-2 py-0.5 text-[10px] font-semibold tracking-[0.05em] uppercase">
            Default
          </span>
        ) : null}
        {isDeploymentDefault && !isDefault ? (
          <span className="border-border/40 bg-surface-2 text-muted-foreground rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-[0.05em] uppercase">
            Deployment default
          </span>
        ) : null}
        {preset.editable ? (
          <span className="rounded-full bg-violet-500/10 px-2 py-0.5 text-[10px] font-semibold tracking-[0.05em] text-violet-300 uppercase">
            Customizable
          </span>
        ) : (
          <span className="bg-muted/60 text-muted-foreground rounded-full px-2 py-0.5 text-[10px] font-semibold tracking-[0.05em] uppercase">
            Built-in
          </span>
        )}
      </div>

      <div className="text-muted-foreground mb-3 flex items-center justify-between text-xs">
        <span>Tools: {toolSummary}</span>
        <span>PTC: {preset.ptc_enabled ? "On" : "Off"}</span>
      </div>

      <div className="mt-auto flex gap-2">
        {preset.editable ? (
          <button
            type="button"
            onClick={onEdit}
            className={`flex-1 rounded-xl px-3 py-1.5 text-xs font-semibold tracking-[0.12em] uppercase transition-colors focus-visible:outline-none ${
              isEditing
                ? "bg-primary text-primary-foreground"
                : "border-border/30 text-muted-foreground hover:border-primary/30 hover:bg-primary/10 hover:text-primary border"
            }`}
          >
            {isEditing ? (
              <>
                <Check size={10} className="mr-1" /> Editing
              </>
            ) : (
              "Customize"
            )}
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={() => onDetails("details")}
              className="border-border/30 text-muted-foreground hover:border-primary/30 hover:bg-primary/10 hover:text-primary flex-1 rounded-xl border px-3 py-1.5 text-xs font-semibold tracking-[0.12em] uppercase transition-colors"
            >
              View details
            </button>
            <button
              type="button"
              onClick={() => onDetails("how-to-use")}
              className="border-border/30 text-muted-foreground hover:border-primary/30 hover:bg-primary/10 hover:text-primary flex-1 rounded-xl border px-3 py-1.5 text-xs font-semibold tracking-[0.12em] uppercase transition-colors"
            >
              How to use
            </button>
          </>
        )}
        {!isDefault ? (
          <button
            type="button"
            onClick={onSetDefault}
            className="border-border/30 text-muted-foreground hover:border-primary/30 hover:bg-primary/10 hover:text-primary rounded-lg border px-3 py-1.5 text-xs font-semibold tracking-[0.12em] uppercase transition-colors"
            title="Use this preset for new conversations"
          >
            <Sparkle size={10} className="mr-1" /> Set as default
          </button>
        ) : null}
        {onDelete ? (
          <button
            type="button"
            onClick={onDelete}
            className="border-destructive/30 text-destructive/80 hover:bg-destructive/10 rounded-lg border px-2 py-1.5 transition-colors"
            title="Delete custom preset"
            aria-label={`Delete ${preset.name}`}
          >
            <Trash2 size={13} aria-hidden="true" />
          </button>
        ) : null}
      </div>
    </div>
  );
}
