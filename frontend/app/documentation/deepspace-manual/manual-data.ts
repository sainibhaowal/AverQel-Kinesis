import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

import type { ManualChapter } from "../_components/DeepSpaceManual";

/** The route the manual is read on. The chapter query parameter selects a chapter. */
const MANUAL_ROUTE = "/documentation/deepspace-manual";

/** The manual's folder, relative to the repository root. */
const MANUAL_FOLDER = path.join("backend", "docs", "deepspace");

/** The manual's table of contents. It is the one place the series is described. */
const README_NAME = "README.md";

/** The chapter the manual opens on. */
const OVERVIEW_SLUG = "readme";

interface TableOfContentsRow {
  number: string;
  title: string;
  file: string;
  summary: string;
}

export interface DeepSpaceManualData {
  chapters: ManualChapter[];
  notices: string[];
}

function readTextFile(filePath: string): string | null {
  try {
    return readFileSync(filePath, "utf8");
  } catch {
    return null;
  }
}

function isDirectory(directoryPath: string): boolean {
  try {
    return statSync(directoryPath).isDirectory();
  } catch {
    return false;
  }
}

/** The manual folder, wherever the build runs from. */
function manualDirectory(): string | null {
  const candidates = [
    path.resolve(process.cwd(), "..", MANUAL_FOLDER),
    path.resolve(process.cwd(), MANUAL_FOLDER),
  ];
  for (const candidate of candidates) {
    // The manual lives beside the frontend, not inside it, so the build must
    // not trace the whole project for it. The chapters are read while the
    // page is rendered, and the rendered page carries them.
    if (existsSync(/* turbopackIgnore: true */ candidate) && isDirectory(candidate)) {
      return candidate;
    }
  }
  return null;
}

function firstHeading(markdown: string): string | null {
  const heading = /^#{1,6}\s+(.+)$/m.exec(markdown);
  return heading ? heading[1].trim() : null;
}

/** Drops the title line, because the page header already carries the title. */
function withoutTitle(markdown: string): string {
  return markdown.replace(/^\s*#{1,6}\s+[^\n]*\n+/, "").trim();
}

/** Drops the table of contents, because the page shows the chapters as a list. */
function withoutTableOfContents(markdown: string): string {
  return markdown
    .replace(/^##\s+The chapters\s*$/m, "")
    .replace(/^\|.*\|\s*$/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** The chapters the manual's table of contents lists. */
function parseTableOfContents(readme: string): {
  rows: TableOfContentsRow[];
  unreadable: number;
} {
  const rows: TableOfContentsRow[] = [];
  let unreadable = 0;

  for (const line of readme.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("|")) continue;

    const cells = trimmed
      .split("|")
      .slice(1, -1)
      .map((cell) => cell.trim());
    const number = cells[0] ?? "";
    if (!number || /^[-: ]+$/.test(number) || number === "#") continue;

    const link = /^\[([^\]]*)\]\(([^)]+)\)$/.exec(cells[1] ?? "");
    if (!link) {
      unreadable += 1;
      continue;
    }

    rows.push({
      number,
      title: link[1].trim(),
      file: link[2].trim(),
      summary: cells.slice(2).join(" | "),
    });
  }

  return { rows, unreadable };
}

/** The chapters in the manual folder, used when the table of contents is unreadable. */
function chaptersFromDisk(directory: string): TableOfContentsRow[] {
  let files: string[] = [];
  try {
    files = readdirSync(directory)
      .filter((name) => name.endsWith(".md") && name !== README_NAME)
      .sort();
  } catch {
    return [];
  }

  return files.map((file) => {
    const slug = file.replace(/\.md$/, "");
    const body = readTextFile(path.join(directory, file)) ?? "";
    return {
      number: /^(\d+)/.exec(slug)?.[1] ?? "",
      title: firstHeading(body) ?? slug,
      file,
      summary: "",
    };
  });
}

/**
 * Reads the manual from the repository: the table of contents, then every
 * chapter it names. A chapter whose file is missing is reported, not hidden.
 */
export function readDeepSpaceManual(): DeepSpaceManualData {
  return readDeepSpaceManualFrom(manualDirectory());
}

/** Reads the manual from one folder, so a missing chapter can be exercised. */
export function readDeepSpaceManualFrom(directory: string | null): DeepSpaceManualData {
  const notices: string[] = [];

  if (!directory) {
    return {
      chapters: [],
      notices: [
        `The DeepSpace manual was not found in ${MANUAL_FOLDER}, so it cannot be shown. It is read from the repository when the app is built.`,
      ],
    };
  }

  const readme = readTextFile(path.join(directory, README_NAME));
  const chapters: ManualChapter[] = [];

  if (readme === null) {
    notices.push(
      `${README_NAME} could not be read, so the chapter list was taken from the files in the manual folder.`,
    );
  } else {
    chapters.push({
      slug: OVERVIEW_SLUG,
      number: "",
      title: firstHeading(readme) ?? "The DeepSpace manual",
      summary: "How the manual is organised, and where to start reading.",
      href: MANUAL_ROUTE,
      body: withoutTableOfContents(withoutTitle(readme)),
      available: true,
      problem: null,
    });
  }

  const { rows, unreadable } = readme
    ? parseTableOfContents(readme)
    : { rows: chaptersFromDisk(directory), unreadable: 0 };

  if (unreadable > 0) {
    notices.push(
      `${unreadable} row${unreadable === 1 ? "" : "s"} of the manual's table of contents could not be read and ${unreadable === 1 ? "was" : "were"} skipped.`,
    );
  }

  for (const row of rows) {
    const slug = row.file.replace(/\.md$/, "");
    const href = `${MANUAL_ROUTE}?chapter=${slug}`;
    const body = /^[A-Za-z0-9._-]+\.md$/.test(row.file)
      ? readTextFile(path.join(directory, row.file))
      : null;

    if (body === null) {
      notices.push(
        `Chapter ${row.number} (${row.title}) is listed in the table of contents, but ${row.file} is not in the manual folder.`,
      );
      chapters.push({
        slug,
        number: row.number,
        title: row.title,
        summary: row.summary,
        href,
        body: "",
        available: false,
        problem: `This chapter's file (${row.file}) is not in the manual folder, so there is nothing to show for it.`,
      });
      continue;
    }

    chapters.push({
      slug,
      number: row.number,
      title: row.title,
      summary: row.summary,
      href,
      body: withoutTitle(body),
      available: true,
      problem: null,
    });
  }

  return { chapters, notices };
}
