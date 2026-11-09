import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ThemeProvider } from "../app/context/ThemeContext";
import DeepSpaceMarkdownRenderer from "../app/dashboard/deepspace/_components/DeepSpaceMarkdownRenderer";
import { extractDeepSpaceSources } from "../app/dashboard/deepspace/_components/DeepSpaceSourcesPanel";
import type { TimelineStep } from "../app/dashboard/deepspace/_lib/deepspace-stream";

const resultSnippet = "Fresh search evidence returned by the provider. ".repeat(8);
const fetchedSnippet = "Fetched page text returned by the URL reader. ".repeat(8);

const researchTimeline: TimelineStep[] = [
  {
    id: "search-result",
    stepId: "search-result",
    turnIndex: 0,
    phase: "exploring",
    type: "tool_call",
    title: "Web Search",
    status: "completed",
    startedAt: "2026-11-06T10:00:00.000Z",
    toolName: "web_search",
    toolOutput: JSON.stringify({
      citations: [
        {
          id: 1,
          title: "Example search title",
          url: "https://example.org/report",
          snippet: resultSnippet,
          favicon: "https://example.org/favicon.ico",
          published_date: "2026-11-05",
        },
        {
          id: 2,
          title: "Another search title",
          url: "https://news.example.net/item",
          snippet: resultSnippet,
          favicon: "https://news.example.net/favicon.ico",
        },
      ],
    }),
  },
  {
    id: "url-read-result",
    stepId: "url-read-result",
    turnIndex: 0,
    phase: "exploring",
    type: "tool_call",
    title: "Read URL",
    status: "completed",
    startedAt: "2026-11-06T10:00:01.000Z",
    toolName: "url_read",
    toolOutput: JSON.stringify({
      citations: [
        {
          title: "Verified page title",
          url: "https://news.example.net/item",
          snippet: fetchedSnippet,
          retrieval_status: "read_full",
        },
      ],
    }),
  },
];

const answerWithSources = `Research summary.\n\n### Sources\n\n[R1] [Example report](https://example.org/report) — search snippet only\n[R2] [Another page](https://news.example.net/item) — read in full`;

describe("DeepSpace source drawer", () => {
  it("replaces source paragraphs with a capsule and opens the saved result metadata", async () => {
    const extracted = extractDeepSpaceSources(answerWithSources, researchTimeline);
    expect(extracted.content).toBe("Research summary.");
    expect(extracted.sources).toHaveLength(2);
    expect(extracted.sources[0]).toMatchObject({
      title: "Example report",
      url: "https://example.org/report",
      snippet: resultSnippet,
      favicon: "https://example.org/favicon.ico",
      retrievalStatus: "search_snippet_only",
    });
    expect(extracted.sources[1]).toMatchObject({
      title: "Verified page title",
      url: "https://news.example.net/item",
      snippet: fetchedSnippet,
      retrievalStatus: "read_full",
    });

    render(<DeepSpaceMarkdownRenderer content={answerWithSources} timeline={researchTimeline} />, {
      wrapper: ThemeProvider,
    });

    expect(screen.queryByRole("heading", { name: "Sources" })).not.toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: "Open 2 cited sources" });
    fireEvent.click(trigger);

    const panel = screen.getByRole("dialog", { name: "Sources" });
    expect(within(panel).getByText("example.org")).toBeInTheDocument();
    expect(within(panel).getByText("Verified page title")).toBeInTheDocument();
    expect(within(panel).getByText("Search snippet")).toBeInTheDocument();
    expect(within(panel).getByText("Read in full")).toBeInTheDocument();
    expect(panel.querySelector("img")).toHaveAttribute("src", "https://example.org/favicon.ico");

    const showMoreButtons = within(panel).getAllByRole("button", { name: "Show more" });
    fireEvent.click(showMoreButtons[0]!);
    expect(showMoreButtons[0]).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(within(panel).getByRole("button", { name: "Close sources panel" }));
    await waitFor(() => expect(document.getElementById(panel.id)).not.toBeInTheDocument());
  });

  it("keeps malformed source sections in Markdown instead of opening unsafe URLs", () => {
    const markdown = "Answer.\n\n### Sources\n\n[R1] [Unsafe](javascript:alert(1))";
    const extracted = extractDeepSpaceSources(markdown);

    expect(extracted.sources).toEqual([]);
    expect(extracted.content).toBe(markdown);
  });
});
