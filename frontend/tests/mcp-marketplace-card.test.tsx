import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import MCPMarketplaceCard, {
  resolveTrustedLogoPath,
} from "../app/dashboard/mcp/_components/MCPMarketplaceCard";
import { marketplaceEntry } from "./mcp-test-fixtures";

describe("MCPMarketplaceCard", () => {
  it("renders community badges, transport/auth, preview, and trusted logo path", () => {
    render(<MCPMarketplaceCard entry={marketplaceEntry} onConnect={vi.fn()} />);
    expect(screen.getByText("Community")).toBeInTheDocument();
    expect(screen.getByText("Remote HTTP")).toBeInTheDocument();
    expect(screen.getByText("OAuth")).toBeInTheDocument();
    expect(screen.getByText("search_mail")).toBeInTheDocument();
    expect(resolveTrustedLogoPath("google")).toBe("/mcp/google.svg");
    expect(resolveTrustedLogoPath("google-gmail")).toBe("/mcp/gmail.svg");
    expect(resolveTrustedLogoPath("gmail")).toBe("/mcp/gmail.svg");
    expect(resolveTrustedLogoPath("google-drive")).toBe("/mcp/google-drive.svg");
    expect(resolveTrustedLogoPath("google-calendar")).toBe("/mcp/google-calendar.svg");
    expect(resolveTrustedLogoPath("google-docs")).toBe("/mcp/google-docs.svg");
    expect(resolveTrustedLogoPath("google-sheets")).toBe("/mcp/google-sheets.svg");
    expect(resolveTrustedLogoPath("google-chat")).toBe("/mcp/google-chat.svg");
    expect(resolveTrustedLogoPath("google-people")).toBe("/mcp/google-people.svg");
    expect(resolveTrustedLogoPath("notion")).toBe("/mcp/notion.svg");
    expect(resolveTrustedLogoPath("slack")).toBe("/mcp/slack.svg");
    expect(resolveTrustedLogoPath("linear")).toBe("/mcp/linear.svg");
    expect(resolveTrustedLogoPath("jira")).toBe("/mcp/jira.svg");
    expect(resolveTrustedLogoPath("discord")).toBe("/mcp/discord.svg");
    expect(resolveTrustedLogoPath("airtable")).toBe("/mcp/airtable.svg");
    expect(resolveTrustedLogoPath("hubspot")).toBe("/mcp/hubspot.svg");
    expect(resolveTrustedLogoPath("git")).toBe("/mcp/git.svg");
    expect(resolveTrustedLogoPath("sqlite")).toBe("/mcp/sqlite.svg");
    expect(resolveTrustedLogoPath("puppeteer")).toBe("/mcp/puppeteer.svg");
    expect(resolveTrustedLogoPath("https://evil.example/logo.svg")).toBeNull();
  });
});
