import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { DeepSpaceSkillReference } from "../app/dashboard/deepspace/_components/DeepSpaceSkillReference";

const fetchWithAuth = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  fetchWithAuth,
}));

const SKILLS = [
  {
    name: "office",
    description: "Word, spreadsheet, and presentation workflows.",
    provider: "bundled",
  },
  {
    name: "review",
    description: "Review a change before delivery.",
    provider: "filesystem",
  },
  {
    name: "release-notes",
    description: "Draft release notes.",
    provider: "filesystem",
  },
];

function renderPicker(query: string, onSelect = vi.fn(), selectionStart?: number) {
  return render(
    <DeepSpaceSkillReference
      conversationId="conv-1"
      query={query}
      onSelect={onSelect}
      selectionStart={selectionStart ?? query.length}
    />,
  );
}

describe("DeepSpaceSkillReference", () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
    fetchWithAuth.mockResolvedValue({
      ok: true,
      json: async () => SKILLS,
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("loads the workspace skills and filters them against the typed token", async () => {
    renderPicker("@rev");

    expect(await screen.findByText("review")).toBeInTheDocument();
    expect(screen.queryByText("release-notes")).not.toBeInTheDocument();
    expect(screen.queryByText("office")).not.toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByText("filesystem")).toBeInTheDocument();
    expect(fetchWithAuth).toHaveBeenCalledWith(
      "/deepspace/chats/conv-1/skills?limit=100",
      { method: "GET" },
    );
  });

  it("inserts the slash command that the backend expands", async () => {
    const onSelect = vi.fn();
    renderPicker("@", onSelect);

    fireEvent.click(await screen.findByText("office"));

    expect(onSelect).toHaveBeenCalledWith("/office ");
  });

  it("replaces only the command token and preserves request text", async () => {
    const onSelect = vi.fn();
    renderPicker("@re fix the bug", onSelect, 3);

    fireEvent.click(await screen.findByText("review"));

    expect(onSelect).toHaveBeenCalledWith("/review fix the bug");
  });

  it("supports arrow navigation, Enter, and Tab selection", async () => {
    const onSelect = vi.fn();
    renderPicker("@", onSelect);
    await screen.findByText("office");

    fireEvent.keyDown(window, { key: "ArrowDown" });
    fireEvent.keyDown(window, { key: "Enter" });
    expect(onSelect).toHaveBeenLastCalledWith("/review ");

    onSelect.mockClear();
    fireEvent.keyDown(window, { key: "ArrowUp" });
    fireEvent.keyDown(window, { key: "Tab" });
    expect(onSelect).toHaveBeenLastCalledWith("/office ");
  });

  it("dismisses with Escape and reopens when the token changes", async () => {
    const { rerender } = renderPicker("@");
    await screen.findByText("office");

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    rerender(
      <DeepSpaceSkillReference
        conversationId="conv-1"
        query="@d"
        onSelect={vi.fn()}
        selectionStart={2}
      />,
    );
    expect(await screen.findByRole("listbox")).toBeInTheDocument();
  });

  it("does not offer a command embedded in prose", async () => {
    renderPicker("please use @d");

    await waitFor(() => expect(fetchWithAuth).toHaveBeenCalled());
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("renders nothing without a conversation and does not fetch", () => {
    render(
      <DeepSpaceSkillReference
        conversationId={null}
        query="@"
        onSelect={vi.fn()}
        selectionStart={1}
      />,
    );
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(fetchWithAuth).not.toHaveBeenCalled();
  });

  it("shows an empty state when no skill matches", async () => {
    renderPicker("@zzz");
    expect(await screen.findByText("No skill matches this text.")).toBeInTheDocument();
  });
});
