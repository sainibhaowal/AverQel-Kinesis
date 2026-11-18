import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ModuleRegistryProvider,
  SlotOutlet,
  SlotPanel,
  SlotTabs,
  useModuleRegistry,
} from "../app/dashboard/deepspace/_lib/module-registry";
import type { ModuleManifest } from "../app/dashboard/deepspace/_lib/module-registry";

function AlphaPanel({ context }: { context?: { conversationId: string | null } }) {
  return <p>alpha panel for {context?.conversationId ?? "none"}</p>;
}

const ALPHA_MODULE: ModuleManifest = {
  id: "module-alpha",
  name: "Alpha",
  version: "1.0.0",
  provides: [
    {
      slot: "panel-tab",
      id: "alpha",
      name: "Alpha",
      label: "Alpha",
      component: () => <p>alpha tab</p>,
      priority: 5,
    },
    {
      slot: "toolbar",
      id: "alpha-action",
      name: "Alpha action",
      component: () => <button type="button">Alpha action</button>,
    },
  ],
};

function RegisterButton({ manifest }: { manifest: ModuleManifest }) {
  const registry = useModuleRegistry();
  return (
    <button type="button" onClick={() => registry.registerModule(manifest)}>
      register
    </button>
  );
}

describe("module registry", () => {
  it("renders a module registered after the slot mounted", () => {
    render(
      <ModuleRegistryProvider>
        <SlotTabs slot="panel-tab" fallback={<p>no tabs</p>} />
        <RegisterButton manifest={ALPHA_MODULE} />
      </ModuleRegistryProvider>,
    );

    expect(screen.getByText("no tabs")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "register" }));

    expect(screen.getByRole("tab", { name: "Alpha" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Alpha" })).toHaveAttribute(
      "id",
      "slot-tab-panel-tab-alpha",
    );
    expect(screen.getByRole("tab", { name: "Alpha" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("removes and restores contributions when a module is disabled and enabled", () => {
    function Toggle() {
      const registry = useModuleRegistry();
      return (
        <>
          <button type="button" onClick={() => registry.disableModule("module-alpha")}>
            disable
          </button>
          <button type="button" onClick={() => registry.enableModule("module-alpha")}>
            enable
          </button>
        </>
      );
    }

    render(
      <ModuleRegistryProvider initialModules={[ALPHA_MODULE]}>
        <SlotTabs slot="panel-tab" fallback={<p>no tabs</p>} />
        <Toggle />
      </ModuleRegistryProvider>,
    );

    expect(screen.getByRole("tab", { name: "Alpha" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "disable" }));
    expect(screen.queryByRole("tab", { name: "Alpha" })).not.toBeInTheDocument();
    expect(screen.getByText("no tabs")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "enable" }));
    expect(screen.getByRole("tab", { name: "Alpha" })).toBeInTheDocument();
  });

  it("renders the selected registration in SlotPanel with the slot context", () => {
    render(
      <ModuleRegistryProvider
        initialModules={[
          {
            ...ALPHA_MODULE,
            provides: [
              {
                slot: "panel-tab",
                id: "alpha",
                name: "Alpha",
                component: AlphaPanel,
              },
            ],
          },
        ]}
        initialContext={{ conversationId: "conv-42" }}
      >
        <SlotPanel slot="panel-tab" activeTab="alpha" />
      </ModuleRegistryProvider>,
    );

    expect(screen.getByText("alpha panel for conv-42")).toBeInTheDocument();
  });

  it("renders toolbar contributions through SlotOutlet", () => {
    render(
      <ModuleRegistryProvider initialModules={[ALPHA_MODULE]}>
        <SlotOutlet slot="toolbar" fallback={<span>empty toolbar</span>} />
      </ModuleRegistryProvider>,
    );

    expect(screen.getByRole("button", { name: "Alpha action" })).toBeInTheDocument();
  });

  it("rejects slot use outside the provider", () => {
    expect(() => render(<SlotTabs slot="panel-tab" />)).toThrow(
      "useModuleRegistry must be used within a ModuleRegistryProvider",
    );
  });
});
