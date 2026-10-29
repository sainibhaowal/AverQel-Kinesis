"use client";

import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Code2,
  CircleDot,
  ListTodo,
  LockKeyhole,
  Network,
  Package,
  PlugZap,
  Puzzle,
  Search,
  ScrollText,
  ShieldCheck,
  SlidersHorizontal,
  type LucideIcon,
} from "lucide-react";

import {
  filterPluginEntries,
  listPluginCatalogue,
  type PluginCatalogue,
  type PluginEntry,
  type SessionPluginPreset,
} from "../_lib/plugin-api";
import { subscribeDefaultPresetChanged } from "@/app/dashboard/deepspace/_lib/agent-mode-api";
import RoundedSelect from "../../../../components/ui/RoundedSelect";

type PluginGroupProps = {
  title: string;
  subtitle: string;
  count: number;
  children: ReactNode;
  defaultOpen?: boolean;
  className?: string;
};

function PluginGroup({
  title,
  subtitle,
  count,
  children,
  defaultOpen = false,
  className,
}: PluginGroupProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [renderContent, setRenderContent] = useState(defaultOpen);
  const closeTimer = useRef<number | null>(null);
  const contentId = useId();

  useEffect(() => {
    return () => {
      if (closeTimer.current !== null) {
        window.clearTimeout(closeTimer.current);
      }
    };
  }, []);

  const toggleOpen = () => {
    if (open) {
      setOpen(false);
      closeTimer.current = window.setTimeout(() => {
        setRenderContent(false);
        closeTimer.current = null;
      }, 300);
      return;
    }

    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
    setRenderContent(true);
    window.requestAnimationFrame(() => setOpen(true));
  };

  return (
    <section
      className={`deepspace-plugin-group border-glass-border bg-surface-1/70 overflow-hidden rounded-xl border ${className ?? ""}`}
    >
      <button
        type="button"
        onClick={toggleOpen}
        aria-expanded={open}
        aria-controls={contentId}
        className="hover:bg-primary/10 hover:text-primary flex w-full items-center justify-between gap-3 p-4 text-left transition-colors"
      >
        <span className="flex min-w-0 items-center gap-3">
          <ChevronDown
            size={16}
            className={`text-muted-foreground shrink-0 transition-transform ${open ? "" : "-rotate-90"}`}
            aria-hidden="true"
          />
          <span className="min-w-0">
            <span className="text-foreground block text-sm font-semibold">{title}</span>
            <span className="text-muted-foreground mt-1 block text-xs">{subtitle}</span>
          </span>
        </span>
        <span className="text-primary shrink-0 text-xs font-semibold">{count}</span>
      </button>
      {renderContent ? (
        <div
          id={contentId}
          aria-hidden={!open}
          inert={!open}
          className={`grid overflow-hidden transition-[grid-template-rows,opacity] duration-300 ease-out motion-reduce:transition-none ${open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"}`}
        >
          <div className="border-glass-border min-h-0 overflow-hidden border-t p-3">{children}</div>
        </div>
      ) : null}
    </section>
  );
}

type PluginAvailability = "available" | "disabled" | "not-in-preset";
type PresetStatusFilter = "all" | PluginAvailability;

