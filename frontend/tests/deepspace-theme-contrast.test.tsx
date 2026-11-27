import fs from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import DeepSpaceApprovalCard from "../app/dashboard/deepspace/_components/DeepSpaceApprovalCard";
import DeepSpaceMediaArtifacts from "../app/dashboard/deepspace/_components/DeepSpaceMediaArtifacts";
import DeepSpaceQuestionCard from "../app/dashboard/deepspace/_components/DeepSpaceQuestionCard";
import DeepSpaceSourcesPanel from "../app/dashboard/deepspace/_components/DeepSpaceSourcesPanel";
import DeepSpaceThread from "../app/dashboard/deepspace/_components/DeepSpaceThread";
import { renderStructuredToolOutput } from "../app/dashboard/deepspace/_components/ToolRichPayload";

describe("DeepSpace light and dark theme contrast", () => {
  it("paints themed canvas backgrounds on fullscreen workspace surfaces in both light and dark modes", () => {
    const css = fs.readFileSync(path.resolve(process.cwd(), "app/globals.css"), "utf8");
    const pageClient = fs.readFileSync(
      path.resolve(process.cwd(), "app/dashboard/deepspace/_components/DeepSpacePageClient.tsx"),
      "utf8",
    );

    expect(css).toContain("html:not(.dark) .deepspace-workspace-surface:fullscreen");
    expect(css).toContain("html:not(.dark) .deepspace-workspace-surface:fullscreen::backdrop");
    expect(css).toContain(".dark .deepspace-workspace-surface:fullscreen");
    expect(css).toContain(".dark .deepspace-workspace-surface:fullscreen::backdrop");
    expect(pageClient).toContain("deepspace-workspace-surface");
  });

  it("uses dark:prose-invert rather than unconditional prose-invert on DeepSpace thread and markdown", () => {
    const threadSource = fs.readFileSync(
      path.resolve(process.cwd(), "app/dashboard/deepspace/_components/DeepSpaceThread.tsx"),
      "utf8",
    );
    const markdownSource = fs.readFileSync(
      path.resolve(
        process.cwd(),
        "app/dashboard/deepspace/_components/DeepSpaceMarkdownRenderer.tsx",
      ),
      "utf8",
    );

    expect(threadSource).not.toMatch(/prose\s+prose-invert/);
    expect(markdownSource).not.toContain('"prose-invert"');
    expect(threadSource).toContain("dark:prose-invert");
    expect(markdownSource).toContain("dark:prose-invert");
  });

  it("renders theme-paired classes on assistant markdown, metrics, footer actions, and sources button", () => {
    render(
      <DeepSpaceThread
        messages={[
          {
            id: "a-1",
            role: "assistant",
            status: "ready",
            content: "# Heading\n\n**Bold item** — [Example](https://example.com)",
            rawContent: "# Heading\n\n**Bold item** — [Example](https://example.com)",
            createdAt: "2026-11-25T22:00:00Z",
            metrics: {
              tokensPerSecond: 172.6,
              timeToFirstTokenMs: 4050,
              providerTimeToFirstOutputMs: 1450,
            },
          },
        ]}
        emptyPrompts={[]}
        onPromptSelect={() => {}}
        onInsertLatestAnswer={() => {}}
        onRegenerate={() => {}}
        onStartEdit={() => {}}
        onCancelEdit={() => {}}
        onUpdateDraft={() => {}}
        onSaveEdit={() => {}}
        onActivateVersion={() => {}}
      />,
    );

    const heading = screen.getByRole("heading", { name: "Heading" });
    expect(heading.className).toContain("text-slate-950");
    expect(heading.className).toContain("dark:text-cyan-50");

    const link = screen.getByRole("link", { name: "Example" });
    expect(link.className).toContain("text-teal-700");
    expect(link.className).toContain("dark:text-cyan-300");

    const speedPill = screen.getByText(/172\.6 tok\/s/i);
    expect(speedPill.className).toContain("bg-white/95");
    expect(speedPill.className).toContain("text-slate-700");
    expect(speedPill.className).toContain("dark:text-slate-300");
  });

  it("renders legible light and dark styles for sources, media artifact, approval, question, and tool panels", () => {
    const { container } = render(
      <>
        <DeepSpaceSourcesPanel
          sources={[{ id: "1", url: "https://example.com", title: "Example" }]}
        />
        <DeepSpaceMediaArtifacts
          artifacts={[
            {
              id: "art-1",
              kind: "document",
              title: "Generated summary",
              url: "/api/files/art-1",
              content_type: "text/plain",
              size_bytes: 256,
              status: "ready",
            },
          ]}
        />
        <DeepSpaceApprovalCard
          approvalId="app-1"
          toolName="bash"
          data={{ tool_input: { command: "echo hi", cwd: "/workspace" } }}
          onResolve={async () => {}}
        />
        <DeepSpaceQuestionCard
          question="Proceed with deploy?\n- Yes\n- No"
          options={[]}
          onSubmit={async () => {}}
        />
        {renderStructuredToolOutput("bash", JSON.stringify({ output: "ok", exit_code: 0 }))}
      </>,
    );

    const sourcesBtn = screen.getByRole("button", { name: /Open 1 cited source/i });
    expect(sourcesBtn.className).toContain("bg-white");
    expect(sourcesBtn.className).toContain("text-slate-800");
    expect(sourcesBtn.className).toContain("dark:bg-neutral-900");
    expect(sourcesBtn.className).toContain("dark:text-neutral-100");

    const html = container.innerHTML;
    expect(html).not.toContain("bg-slate-950/90 text-slate-100");
    expect(html).toContain("bg-slate-50 text-slate-900 dark:border-white/10 dark:bg-black/45");
    expect(html).toContain("bg-white/95 shadow-sm dark:border-cyan-300/20 dark:bg-[#091510]/80");
    expect(html).toContain("bg-slate-50/90 p-3");
  });
});
