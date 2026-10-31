import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { render, screen, within } from "@testing-library/react";
import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

import DeepSpaceManual from "../app/documentation/_components/DeepSpaceManual";
import {
  readDeepSpaceManual,
  readDeepSpaceManualFrom,
} from "../app/documentation/deepspace-manual/manual-data";

const { searchParamsMock } = vi.hoisted(() => ({
  searchParamsMock: { value: new URLSearchParams() },
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => searchParamsMock.value,
}));

const manual = readDeepSpaceManual();
const temporaryFolders: string[] = [];

function writeManualFolder(files: Record<string, string>): string {
  const folder = mkdtempSync(path.join(tmpdir(), "deepspace-manual-"));
  temporaryFolders.push(folder);
  for (const [name, content] of Object.entries(files)) {
    writeFileSync(path.join(folder, name), content, "utf8");
  }
  return folder;
}

function renderManual() {
  return render(<DeepSpaceManual chapters={manual.chapters} notices={manual.notices} />);
}

afterAll(() => {
  for (const folder of temporaryFolders) {
    rmSync(folder, { recursive: true, force: true });
  }
});

describe("the DeepSpace manual", () => {
  beforeEach(() => {
    searchParamsMock.value = new URLSearchParams();
  });

  it("reads every chapter the manual's table of contents names", () => {
    expect(manual.notices).toEqual([]);
    expect(manual.chapters).toHaveLength(41);
    expect(manual.chapters[0].slug).toBe("readme");
    expect(manual.chapters[0].title).toBe("The DeepSpace manual");
    expect(manual.chapters[1].slug).toBe("01-what-deepspace-is");
    expect(manual.chapters[1].body).toContain("## Use case");
    expect(manual.chapters.every((chapter) => chapter.available)).toBe(true);
  });

  it("lists the chapters with their titles and links to their own route", () => {
    renderManual();

    const list = screen.getByRole("navigation", { name: /manual chapters/i });
    const links = within(list).getAllByRole("link");
    expect(links).toHaveLength(41);
    expect(links[1]).toHaveTextContent("What DeepSpace is");
    expect(links[1]).toHaveAttribute(
      "href",
      "/documentation/deepspace-manual?chapter=01-what-deepspace-is",
    );
    expect(links.at(-1)).toHaveAttribute(
      "href",
      "/documentation/deepspace-manual?chapter=41-local-searxng",
    );
  });

  it("renders the selected chapter's title, sections, lists, code, and tables", () => {
    searchParamsMock.value = new URLSearchParams("chapter=01-what-deepspace-is");
    renderManual();

    expect(screen.getByRole("heading", { level: 2, name: "What DeepSpace is" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Use case" })).toBeInTheDocument();

    const tables = screen.getAllByRole("table");
    expect(tables.length).toBeGreaterThan(0);
    expect(within(tables[0]).getByText("The user")).toBeInTheDocument();

    expect(screen.getAllByRole("list").length).toBeGreaterThan(0);
  });

  it("gives every heading the anchor the chapter's own contents list links to", () => {
    searchParamsMock.value = new URLSearchParams("chapter=01-what-deepspace-is");
    renderManual();

    const contents = screen.getByRole("heading", { level: 3, name: "Contents" });
    const list = contents.nextElementSibling as HTMLElement;
    const anchors = Array.from(list.querySelectorAll("a")).map((anchor) =>
      anchor.getAttribute("href"),
    );
    expect(anchors.length).toBeGreaterThan(0);
    for (const anchor of anchors) {
      expect(anchor).toMatch(/^#/);
      expect(document.querySelector(`[id="${anchor?.slice(1)}"]`)).not.toBeNull();
    }
  });

  it("marks the chapter the reader is on", () => {
    searchParamsMock.value = new URLSearchParams("chapter=03-parallel-execution");
    renderManual();

    const list = screen.getByRole("navigation", { name: /manual chapters/i });
    const current = within(list).getByRole("link", { name: /Parallel execution/ });
    expect(current).toHaveAttribute("aria-current", "page");
  });

  it("says plainly that a chapter does not exist", () => {
    searchParamsMock.value = new URLSearchParams("chapter=99-not-a-chapter");
    renderManual();

    expect(screen.getByText(/There is no chapter called/)).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: /manual chapters/i })).toBeInTheDocument();
  });

  it("renders a code block as code", () => {
    searchParamsMock.value = new URLSearchParams("chapter=12-hooks");
    renderManual();

    expect(document.querySelectorAll("pre code").length).toBeGreaterThan(0);
  });

  it("reports a chapter whose file is missing instead of breaking", () => {
    const folder = writeManualFolder({
      "README.md": [
        "# The DeepSpace manual",
        "",
        "## The chapters",
        "",
        "| # | Chapter | What it covers |",
        "|---|---|---|",
        "| 01 | [Present](01-present.md) | A chapter that is there. |",
        "| 02 | [Missing](02-missing.md) | A chapter whose file is not there. |",
        "",
      ].join("\n"),
      "01-present.md": "# 01. Present\n\n## Use case\n\nIt is here.\n",
    });

    const partial = readDeepSpaceManualFrom(folder);
    expect(partial.chapters).toHaveLength(3);
    expect(partial.chapters[1].available).toBe(true);
    expect(partial.chapters[2].available).toBe(false);
    expect(partial.notices.join(" ")).toContain("02-missing.md");

    searchParamsMock.value = new URLSearchParams("chapter=02-missing");
    render(<DeepSpaceManual chapters={partial.chapters} notices={partial.notices} />);
    expect(screen.getAllByText(/is not in the manual folder/).length).toBeGreaterThan(0);
  });

  it("falls back to the files in the folder when the table of contents is missing", () => {
    const folder = writeManualFolder({
      "01-only.md": "# 01. Only\n\nThe one chapter.\n",
    });

    const fallback = readDeepSpaceManualFrom(folder);
    expect(fallback.chapters).toHaveLength(1);
    expect(fallback.chapters[0].title).toBe("01. Only");
    expect(fallback.notices.join(" ")).toContain("README.md could not be read");
  });

  it("says plainly that the manual folder is missing", () => {
    const missing = readDeepSpaceManualFrom(null);
    expect(missing.chapters).toEqual([]);
    expect(missing.notices.join(" ")).toContain("cannot be shown");
  });
});
