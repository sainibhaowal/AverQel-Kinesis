import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import DeepSpaceComposer from "../app/dashboard/deepspace/_components/DeepSpaceComposer";

function StatefulComposer({
  promptHistory,
  initialQuery = "",
  conversationId = "conversation-a",
  onSubmit = vi.fn(),
}: {
  promptHistory: string[];
  initialQuery?: string;
  conversationId?: string;
  onSubmit?: () => void;
}) {
  const [query, setQuery] = useState(initialQuery);
  return (
    <DeepSpaceComposer
      conversationId={conversationId}
      promptHistory={promptHistory}
      query={query}
      isStreaming={false}
      onQueryChange={setQuery}
      onSubmit={onSubmit}
      onStop={vi.fn()}
    />
  );
}

describe("DeepSpace composer input behavior", () => {
  it("restores textarea focus after sending with the button", () => {
    const onSubmit = vi.fn();
    render(<StatefulComposer promptHistory={[]} initialQuery="Next prompt" onSubmit={onSubmit} />);
    const textarea = screen.getByPlaceholderText("Message DeepSpace...") as HTMLTextAreaElement;
    const sendButton = screen.getByRole("button", { name: "Send message" });

    sendButton.focus();
    fireEvent.click(sendButton);

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(textarea);
  });

  it("cycles through sent prompts with Up and Down and restores the unsent draft", () => {
    render(
      <StatefulComposer
        promptHistory={["first sent prompt", "second sent prompt"]}
        initialQuery="unsent draft"
      />,
    );
    const textarea = screen.getByPlaceholderText("Message DeepSpace...") as HTMLTextAreaElement;
    textarea.focus();
    textarea.setSelectionRange(0, 0);

    fireEvent.keyDown(textarea, { key: "ArrowUp" });
    expect(textarea).toHaveValue("second sent prompt");
    expect(textarea.selectionStart).toBe("second sent prompt".length);

    fireEvent.keyDown(textarea, { key: "ArrowUp" });
    expect(textarea).toHaveValue("first sent prompt");

    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    expect(textarea).toHaveValue("second sent prompt");

    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    expect(textarea).toHaveValue("unsent draft");
    expect(textarea.selectionStart).toBe("unsent draft".length);
  });

  it("leaves normal multiline arrow editing alone away from the first line", () => {
    render(
      <StatefulComposer
        promptHistory={["previous prompt"]}
        initialQuery={"first line\nsecond line"}
      />,
    );
    const textarea = screen.getByPlaceholderText("Message DeepSpace...") as HTMLTextAreaElement;
    textarea.focus();
    textarea.setSelectionRange("first line\n".length + 3, "first line\n".length + 3);

    fireEvent.keyDown(textarea, { key: "ArrowUp" });

    expect(textarea).toHaveValue("first line\nsecond line");
  });

  it("uses the newly selected conversation's prompt history", () => {
    const view = render(<StatefulComposer promptHistory={["conversation A prompt"]} />);
    view.rerender(
      <StatefulComposer
        conversationId="conversation-b"
        promptHistory={["conversation B prompt"]}
      />,
    );
    const textarea = screen.getByPlaceholderText("Message DeepSpace...") as HTMLTextAreaElement;
    textarea.focus();

    fireEvent.keyDown(textarea, { key: "ArrowUp" });

    expect(textarea).toHaveValue("conversation B prompt");
  });
});
