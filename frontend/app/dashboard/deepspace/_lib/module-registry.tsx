"use client";

import {
  createContext,
  useContext,
  useMemo,
  useState,
  useCallback,
  useEffect,
  useSyncExternalStore,
  Fragment,
  ReactNode,
  createElement,
  type ReactElement,
} from "react";

// ============================================================================
// Types
// ============================================================================

export type SlotName =
  | "toolbar"
  | "toolbar-item"
  | "sidebar"
  | "panel"
  | "panel-tab"
  | "context-menu"
  | "slash-command"
  | "composer-toolbar"
  | "composer-slash"
  | "status-bar"
  | "notification";

export interface SlotContext {
  conversationId: string | null;
  userId: string | null;
  tenantId: string | null;
  workspaceId: string | null;
  permissions: string[];
  roles: string[];
}

export interface SlotProps {
  context?: SlotContext;
  [key: string]: unknown;
}

export interface SlotRegistration {
  slot: SlotName;
  component: React.ComponentType<SlotProps>;
  priority?: number;
  condition?: (ctx: SlotContext) => boolean;
  fallback?: React.ComponentType;
  id?: string;
  name?: string;
  label?: string;
  icon?: React.ComponentType<{ size?: number; className?: string }>;
}

export interface ModuleManifest {
  id: string;
  name: string;
  version: string;
  provides: SlotRegistration[];
  dependsOn?: string[];
  permissions?: string[];
  enabledByDefault?: boolean;
  lazy?: boolean;
}

export interface ModuleHandle {
  id: string;
  manifest: ModuleManifest;
  enabled: boolean;
  unregister: () => void;
  enable: () => void;
  disable: () => void;
}

export interface SlotContextValue {
  conversationId: string | null;
  userId: string | null;
  tenantId: string | null;
  workspaceId: string | null;
  permissions: string[];
  roles: string[];
}

export interface ModuleRegistry {
  registerModule: (manifest: ModuleManifest) => ModuleHandle;
  unregisterModule: (moduleId: string) => void;
  getModule: (moduleId: string) => ModuleHandle | undefined;
  getSlot: (slot: SlotName) => SlotRegistration[];
  enableModule: (moduleId: string) => void;
  disableModule: (moduleId: string) => void;
  getContext: () => SlotContextValue;
  setContext: (context: Partial<SlotContextValue>) => void;
  subscribeContext: (callback: (context: SlotContextValue) => void) => () => void;
  /** Subscribe to registry changes so slot components re-render when modules register. */
  subscribe: (listener: () => void) => () => void;
  /** Monotonic registry version, consumed by `useSyncExternalStore`. */
  getVersion: () => number;
}

interface ModuleEntry {
  manifest: ModuleManifest;
  enabled: boolean;
  handle: ModuleHandle;
}

/** A slot contribution that remembers which module owns it. */
interface RegisteredSlot extends SlotRegistration {
  moduleId: string;
}

// ============================================================================
// Context
// ============================================================================

const ModuleRegistryContext = createContext<ModuleRegistry | null>(null);
const SlotContextValue = createContext<SlotContextValue>({
  conversationId: null,
  userId: null,
  tenantId: null,
  workspaceId: null,
  permissions: [],
  roles: [],
});

export function useSlotContext(): SlotContextValue {
  return useContext(SlotContextValue);
}

export function useModuleRegistry(): ModuleRegistry {
  const registry = useContext(ModuleRegistryContext);
  if (!registry) {
    throw new Error("useModuleRegistry must be used within a ModuleRegistryProvider");
  }
  return registry;
}

export function useSlotContextValue(): SlotContextValue {
  return useContext(SlotContextValue);
}

// ============================================================================
// Module Registry Implementation
// ============================================================================