function PluginStatus({ status }: { status: PluginAvailability }) {
  const enabled = status === "available";
  const label =
    status === "available"
      ? "Available"
      : status === "disabled"
        ? "Off by deployment"
        : "Not in this preset";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[10px] font-semibold tracking-[0.08em] uppercase ${
        enabled
          ? "border-primary/25 bg-primary/10 text-primary"
          : status === "disabled"
            ? "border-amber-400/25 bg-amber-400/10 text-amber-200"
            : "border-glass-border bg-surface-2/70 text-muted-foreground"
      }`}
    >
      {enabled ? (
        <CheckCircle2 size={11} aria-hidden="true" />
      ) : (
        <AlertTriangle size={11} aria-hidden="true" />
      )}
      {label}
    </span>
  );
}

function PluginCard({
  entry,
  presetNames,
  availability,
  presetToolNames,
}: {
  entry: PluginEntry;
  presetNames?: string[];
  availability?: PluginAvailability;
  presetToolNames?: readonly string[];
}) {
  const [open, setOpen] = useState(false);
  const status: PluginAvailability =
    availability ??
    (entry.available === false || !entry.deployment_enabled ? "disabled" : "available");

  return (
    <article className="deepspace-plugin-card border-glass-border bg-surface-0/65 rounded-lg border">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        className="hover:bg-primary/10 hover:text-primary flex w-full items-start justify-between gap-4 p-4 text-left transition-colors"
      >
        <span className="flex min-w-0 items-start gap-3">
          <span className="bg-primary/10 text-primary mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg">
            <Puzzle size={15} aria-hidden="true" />
          </span>
          <span className="min-w-0">
            <span className="text-foreground block truncate text-sm font-semibold">
              {entry.name}
            </span>
            <span className="text-muted-foreground mt-1 block text-xs leading-relaxed">
              {entry.description || "No description supplied."}
            </span>
            <code className="text-muted-foreground mt-2 block truncate text-[10px]">
              {entry.id}
            </code>
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-2">
          <PluginStatus status={status} />
          <ChevronDown
            size={14}
            className={`text-muted-foreground transition-transform ${open ? "" : "-rotate-90"}`}
            aria-hidden="true"
          />
        </span>
      </button>
      {open ? (
        <div className="border-glass-border space-y-3 border-t px-4 py-3 text-xs">
          <dl className="grid gap-2 sm:grid-cols-2">
            <div>
              <dt className="text-muted-foreground">Source</dt>
              <dd className="text-foreground mt-0.5 font-medium">{entry.source || "unknown"}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Version</dt>
              <dd className="text-foreground mt-0.5 font-medium">
                {entry.version || "unspecified"}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Handler</dt>
              <dd className="text-foreground mt-0.5 font-medium">
                {entry.has_behaviour
                  ? entry.handler_kind || "configured"
                  : "Guidance and policy only"}
              </dd>
            </div>
            {entry.native_tool ? (
              <div>
                <dt className="text-muted-foreground">Native registry tool</dt>
                <dd className="text-foreground mt-0.5 font-medium">
                  <code>{entry.native_tool}</code>
                </dd>
              </div>
            ) : null}
            <div>
              <dt className="text-muted-foreground">Configuration</dt>
              <dd className="text-foreground mt-0.5 font-medium">
                {entry.settings.length > 0 ? `${entry.settings.length} setting(s)` : "No settings"}
              </dd>
            </div>
          </dl>
          {entry.tools.length > 0 ? (
            <div>
              <p className="text-muted-foreground mb-1">Tools</p>
              <ul className="space-y-1">
                {entry.tools.map((tool) => {
                  const toolAvailable = presetToolNames
                    ? status === "available" && presetToolNames.includes(tool.name)
                    : undefined;
                  return (
                    <li key={tool.name} className="text-foreground flex items-start gap-2">
                      <Code2
                        size={13}
                        className={`${toolAvailable === false ? "text-muted-foreground" : "text-primary"} mt-0.5 shrink-0`}
                        aria-hidden="true"
                      />
                      <span className="flex min-w-0 flex-1 items-start justify-between gap-3">
                        <span className="min-w-0">
                          <code>{tool.name}</code>
                          <span className="text-muted-foreground ml-2">{tool.mode}</span>
                        </span>
                        {toolAvailable !== undefined ? (
                          <span
                            className={`shrink-0 text-[10px] font-semibold tracking-[0.08em] uppercase ${toolAvailable ? "text-primary" : "text-muted-foreground"}`}
                          >
                            {toolAvailable ? "Available" : "Off"}
                          </span>
                        ) : null}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : null}
          {presetNames && presetNames.length > 0 ? (
            <p className="text-muted-foreground">
              Available in: <span className="text-foreground">{presetNames.join(" · ")}</span>
            </p>
          ) : null}
          {entry.enabled_by_default ? (
            <p className="text-primary">Enabled for new conversations by default.</p>
          ) : null}
          {entry.has_guidance ? (
            <p className="text-muted-foreground">Adds guidance to turns that run this plugin.</p>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function presetPluginAvailability(
  entry: PluginEntry,
  presetId: string,
  selectedPluginIds: ReadonlySet<string>,
  pluginsEnabled: boolean,
): PluginAvailability {
  if (!pluginsEnabled || !entry.deployment_enabled) return "disabled";
  if (entry.available_in_presets.includes(presetId) || selectedPluginIds.has(entry.id)) {
    return "available";
  }
  return "not-in-preset";
}

function PresetRuntimeSummary({
  preset,
  availableCount,
  notInPresetCount,
  disabledCount,
  statusFilter,
  onStatusFilterChange,
}: {
  preset: SessionPluginPreset;
  availableCount: number;
  notInPresetCount: number;
  disabledCount: number;
  statusFilter: PresetStatusFilter;
  onStatusFilterChange: (filter: PresetStatusFilter) => void;
}) {
  const modeFlags = [
    typeof preset.ptc_enabled === "boolean"
      ? `PTC ${preset.ptc_enabled ? "on" : "off"}`
      : "PTC status unavailable",
    typeof preset.subagent_max_depth === "number"
      ? preset.subagent_max_depth > 0
        ? `Delegation depth ${preset.subagent_max_depth}`
        : "Delegation off"
      : "Delegation: deployment default",
    `Sandbox: ${preset.sandbox_mode ?? "deployment default"}`,
  ];

  return (
    <div
      className="border-glass-border bg-surface-0/70 mb-3 rounded-xl border p-3"
      aria-label={`${preset.name} runtime status`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-foreground text-xs font-semibold">{preset.name} runtime status</p>
          <p className="text-muted-foreground mt-1 text-[11px]">{preset.description}</p>
        </div>
        <span className="text-primary text-[10px] font-semibold tracking-[0.08em] uppercase">
          {preset.tool_count} enabled tool{preset.tool_count === 1 ? "" : "s"}
        </span>
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {modeFlags.map((flag) => (
          <span
            key={flag}
            className="border-glass-border bg-surface-2/70 text-muted-foreground rounded-md border px-2 py-1 text-[10px] font-semibold tracking-[0.06em] uppercase"
          >
            {flag}
          </span>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] font-semibold tracking-[0.06em] uppercase">
        <div
          className="flex flex-wrap gap-1.5"
          role="group"
          aria-label="Filter preset extensions by availability"
        >
          {(
            [
              { id: "all", label: "All", count: availableCount + notInPresetCount + disabledCount },
              { id: "available", label: "Available", count: availableCount },
              { id: "not-in-preset", label: "Not in preset", count: notInPresetCount },
              { id: "disabled", label: "Off", count: disabledCount },
            ] satisfies Array<{ id: PresetStatusFilter; label: string; count: number }>
          ).map((filter) => {
            const selected = statusFilter === filter.id;
            return (
              <button
                key={filter.id}
                type="button"
                aria-pressed={selected}
                onClick={() => onStatusFilterChange(filter.id)}
                className={`rounded-lg border px-2.5 py-1.5 text-[10px] font-semibold tracking-[0.06em] uppercase transition-colors ${
                  selected
                    ? filter.id === "disabled"
                      ? "border-amber-300/40 bg-amber-400/15 text-amber-200"
                      : "border-primary/40 bg-primary/15 text-primary"
                    : "border-glass-border bg-surface-2/50 text-muted-foreground hover:border-primary/30 hover:text-foreground"
                }`}
              >
                {filter.label}: {filter.count}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function CapabilityStat({
  label,
  value,
  detail,
}: {
  label: string;
  value: number;
  detail: string;
}) {
  return (
    <div className="deepspace-capability-stat border-glass-border bg-surface-0/65 rounded-lg border p-4">
      <p className="text-primary/80 text-[10px] font-semibold tracking-[0.08em] uppercase">
        {label}
      </p>
      <p className="text-foreground mt-2 text-2xl font-semibold">{value}</p>
      <p className="text-muted-foreground mt-1 text-xs leading-relaxed">{detail}</p>
    </div>
  );
}

const CORE_RUNTIME_ICON_MATCHES: Array<{ terms: string[]; icon: LucideIcon }> = [
  { terms: ["agent-loop", "agent loop"], icon: Activity },
  { terms: ["session-log", "session log"], icon: ScrollText },
  { terms: ["tool-policy", "tool policy"], icon: ShieldCheck },
  { terms: ["sandbox"], icon: LockKeyhole },
  { terms: ["preset", "agent-mode", "agent mode"], icon: SlidersHorizontal },
  { terms: ["worker", "queue"], icon: ListTodo },
  { terms: ["mcp"], icon: Network },
  { terms: ["plugin", "registry"], icon: PlugZap },
];

const CORE_RUNTIME_FALLBACK_ICONS: LucideIcon[] = [
  Activity,
  ScrollText,
  ShieldCheck,
  LockKeyhole,
  SlidersHorizontal,
  ListTodo,
  Network,
  PlugZap,
];

function coreRuntimeIcon(id: string, name: string, index: number): LucideIcon {
  const label = `${id} ${name}`.toLowerCase();
  return (
    CORE_RUNTIME_ICON_MATCHES.find(({ terms }) => terms.some((term) => label.includes(term)))
      ?.icon ??
    CORE_RUNTIME_FALLBACK_ICONS[index % CORE_RUNTIME_FALLBACK_ICONS.length] ??
    CircleDot
  );
}

function CoreRuntimeCard({
  id,
  name,
  description,
  index,
}: {
  id: string;
  name: string;
  description: string;
  index: number;
}) {
  const Icon = coreRuntimeIcon(id, name, index);
  return (
    <article className="deepspace-core-card border-glass-border bg-surface-0/65 flex items-start gap-3 rounded-lg border p-4">
      <span className="border-primary/20 bg-primary/10 text-primary mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border">
        <Icon size={15} aria-hidden="true" />
      </span>
      <span>
        <span className="text-foreground block text-sm font-semibold">{name}</span>
        <span className="text-muted-foreground mt-1 block text-xs leading-relaxed">
          {description}
        </span>
      </span>
    </article>
  );
}

function sessionPreset(
  catalogue: PluginCatalogue,
  selectedId: string,
): SessionPluginPreset | undefined {
  return (
    catalogue.sessionPresets.find((preset) => preset.id === selectedId) ??
    catalogue.sessionPresets.find((preset) => preset.is_default) ??
    catalogue.sessionPresets[0]
  );
}

/** Display DeepSpace core capabilities and optional preset-scoped extensions. */
export default function PluginInventoryTab() {
  const [catalogue, setCatalogue] = useState<PluginCatalogue | null>(null);
  const [selectedPresetId, setSelectedPresetId] = useState("standard");
  const [presetStatusFilter, setPresetStatusFilter] = useState<PresetStatusFilter>("all");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void listPluginCatalogue()
      .then(
        (value) => {
          if (cancelled) return;
          setCatalogue(value);
          setSelectedPresetId(value.defaultPreset);
        },
        (cause: unknown) => {
          if (!cancelled)
            setError(
              cause instanceof Error
                ? cause.message
                : "The DeepSpace capability catalogue could not be loaded.",
            );
        },
      )
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    return subscribeDefaultPresetChanged(({ preset }) => {
      setSelectedPresetId(preset);
      setCatalogue((prev) => (prev ? { ...prev, defaultPreset: preset } : prev));
    });
  }, []);

  const selected = catalogue ? sessionPreset(catalogue, selectedPresetId) : undefined;
  const selectedPluginIds = useMemo(
    () => new Set(selected?.plugins.map((entry) => entry.id) ?? []),
    [selected?.plugins],
  );
  const presetAvailability = useMemo(() => {
    if (!catalogue || !selected) {
      return { available: 0, notInPreset: 0, disabled: 0 };
    }
    return catalogue.globalPlugins.reduce(
      (counts, entry) => {
        const status = presetPluginAvailability(
          entry,
          selected.id,
          selectedPluginIds,
          catalogue.pluginsEnabled,
        );
        if (status === "available") counts.available += 1;
        else if (status === "disabled") counts.disabled += 1;
        else counts.notInPreset += 1;
        return counts;
      },
      { available: 0, notInPreset: 0, disabled: 0 },
    );
  }, [catalogue, selected, selectedPluginIds]);
  const visiblePresetPlugins = useMemo(
    () => filterPluginEntries(catalogue?.globalPlugins ?? [], query),
    [catalogue?.globalPlugins, query],
  );
  const filteredPresetPlugins = useMemo(
    () =>
      visiblePresetPlugins.filter(
        (entry) =>
          presetStatusFilter === "all" ||
          presetPluginAvailability(
            entry,
            selected?.id ?? "",
            selectedPluginIds,
            catalogue?.pluginsEnabled ?? false,
          ) === presetStatusFilter,
      ),
    [
      catalogue?.pluginsEnabled,
      presetStatusFilter,
      selected?.id,
      selectedPluginIds,
      visiblePresetPlugins,
    ],
  );
  const visibleGlobalPlugins = useMemo(
    () => filterPluginEntries(catalogue?.globalPlugins ?? [], query),
    [catalogue?.globalPlugins, query],
  );
  const counts = catalogue?.counts ?? {
    extensions: catalogue?.globalPlugins.length ?? 0,
    enabled_extensions:
      catalogue?.globalPlugins.filter((entry) => entry.deployment_enabled).length ?? 0,
    tools: new Set(
      catalogue?.globalPlugins.flatMap((entry) => entry.tools.map((tool) => tool.name)),
    ).size,
    native_tools: 0,
    compatibility_tools: 0,
    extension_tools: 0,
  };

  if (loading) {
    return (
      <div className="text-muted-foreground flex items-center justify-center py-16 text-sm">
        Loading DeepSpace capabilities…
      </div>
    );
  }
  if (error || catalogue === null) {
    return (
      <div
        className="border-destructive/30 bg-destructive/10 text-destructive/90 flex items-center gap-2 rounded-xl border p-4 text-sm"
        role="alert"
      >
        <AlertTriangle size={15} aria-hidden="true" />
        {error ?? "The DeepSpace capability catalogue could not be loaded."}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="settings-card border-border/30 bg-card/50 rounded-2xl border p-6">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-foreground text-sm font-bold tracking-tight">
              DeepSpace capabilities
            </h2>
            <p className="text-muted-foreground mt-1 text-xs">
              The core runtime is always available. Extensions add optional tools, guidance, or
              restrictions.
            </p>
          </div>
          <span
            className={`rounded-full border px-2 py-1 text-[10px] font-semibold tracking-[0.08em] uppercase ${catalogue.pluginsEnabled ? "border-primary/25 bg-primary/10 text-primary" : "border-warning/30 bg-warning/10 text-warning"}`}
          >
            {catalogue.pluginsEnabled ? "Extensions on" : "Extensions off"}
          </span>
        </div>
        <label className="border-glass-border bg-surface-0/70 focus-within:border-primary/50 focus-within:ring-primary/15 flex items-center gap-2 rounded-lg border px-3 py-2.5 transition-colors focus-within:ring-2">
          <Search size={15} className="text-muted-foreground shrink-0" aria-hidden="true" />
          <span className="sr-only">Search extensions and tools</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.currentTarget.value)}
            placeholder="Search extensions and tools"
            aria-label="Search extensions and tools"
            className="text-foreground placeholder:text-muted-foreground/60 min-w-0 flex-1 bg-transparent text-sm outline-none"
          />
        </label>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <CapabilityStat
          label="Core runtime"
          value={catalogue.coreRuntime.length}
          detail="Always provided by DeepSpace"
        />
        <CapabilityStat
          label="Extensions"
          value={counts.extensions}
          detail={`${counts.enabled_extensions} enabled by deployment policy`}
        />
        <CapabilityStat
          label="Registered tools"
          value={counts.tools}
          detail={`${counts.native_tools} native · ${counts.compatibility_tools} compatibility`}
        />
        <CapabilityStat
          label="Preset"
          value={selected?.tool_count ?? 0}
          detail={selected ? `${selected.name} extension tools` : "Loading preset"}
        />
      </div>

      {catalogue.errors.length > 0 ? (
        <div
          className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-xs text-amber-200"
          role="status"
        >
          <p className="font-semibold">Some extension declarations were skipped.</p>
          <ul className="mt-2 list-disc space-y-1 pl-4">
            {catalogue.errors.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <p className="text-muted-foreground px-1 text-xs">
        <Package size={13} className="mr-1 inline-block" aria-hidden="true" />
        Conversation extension activation is recorded in the session event log and remains subject
        to deployment policy. Dynamic MCP tools and workspace-scoped extensions are resolved
        separately for the conversation.
      </p>

      <div className="grid gap-4">
        <div>
          <PluginGroup
            title="Core DeepSpace runtime"
            subtitle="Built into the product; these are not optional plugins"
            count={catalogue.coreRuntime.length}
            defaultOpen
          >
            <div className="grid gap-2 md:grid-cols-2">
              {catalogue.coreRuntime.map((component, index) => (
                <CoreRuntimeCard
                  key={component.id}
                  id={component.id}
                  name={component.name}
                  description={component.description}
                  index={index}
                />
              ))}
            </div>
          </PluginGroup>
        </div>

        {selected ? (
          <div>
            <PluginGroup
              title="Preset extensions"
              subtitle={`For ${selected.name} · ${presetAvailability.available} available · ${presetAvailability.notInPreset} not in preset · ${presetAvailability.disabled} off`}
              count={visiblePresetPlugins.length}
              defaultOpen
            >
              <div className="mb-3 flex items-center justify-between gap-3">
                <span className="text-muted-foreground text-xs">
                  Extension availability follows the same tool allowlist used by the chat runtime.
                </span>
                <label className="sr-only" htmlFor="plugin-preset">
                  Session preset
                </label>
                <RoundedSelect
                  id="plugin-preset"
                  label="Session preset"
                  value={selected.id}
                  onChange={(presetId) => {
                    setSelectedPresetId(presetId);
                    setPresetStatusFilter("all");
                  }}
                  options={catalogue.sessionPresets.map((preset) => ({
                    value: preset.id,
                    label: `${preset.name}${preset.is_default ? " (default)" : ""}`,
                  }))}
                  triggerClassName="border-glass-border bg-surface-0 text-foreground hover:bg-surface-1 focus-visible:outline-primary/70 min-w-[12rem] rounded-xl border px-3 py-2 text-xs font-semibold focus-visible:outline-offset-2"
                />
              </div>
              <PresetRuntimeSummary
                preset={selected}
                availableCount={presetAvailability.available}
                notInPresetCount={presetAvailability.notInPreset}
                disabledCount={presetAvailability.disabled}
                statusFilter={presetStatusFilter}
                onStatusFilterChange={setPresetStatusFilter}
              />
              {filteredPresetPlugins.length > 0 ? (
                <div
                  className="deepspace-plugin-list max-h-[32rem] space-y-2 overflow-y-auto overscroll-contain pr-1"
                  aria-label="Preset extensions"
                >
                  {filteredPresetPlugins.map((entry) => (
                    <PluginCard
                      key={entry.id}
                      entry={entry}
                      availability={presetPluginAvailability(
                        entry,
                        selected.id,
                        selectedPluginIds,
                        catalogue.pluginsEnabled,
                      )}
                      presetToolNames={selected.tools}
                    />
                  ))}
                </div>
              ) : (
                <p className="text-muted-foreground px-1 py-4 text-xs">
                  No preset extensions match this status and search.
                </p>
              )}
            </PluginGroup>
          </div>
        ) : null}

        <div>
          <PluginGroup
            title="Available extensions"
            subtitle={`Registered by this deployment · ${visibleGlobalPlugins.length} extension${visibleGlobalPlugins.length === 1 ? "" : "s"}`}
            count={visibleGlobalPlugins.length}
          >
            {visibleGlobalPlugins.length > 0 ? (
              <div
                className="deepspace-plugin-list max-h-[32rem] space-y-2 overflow-y-auto overscroll-contain pr-1"
                aria-label="Available extensions"
              >
                {visibleGlobalPlugins.map((entry) => (
                  <PluginCard
                    key={entry.id}
                    entry={entry}
                    presetNames={entry.available_in_presets.map(
                      (id) =>
                        catalogue.sessionPresets.find((preset) => preset.id === id)?.name ?? id,
                    )}
                  />
                ))}
              </div>
            ) : (
              <p className="text-muted-foreground px-1 py-4 text-xs">
                No extensions match this search.
              </p>
            )}
          </PluginGroup>
        </div>
      </div>
    </div>
  );
}
