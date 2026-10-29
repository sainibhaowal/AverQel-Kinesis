import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import DeepSpaceComposer from "../app/dashboard/deepspace/_components/DeepSpaceComposer";

/**
 * The composer's workspace-panel toggle opens the panel that shows the
 * workspace files, a terminal, and the in-app browser.
 */
describe("DeepSpaceComposer workspace panel toggle", () => {
  it("offers the toggle and reports the state", () => {
    const onSidePanelToggle = vi.fn();
    render(
      <DeepSpaceComposer
        query="Ship the feature"
        conversationId="conv-1"
        isStreaming={false}
        onQueryChange={vi.fn()}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
        sidePanelOpen={false}
        onSidePanelToggle={onSidePanelToggle}
      />,
    );

    const toggle = screen.getByRole("button", { name: "Show the workspace panel" });
    expect(toggle).toBeInTheDocument();
    expect(toggle.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(toggle);
    expect(onSidePanelToggle).toHaveBeenCalledOnce();
  });

  it("marks the toggle as pressed while the panel is open", () => {
    render(
      <DeepSpaceComposer
        query="Ship the feature"
        conversationId="conv-1"
        isStreaming={false}
        onQueryChange={vi.fn()}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
        sidePanelOpen={true}
        onSidePanelToggle={vi.fn()}
      />,
    );

    const toggle = screen.getByRole("button", { name: "Hide the workspace panel" });
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
  });
});