function createModuleRegistry(
  initialContext: SlotContextValue = {
    conversationId: null,
    userId: null,
    tenantId: null,
    workspaceId: null,
    permissions: [],
    roles: [],
  },
): ModuleRegistry {
  const modules = new Map<string, ModuleEntry>();
  const slotRegistrations = new Map<SlotName, RegisteredSlot[]>();
  const listeners = new Set<() => void>();
  let contextValue = initialContext;
  let version = 0;

  const moduleRegistry: ModuleRegistry = {
    registerModule,
    unregisterModule,
    getModule,
    getSlot,
    enableModule,
    disableModule,
    getContext: () => contextValue,
    setContext,
    subscribeContext,
    subscribe,
    getVersion: () => version,
  };

  return moduleRegistry;

  function notify(): void {
    version += 1;
    for (const listener of listeners) listener();
  }

  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }

  function contributionsOf(manifest: ModuleManifest): RegisteredSlot[] {
    return manifest.provides.map((registration) => ({
      ...registration,
      // Keep the caller's stable tab/action id. The module id is tracked
      // separately so unregistering one module cannot remove another
      // module's contribution that happens to share an id.
      id: registration.id ?? manifest.id,
      moduleId: manifest.id,
    }));
  }

  function insertContributions(manifest: ModuleManifest): void {
    for (const registration of contributionsOf(manifest)) {
      const existing = slotRegistrations.get(registration.slot) ?? [];
      const next = [...existing, registration];
      next.sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0));
      slotRegistrations.set(registration.slot, next);
    }
  }

  function removeContributions(moduleId: string): void {
    for (const [slot, registrations] of slotRegistrations) {
      const filtered = registrations.filter((r) => r.moduleId !== moduleId);
      if (filtered.length === 0) {
        slotRegistrations.delete(slot);
      } else {
        slotRegistrations.set(slot, filtered);
      }
    }
  }

  function registerModule(manifest: ModuleManifest): ModuleHandle {
    if (modules.has(manifest.id)) {
      console.warn(`Module ${manifest.id} already registered, replacing`);
      unregisterModule(manifest.id);
    }

    const entry: ModuleEntry = {
      manifest,
      enabled: manifest.enabledByDefault ?? true,
      handle: null as unknown as ModuleHandle,
    };

    const handle: ModuleHandle = {
      id: manifest.id,
      manifest,
      enabled: entry.enabled,
      unregister: () => unregisterModule(manifest.id),
      enable: () => enableModule(manifest.id),
      disable: () => disableModule(manifest.id),
    };

    entry.handle = handle;
    modules.set(manifest.id, entry);

    if (entry.enabled) insertContributions(manifest);
    notify();

    return handle;
  }

  function unregisterModule(moduleId: string): void {
    const entry = modules.get(moduleId);
    if (!entry) return;

    removeContributions(moduleId);
    modules.delete(moduleId);
    notify();
  }

  function getModule(moduleId: string): ModuleHandle | undefined {
    return modules.get(moduleId)?.handle;
  }

  function getSlot(slot: SlotName): SlotRegistration[] {
    return slotRegistrations.get(slot) ?? [];
  }

  function enableModule(moduleId: string): void {
    const entry = modules.get(moduleId);
    if (!entry || entry.enabled) return;
    entry.enabled = true;
    insertContributions(entry.manifest);
    notify();
  }

  function disableModule(moduleId: string): void {
    const entry = modules.get(moduleId);
    if (!entry || !entry.enabled) return;
    entry.enabled = false;
    removeContributions(moduleId);
    notify();
  }

  function setContext(newContext: Partial<SlotContextValue>): void {
    const changed = (Object.keys(newContext) as (keyof SlotContextValue)[]).some(
      (key) => newContext[key] !== contextValue[key],
    );
    if (!changed) return;
    contextValue = { ...contextValue, ...newContext };
    notify();
  }

  function subscribeContext(callback: (context: SlotContextValue) => void): () => void {
    // Context changes go through the registry subscription; callers that need
    // the new value read getContext() when notified.
    return subscribe(() => callback(contextValue));
  }
}

/**
 * Re-render a slot component whenever modules register, unregister, or change
 * enabled state. Without this, a module registered in an effect after mount
 * would never reach an already-rendered slot.
 */
function useRegistryVersion(): number {
  const registry = useModuleRegistry();
  return useSyncExternalStore(registry.subscribe, registry.getVersion, registry.getVersion);
}

// ============================================================================
// SlotOutlet Component
// ============================================================================

interface SlotOutletProps {
  slot: SlotName;
  fallback?: ReactNode;
  context?: Partial<SlotContextValue>;
  className?: string;
  children?: ReactNode;
}

