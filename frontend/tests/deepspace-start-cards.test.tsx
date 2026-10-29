import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import DeepSpaceStartCards from "../app/dashboard/deepspace/_components/DeepSpaceStartCards";

/**
 * The start cards are the three ways into a conversation's own tools: the
 * workspace files, a terminal, and the in-app browser.
 */
describe("DeepSpaceStartCards", () => {
  it("opens the workspace files, a terminal, and the browser", () => {
    const onOpenFiles = vi.fn();
    const onOpenTerminal = vi.fn();
    const onOpenBrowser = vi.fn();
    render(
      <DeepSpaceStartCards
        onOpenFiles={onOpenFiles}
        onOpenTerminal={onOpenTerminal}
        onOpenBrowser={onOpenBrowser}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Workspace files/ }));
    expect(onOpenFiles).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: /New terminal/ }));
    expect(onOpenTerminal).toHaveBeenCalledWith("bash");

    fireEvent.click(screen.getByRole("button", { name: /Browser/ }));
    expect(onOpenBrowser).toHaveBeenCalledOnce();
  });

  it("opens a terminal under the shell the user picks", () => {
    const onOpenTerminal = vi.fn();
    render(
      <DeepSpaceStartCards
        onOpenFiles={vi.fn()}
        onOpenTerminal={onOpenTerminal}
        onOpenBrowser={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Choose the terminal shell" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "pwsh" }));

    expect(onOpenTerminal).toHaveBeenCalledWith("pwsh");
  });

  it("keeps the picked shell for the next terminal", () => {
    const onOpenTerminal = vi.fn();
    render(
      <DeepSpaceStartCards
        onOpenFiles={vi.fn()}
        onOpenTerminal={onOpenTerminal}
        onOpenBrowser={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Choose the terminal shell" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "cmd" }));
    fireEvent.click(screen.getByRole("button", { name: /New terminal/ }));

    expect(onOpenTerminal).toHaveBeenLastCalledWith("cmd");
  });

  it("closes the shell menu when the user presses Escape", () => {
    render(
      <DeepSpaceStartCards
        onOpenFiles={vi.fn()}
        onOpenTerminal={vi.fn()}
        onOpenBrowser={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Choose the terminal shell" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("disables the cards when the conversation has no workspace yet", () => {
    const onOpenFiles = vi.fn();
    render(
      <DeepSpaceStartCards
        disabled
        onOpenFiles={onOpenFiles}
        onOpenTerminal={vi.fn()}
        onOpenBrowser={vi.fn()}
      />,
    );

    const files = screen.getByRole("button", { name: /Workspace files/ });
    expect(files).toBeDisabled();
    fireEvent.click(files);
    expect(onOpenFiles).not.toHaveBeenCalled();
  });
});
