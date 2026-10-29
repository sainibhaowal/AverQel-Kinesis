import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import DeepSpaceAgentModeMenu from "../app/dashboard/deepspace/_components/DeepSpaceAgentModeMenu";
import {
  filterEnabledAgentModes,
  type AgentMode,
} from "../app/dashboard/deepspace/_lib/agent-mode-api";

const MODES: AgentMode[] = [
  {
    id: "standard",
    name: "Standard",
    description: "Every tool the policy allows.",
    ptc_enabled: false,
    plan_mode_by_default: false,
    editable: false,
  },
  {
    id: "ptc",
    name: "Programmatic tool calling",
    description: "The agent writes one program that calls the tools.",
    ptc_enabled: true,
    plan_mode_by_default: false,
    editable: false,
  },
  {
    id: "minimal",
    name: "Minimal",
    description: "Read-only tools for a quick question.",
    ptc_enabled: false,
    plan_mode_by_default: false,
    editable: false,
  },
  {
    id: "creator",
    name: "Creator",
    description: "Notes, Library files, artifacts, and memory.",
    ptc_enabled: false,
    plan_mode_by_default: false,
    editable: false,
  },
];

/**
 * The agent mode menu shows every mode the deployment offers and lets the
 * user pick another one.
 */
describe("DeepSpaceAgentModeMenu", () => {
  it("filters modes hidden in the user's settings response", () => {
    expect(
      filterEnabledAgentModes(MODES, {
        enablement: [
          { preset_id: "ptc", enabled: false },
          { preset_id: "minimal", enabled: true },
        ],
      }).map((mode) => mode.id),
    ).toEqual(["standard", "minimal", "creator"]);
  });

  it("keeps every mode when the enablement response is unavailable", () => {
    expect(filterEnabledAgentModes(MODES, null)).toEqual(MODES);
  });

  it("shows the mode in force and lists the others", () => {
    render(<DeepSpaceAgentModeMenu modes={MODES} mode="standard" onModeChange={vi.fn()} />);

    const trigger = screen.getByRole("button", { name: "Agent mode" });
    expect(trigger).toHaveTextContent("Standard");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    fireEvent.click(trigger);

    const menu = screen.getByRole("menu", { name: "Agent mode" });
    expect(menu).toBeInTheDocument();
    expect(screen.getByRole("menuitemradio", { name: /Standard/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(
      screen.getByRole("menuitemradio", { name: /Programmatic tool calling/ }),
    ).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("Read-only tools for a quick question.")).toBeInTheDocument();
  });

  it("reports the mode the user picks", () => {
    const onModeChange = vi.fn();
    render(<DeepSpaceAgentModeMenu modes={MODES} mode="standard" onModeChange={onModeChange} />);

    fireEvent.click(screen.getByRole("button", { name: "Agent mode" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: /Creator/ }));

    expect(onModeChange).toHaveBeenCalledWith("creator");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("closes the menu when the user presses Escape", () => {
    render(<DeepSpaceAgentModeMenu modes={MODES} mode="standard" onModeChange={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Agent mode" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("offers nothing when the deployment lists no modes", () => {
    render(<DeepSpaceAgentModeMenu modes={[]} mode="" onModeChange={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Agent mode" })).toBeDisabled();
  });

  it("marks the default mode in the menu when provided", () => {
    render(
      <DeepSpaceAgentModeMenu
        modes={MODES}
        mode="standard"
        defaultMode="ptc"
        onModeChange={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Agent mode" }));
    expect(screen.getByText("Default")).toBeInTheDocument();
  });
});