export function SlotOutlet({
  slot,
  fallback = null,
  context,
  className,
  children,
}: SlotOutletProps): ReactElement {
  const registry = useModuleRegistry();
  // Subscribe to registry changes; the fresh list is read below.
  useRegistryVersion();
  const parentContext = useSlotContextValue();
  const mergedContext = useMemo(() => ({ ...parentContext, ...context }), [parentContext, context]);

  const registrations = registry
    .getSlot(slot)
    .filter((reg) => !reg.condition || reg.condition(mergedContext));
  const sortedRegistrations = [...registrations].sort(
    (a, b) => (b.priority ?? 0) - (a.priority ?? 0),
  );

  // If there are registered components, render the highest priority one
  if (sortedRegistrations.length > 0) {
    const registration = sortedRegistrations[0];
    const Component = registration.component;
    return (
      <div className={className}>
        <Component context={mergedContext} />
      </div>
    );
  }

  // Render fallback or children
  return <div className={className}>{children ?? fallback}</div>;
}

// ============================================================================
// ModuleRegistryProvider Component
// ============================================================================

interface ModuleRegistryProviderProps {
  children: ReactNode;
  initialContext?: Partial<SlotContextValue>;
  initialModules?: ModuleManifest[];
}

export function ModuleRegistryProvider({
  children,
  initialContext,
  initialModules = [],
}: ModuleRegistryProviderProps) {
  const [contextValue, setContextValue] = useState<SlotContextValue>({
    conversationId: null,
    userId: null,
    tenantId: null,
    workspaceId: null,
    permissions: [],
    roles: [],
    ...initialContext,
  });

  // The registry instance lives for the provider's lifetime; its mutable
  // context is seeded from the first render and updated through setContext.
  const [registryRef] = useState(() => createModuleRegistry(contextValue));

  // Register initial modules
  useEffect(() => {
    for (const manifest of initialModules) {
      registryRef.registerModule(manifest);
    }
    return () => {
      // Cleanup on unmount
      for (const manifest of initialModules) {
        registryRef.unregisterModule(manifest.id);
      }
    };
  }, [initialModules, registryRef]);

  // Mirror registry context updates into React state so `useSlotContext`
  // consumers see the conversation/user the panel is currently showing.
  useEffect(() => registryRef.subscribeContext(setContextValue), [registryRef]);

  return (
    <ModuleRegistryContext.Provider value={registryRef}>
      <SlotContextValue.Provider value={contextValue}>{children}</SlotContextValue.Provider>
    </ModuleRegistryContext.Provider>
  );
}

// ============================================================================
// Hooks
// ============================================================================

export function useSlot(
  slot: SlotName,
  _fallback?: ReactNode,
  context?: Partial<SlotContextValue>,
) {
  const registry = useModuleRegistry();
  useRegistryVersion();
  const parentContext = useSlotContext();

  const mergedContext = useMemo(() => ({ ...parentContext, ...context }), [parentContext, context]);

  const registrations = registry
    .getSlot(slot)
    .filter((reg) => !reg.condition || reg.condition(mergedContext));

  const sortedRegistrations = [...registrations].sort(
    (a, b) => (b.priority ?? 0) - (a.priority ?? 0),
  );

  return {
    registrations: sortedRegistrations,
    fallback: null, // Will be handled by SlotOutlet
  };
}

export function useSlotOutlet(slot: SlotName, fallback?: ReactNode) {
  const { registrations } = useSlot(slot, fallback);
  const context = useSlotContext();

  if (registrations.length > 0) {
    const registration = registrations[0];
    const Component = registration.component;
    return <Component context={context} />;
  }

  return <>{fallback}</>;
}

// ============================================================================
// Helper hooks for modules
// ============================================================================

export function useModule(moduleId: string) {
  const registry = useModuleRegistry();
  useRegistryVersion();
  return registry.getModule(moduleId);
}

export function useSlotRegistrations(slot: SlotName) {
  const registry = useModuleRegistry();
  useRegistryVersion();
  const context = useSlotContext();

  return registry.getSlot(slot).filter((reg) => !reg.condition || reg.condition(context));
}

export function useSlotComponent(slot: SlotName, fallback?: ReactNode) {
  const { registrations } = useSlot(slot);
  const context = useSlotContext();

  if (registrations.length > 0) {
    const registration = registrations[0];
    const Component = registration.component;
    return <Component context={context} />;
  }

  return <>{fallback}</>;
}

// ============================================================================
// SlotTabs Component - Renders multiple registered components as tabs
// ============================================================================

interface SlotTabsProps {
  slot: SlotName;
  fallback?: ReactNode;
  context?: Partial<SlotContextValue>;
  className?: string;
  activeTab?: string;
  onTabChange?: (tabId: string) => void;
  ariaLabel?: string;
  renderTab?: (registration: SlotRegistration, isActive: boolean, onClick: () => void) => ReactNode;
}

