import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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

  it("portals the shell menu and keeps it inside the viewport near the top edge", async () => {
    const bounds = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (this.getAttribute("aria-label") === "Choose the terminal shell") {
          return new DOMRect(370, 10, 28, 28);
        }
        if (this.getAttribute("role") === "menu") return new DOMRect(0, 0, 128, 76);
        return new DOMRect(0, 0, 0, 0);
      });
    const originalWidth = Object.getOwnPropertyDescriptor(window, "innerWidth");
    const originalHeight = Object.getOwnPropertyDescriptor(window, "innerHeight");
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 400 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });
    render(
      <DeepSpaceStartCards
        onOpenFiles={vi.fn()}
        onOpenTerminal={vi.fn()}
        onOpenBrowser={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Choose the terminal shell" }));

    const menu = await screen.findByRole("menu", { name: "Terminal shell" });
    await waitFor(() => expect(menu).toHaveStyle({ position: "fixed", top: "42px" }));
    const left = Number.parseFloat(menu.style.left);
    const width = menu.getBoundingClientRect().width;
    expect(left).toBeGreaterThanOrEqual(8);
    expect(left + width).toBeLessThanOrEqual(window.innerWidth - 8);
    expect(menu.parentElement).toBe(document.body);
    bounds.mockRestore();
    if (originalWidth) Object.defineProperty(window, "innerWidth", originalWidth);
    else Reflect.deleteProperty(window, "innerWidth");
    if (originalHeight) Object.defineProperty(window, "innerHeight", originalHeight);
    else Reflect.deleteProperty(window, "innerHeight");
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
