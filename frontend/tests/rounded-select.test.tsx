import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import RoundedSelect from "../app/components/ui/RoundedSelect";

const OPTIONS = [
  { value: "standard", label: "Standard (default)" },
  { value: "ptc", label: "Programmatic tool calling" },
  { value: "minimal", label: "Minimal" },
  { value: "creator", label: "Creator" },
  { value: "custom", label: "Custom" },
];

describe("RoundedSelect", () => {
  it("renders a rounded custom listbox instead of a native option popup", () => {
    render(
      <RoundedSelect
        label="Session preset"
        value="standard"
        options={OPTIONS}
        onChange={vi.fn()}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Session preset" });
    expect(trigger).toHaveAttribute("aria-haspopup", "listbox");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    fireEvent.click(trigger);

    const listbox = screen.getByRole("listbox", { name: "Session preset" });
    expect(listbox).toHaveClass("rounded-xl");
    expect(screen.getByRole("option", { name: "Standard (default)" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("option", { name: "Custom" })).toBeInTheDocument();
  });

  it("reports the selected option and returns focus to the trigger", async () => {
    const onChange = vi.fn();
    render(
      <RoundedSelect
        label="Session preset"
        value="standard"
        options={OPTIONS}
        onChange={onChange}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Session preset" });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("option", { name: "Minimal" }));

    expect(onChange).toHaveBeenCalledWith("minimal");
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
    expect(document.activeElement).toBe(trigger);
  });
});