export function SlotTabs({
  slot,
  fallback = null,
  context,
  className,
  activeTab,
  onTabChange,
  ariaLabel,
  renderTab,
}: SlotTabsProps): ReactElement {
  const registry = useModuleRegistry();
  useRegistryVersion();
  const parentContext = useSlotContextValue();
  const mergedContext = useMemo(() => ({ ...parentContext, ...context }), [parentContext, context]);

  const registrations = registry
    .getSlot(slot)
    .filter((reg) => !reg.condition || reg.condition(mergedContext));
  const sortedRegistrations = [...registrations].sort(
    (a, b) => (b.priority ?? 0) - (a.priority ?? 0),
  );

  const [ownTabId, setOwnTabId] = useState<string | null>(null);
  // The controlled `activeTab` wins. Otherwise use the tab the user picked,
  // and fall back to the highest-priority registration once one exists.
  const firstRegistration = sortedRegistrations[0];
  const activeTabId =
    activeTab ?? ownTabId ?? firstRegistration?.id ?? firstRegistration?.slot ?? null;

  const handleTabClick = useCallback(
    (registration: SlotRegistration) => {
      const tabId = registration.id ?? registration.slot;
      setOwnTabId(tabId);
      onTabChange?.(tabId);
    },
    [onTabChange],
  );

  const tabs = sortedRegistrations.map((registration) => {
    const tabId = registration.id ?? registration.slot;
    const isActive = activeTabId === tabId;
    const onClick = () => handleTabClick(registration);

    if (renderTab) {
      // The caller's renderer owns the markup; wrap it so React still gets a
      // stable list key for every tab.
      return createElement(Fragment, { key: tabId }, renderTab(registration, isActive, onClick));
    }

    return (
      <button
        key={tabId}
        type="button"
        role="tab"
        id={`slot-tab-${slot}-${tabId}`}
        aria-selected={isActive}
        aria-controls={`slot-panel-${slot}-${tabId}`}
        onClick={onClick}
        aria-label={registration.name ?? registration.id ?? registration.slot}
        className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold whitespace-nowrap transition ${
          isActive
            ? "border-border-subtle bg-surface-1 text-foreground shadow-xs"
            : "text-muted-foreground hover:bg-surface-1 hover:text-foreground border-transparent"
        }`}
      >
        {registration.icon ? <registration.icon size={13} className="shrink-0" /> : null}
        <span>{registration.name ?? registration.id ?? registration.slot}</span>
      </button>
    );
  });

  if (tabs.length === 0) {
    return <div className={className}>{fallback}</div>;
  }

  return (
    <div className={className}>
      <div
        role="tablist"
        aria-label={ariaLabel ?? `${slot} tabs`}
        className="scrollbar-hide flex min-w-0 flex-1 items-center gap-1 overflow-x-auto py-0.5"
      >
        {tabs}
      </div>
    </div>
  );
}

// ============================================================================
// SlotPanel Component - Renders the active panel for a slot
// ============================================================================

interface SlotPanelProps {
  slot: SlotName;
  fallback?: ReactNode;
  context?: Partial<SlotContextValue>;
  className?: string;
  /** Registration id to render. Defaults to the highest-priority contribution. */
  activeTab?: string;
}

export function SlotPanel({
  slot,
  fallback = null,
  context,
  className,
  activeTab,
}: SlotPanelProps): ReactElement {
  const registry = useModuleRegistry();
  useRegistryVersion();
  const parentContext = useSlotContextValue();
  const mergedContext = useMemo(() => ({ ...parentContext, ...context }), [parentContext, context]);

  const registrations = registry
    .getSlot(slot)
    .filter((reg) => !reg.condition || reg.condition(mergedContext));
  const sortedRegistrations = [...registrations].sort(
    (a, b) => (b.priority ?? 0) - (a.priority ?? 0),
  );

  const activeRegistration = activeTab
    ? sortedRegistrations.find(
        (registration) => (registration.id ?? registration.slot) === activeTab,
      )
    : sortedRegistrations[0];

  if (!activeRegistration) {
    return <div className={className}>{fallback}</div>;
  }

  const Component = activeRegistration.component;

  return (
    <div className={className}>
      <Component context={mergedContext} />
    </div>
  );
}
