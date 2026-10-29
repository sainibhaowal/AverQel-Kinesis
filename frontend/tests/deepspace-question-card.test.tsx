import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import DeepSpaceQuestionCard, {
  parseQuestionContent,
} from "../app/dashboard/deepspace/_components/DeepSpaceQuestionCard";

describe("DeepSpaceQuestionCard", () => {
  const screenshotPrompt =
    "I tried to open the interactive question prompt, but that tool (`ask_user`) isn't in this runtime's authorized catalog — discovery returned only document-comparison tools. So I'll ask here instead, in the same one-question form: **What would you like me to do?** - **(a) Capability rundown** — a precise list of what I can actually do here right now, verified against the live tool catalog - **(b) Workspace inspection** — look at `/home/ravi/DeepSpace/Project` and report what's in it (files, language, size, structure) - **(c) Research** — find and summarise information on a topic you name - **(d) Build or change something** — you describe the outcome, I write a plan, you approve it, then I execute - **(e) Something else** — say it in a sentence One caveat worth stating up front: earlier in this conversation web search was failing (`SearXNG endpoint could not be resolved`), so option (c) may be limited to reading pages directly rather than keyword search. I'd confirm that before promising results. Pick a letter — or just tell me the outcome you want.";

  it("parses single-line raw markdown questions into prompt, options, and caveat footer", () => {
    const parsed = parseQuestionContent(screenshotPrompt);
    expect(parsed.prompt).toContain("What would you like me to do?");
    expect(parsed.options).toHaveLength(5);
    expect(parsed.options[0].badge).toBe("A");
    expect(parsed.options[0].title).toBe("Capability rundown");
    expect(parsed.options[1].badge).toBe("B");
    expect(parsed.options[1].title).toBe("Workspace inspection");
    expect(parsed.options[4].badge).toBe("E");
    expect(parsed.options[4].title).toBe("Something else");
    expect(parsed.footer).toContain("One caveat worth stating up front");
  });

  it("renders the screenshot prompt with parsed Markdown and interactive option buttons", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <DeepSpaceQuestionCard
        question={screenshotPrompt}
        options={[]}
        onSubmit={onSubmit}
      />,
    );

    // Verify card header and badges
    expect(screen.getByLabelText("DeepSpace question")).toBeInTheDocument();
    expect(screen.getByText("Your input is needed")).toBeInTheDocument();
    expect(screen.getByText("Waiting for you")).toBeInTheDocument();

    // Verify parsed prompt markdown (no raw asterisks around What would you like me to do?)
    expect(screen.getByText("What would you like me to do?")).toBeInTheDocument();
    expect(screen.getByText("ask_user")).toBeInTheDocument();

    // Verify parsed options buttons
    expect(screen.getByText("Capability rundown")).toBeInTheDocument();
    expect(screen.getByText("Workspace inspection")).toBeInTheDocument();
    expect(screen.getByText("Research")).toBeInTheDocument();
    expect(screen.getByText("Build or change something")).toBeInTheDocument();
    expect(screen.getByText("Something else")).toBeInTheDocument();

    // Verify first option shows Recommended badge
    expect(screen.getByText("Recommended")).toBeInTheDocument();

    // Verify caveat callout footer is rendered with its markdown code pill
    expect(screen.getByText(/One caveat worth stating up front/i)).toBeInTheDocument();
    expect(screen.getByText("SearXNG endpoint could not be resolved")).toBeInTheDocument();

    // Click option A
    fireEvent.click(screen.getByRole("button", { name: /Recommended: \(a\) Capability rundown/i }));
    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith("(a) Capability rundown");
    });
  });

  it("renders explicit options when provided via props", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <DeepSpaceQuestionCard
        question="Which export format would you prefer?"
        options={["Markdown", "JSON format", "Plain text"]}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByText("Which export format would you prefer?")).toBeInTheDocument();
    expect(screen.getByText("Recommended")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recommended: Markdown" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Choose: JSON format" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Choose: JSON format" }));
    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith("JSON format");
    });
  });

  it("allows writing and submitting a custom answer", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <DeepSpaceQuestionCard
        question="Which export format would you prefer?"
        options={["Markdown", "JSON"]}
        onSubmit={onSubmit}
      />,
    );

    // Expand custom answer
    const toggleButton = screen.getByRole("button", { name: "Write a different answer" });
    fireEvent.click(toggleButton);

    const textarea = screen.getByLabelText("Write your answer");
    fireEvent.change(textarea, { target: { value: "CSV format please" } });

    const submitBtn = screen.getByRole("button", { name: "Send answer" });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith("CSV format please");
    });
  });
});
