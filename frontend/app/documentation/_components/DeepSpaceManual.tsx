"use client";

import { useMemo, useState, type ComponentProps } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { ChevronLeft, ChevronRight, ListTree, TriangleAlert } from "lucide-react";
import ReactMarkdown, { type Components, type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";

/** The chapter the manual opens on: the table of contents file itself. */
const OVERVIEW_SLUG = "readme";

/** One entry of the manual: a numbered chapter, or the manual's own overview. */
export interface ManualChapter {
  /** The file name without its extension. The `chapter` query parameter selects it. */
  slug: string;
  /** The chapter number from the table of contents. Empty for the overview. */
  number: string;
  title: string;
  /** The one-line description the table of contents gives for the chapter. */
  summary: string;
  /** The in-app route that opens the chapter. */
  href: string;
  /** The chapter markdown. Empty when the chapter file could not be read. */
  body: string;
  /** False when the table of contents lists a chapter whose file is missing. */
  available: boolean;
  /** Why the chapter cannot be shown, in one plain sentence. */
  problem: string | null;
}

export interface DeepSpaceManualProps {
  chapters: ManualChapter[];
  /** Plain statements about parts of the manual that could not be read. */
  notices: string[];
}

type ManualElementTag =
  | "h2"
  | "p"
  | "strong"
  | "em"
  | "del"
  | "ul"
  | "ol"
  | "li"
  | "blockquote"
  | "table"
  | "thead"
  | "th"
  | "td"
  | "pre";

type ManualProps<T extends ManualElementTag> = ComponentProps<T> & ExtraProps;
type ManualLinkProps = ComponentProps<"a"> & ExtraProps;
type ManualCodeProps = ComponentProps<"code"> & ExtraProps;

/** The value of one query parameter, or null when the URL does not carry it. */
function readSearchParam(
  params: { get: (key: string) => string | null } | null,
  key: string,
): string | null {
  const value = params?.get(key)?.trim();
  return value ? value : null;
}

interface ManualMarkdownNode {
  type: string;
  value?: string;
  children?: ManualMarkdownNode[];
  data?: { hProperties?: Record<string, unknown> };
}

function manualNodeText(node: ManualMarkdownNode): string {
  if (node.type === "text" || node.type === "inlineCode") return node.value ?? "";
  return (node.children ?? []).map(manualNodeText).join("");
}

/**
 * The anchor a heading gets. The chapters link to their own sections with
 * these anchors, so the same rule has to apply to the rendered headings.
 */
function manualHeadingSlug(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s+/g, "-");
}

function assignHeadingIds(node: ManualMarkdownNode, seen: Map<string, number>): void {
  if (node.type === "heading") {
    const base = manualHeadingSlug(manualNodeText(node));
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    node.data = {
      ...node.data,
      hProperties: {
        ...(node.data?.hProperties ?? {}),
        id: count === 0 ? base : `${base}-${count}`,
      },
    };
  }
  for (const child of node.children ?? []) assignHeadingIds(child, seen);
}

/** Gives every heading the anchor the chapters link to in their own contents list. */
function remarkHeadingIds() {
  return (tree: unknown): void => {
    if (!tree || typeof tree !== "object") return;
    assignHeadingIds(tree as ManualMarkdownNode, new Map<string, number>());
  };
}

function manualHeading(level: number, className: string) {
  return function ManualHeading({ children, id }: ManualProps<"h2">) {
    const Tag = `h${Math.min(level + 1, 6)}` as "h3" | "h4" | "h5" | "h6";
    return (
      <Tag id={id} className={className}>
        {children}
      </Tag>
    );
  };
}

function createManualComponents(chapterRoutes: Map<string, string>): Components {
  return {
    h1: manualHeading(1, "text-foreground mt-8 mb-3 text-2xl font-black tracking-tight"),
    h2: manualHeading(2, "text-foreground mt-8 mb-3 text-xl font-black tracking-tight"),
    h3: manualHeading(3, "text-foreground mt-6 mb-2 text-lg font-bold"),
    h4: manualHeading(4, "text-foreground mt-5 mb-2 text-base font-bold"),
    h5: manualHeading(5, "text-foreground mt-4 mb-2 text-sm font-bold"),
    h6: manualHeading(6, "text-muted-foreground mt-4 mb-2 text-xs font-bold tracking-wider uppercase"),
    p: ({ children }: ManualProps<"p">) => (
      <p className="text-foreground/90 my-4 leading-8">{children}</p>
    ),
    strong: ({ children }: ManualProps<"strong">) => (
      <strong className="text-foreground font-bold">{children}</strong>
    ),
    em: ({ children }: ManualProps<"em">) => <em className="italic">{children}</em>,
    del: ({ children }: ManualProps<"del">) => (
      <del className="text-muted-foreground line-through">{children}</del>
    ),
    hr: () => <hr className="border-glass-border my-8" />,
    ul: ({ children }: ManualProps<"ul">) => (
      <ul className="text-foreground/90 my-4 list-disc space-y-2 pl-6 marker:text-primary">
        {children}
      </ul>
    ),
    ol: ({ children }: ManualProps<"ol">) => (
      <ol className="text-foreground/90 my-4 list-decimal space-y-2 pl-6 marker:text-primary">
        {children}
      </ol>
    ),
    li: ({ children }: ManualProps<"li">) => <li className="leading-7">{children}</li>,
    blockquote: ({ children }: ManualProps<"blockquote">) => (
      <blockquote className="border-primary/40 bg-primary/5 my-5 border-l-2 px-4 py-3 leading-7">
        {children}
      </blockquote>
    ),
    table: ({ children }: ManualProps<"table">) => (
      <div className="border-glass-border my-6 overflow-x-auto rounded-2xl border">
        <table className="w-full border-collapse text-left text-sm">{children}</table>
      </div>
    ),
    thead: ({ children }: ManualProps<"thead">) => (
      <thead className="bg-surface-1/70">{children}</thead>
    ),
    th: ({ children }: ManualProps<"th">) => (
      <th className="text-foreground border-glass-border border-b px-4 py-3 text-xs font-black tracking-wide uppercase">
        {children}
      </th>
    ),
    td: ({ children }: ManualProps<"td">) => (
      <td className="text-foreground/85 border-glass-border/70 border-b px-4 py-3 align-top leading-6">
        {children}
      </td>
    ),
    pre: ({ children }: ManualProps<"pre">) => (
      <pre className="border-glass-border bg-surface-1/60 my-5 overflow-x-auto rounded-2xl border p-4 text-xs leading-6">
        {children}
      </pre>
    ),
    code: ({ children, className }: ManualCodeProps) => {
      if (className) {
        return (
          <code className={`font-mono text-foreground/90 ${className}`}>{children}</code>
        );
      }
      return (
        <code className="border-glass-border bg-surface-1/60 rounded-md border px-1.5 py-0.5 font-mono text-[0.85em] text-foreground">
          {children}
        </code>
      );
    },
    a: ({ href, children }: ManualLinkProps) => {
      const target = href ?? "";
      const chapterHref = chapterRoutes.get(target.replace(/\.md$/, ""));
      if (chapterHref) {
        return (
          <Link
            href={chapterHref}
            className="text-primary font-semibold underline decoration-primary/40 underline-offset-4 hover:decoration-primary"
          >
            {children}
          </Link>
        );
      }
      if (target.startsWith("#")) {
        return (
          <a
            href={target}
            className="text-primary font-semibold underline decoration-primary/40 underline-offset-4 hover:decoration-primary"
          >
            {children}
          </a>
        );
      }
      if (/^https?:\/\//.test(target)) {
        return (
          <a
            href={target}
            target="_blank"
            rel="noreferrer noopener"
            className="text-primary font-semibold underline decoration-primary/40 underline-offset-4 hover:decoration-primary"
          >
            {children}
          </a>
        );
      }
      return (
        <a
          href={target}
          className="text-primary font-semibold underline decoration-primary/40 underline-offset-4 hover:decoration-primary"
        >
          {children}
        </a>
      );
    },
  };
}

function ManualMarkdown({
  content,
  chapterRoutes,
}: {
  content: string;
  chapterRoutes: Map<string, string>;
}) {
  const components = useMemo(() => createManualComponents(chapterRoutes), [chapterRoutes]);
  const plugins = useMemo(() => [remarkGfm, remarkHeadingIds], []);

  return (
    <div className="text-foreground/90 max-w-none break-words">
      <ReactMarkdown remarkPlugins={plugins} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}

function ChapterList({
  chapters,
  currentSlug,
  open,
  onToggle,
  onSelect,
}: {
  chapters: ManualChapter[];
  currentSlug: string | null;
  open: boolean;
  onToggle: () => void;
  onSelect: () => void;
}) {
  return (
    <div className="lg:sticky lg:top-0 lg:self-start">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls="deepspace-manual-chapters"
        className="theme-panel border-glass-border bg-background/40 flex w-full items-center justify-between gap-3 rounded-2xl border px-4 py-3 text-left text-xs font-black tracking-wide uppercase lg:hidden"
      >
        <span className="flex items-center gap-2">
          <ListTree size={14} className="text-primary" />
          {open ? "Hide the chapters" : `The ${chapters.length} chapters`}
        </span>
        <ChevronRight
          size={14}
          className={`text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`}
        />
      </button>
      <nav
        id="deepspace-manual-chapters"
        aria-label="DeepSpace manual chapters"
        className={`${open ? "block" : "hidden"} lg:block`}
      >
        <ol className="docs-scrollbar mt-3 max-h-[55vh] space-y-0.5 overflow-y-auto pr-1 lg:mt-0 lg:max-h-[calc(100dvh-11rem)]">
          {chapters.map((chapter) => {
            const current = chapter.slug === currentSlug;
            return (
              <li key={chapter.slug}>
                <Link
                  href={chapter.href}
                  onClick={onSelect}
                  aria-current={current ? "page" : undefined}
                  className={`flex items-baseline gap-2 rounded-xl px-3 py-2 text-xs font-bold transition-colors ${
                    current
                      ? "bg-primary/10 text-primary"
                      : "text-muted-foreground hover:bg-primary/5 hover:text-foreground"
                  }`}
                >
                  {chapter.number ? (
                    <span className="w-6 shrink-0 tabular-nums opacity-70">{chapter.number}</span>
                  ) : null}
                  <span className="min-w-0 flex-1">{chapter.title}</span>
                </Link>
              </li>
            );
          })}
        </ol>
      </nav>
    </div>
  );
}

export default function DeepSpaceManual({ chapters, notices }: DeepSpaceManualProps) {
  const searchParams = useSearchParams();
  const requestedSlug = readSearchParam(searchParams, "chapter");
  const [listOpen, setListOpen] = useState(false);

  const chapterRoutes = useMemo(() => {
    const routes = new Map<string, string>();
    for (const chapter of chapters) routes.set(chapter.slug, chapter.href);
    return routes;
  }, [chapters]);

  const selected = useMemo(() => {
    if (requestedSlug) {
      return chapters.find((chapter) => chapter.slug === requestedSlug) ?? null;
    }
    return chapters.find((chapter) => chapter.slug === OVERVIEW_SLUG) ?? chapters[0] ?? null;
  }, [chapters, requestedSlug]);

  const position = selected
    ? chapters.findIndex((chapter) => chapter.slug === selected.slug)
    : -1;
  const previous = position > 0 ? chapters[position - 1] : null;
  const next = position >= 0 && position < chapters.length - 1 ? chapters[position + 1] : null;

  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,16rem)_minmax(0,1fr)] lg:gap-10">
      <ChapterList
        chapters={chapters}
        currentSlug={selected?.slug ?? null}
        open={listOpen}
        onToggle={() => setListOpen((current) => !current)}
        onSelect={() => setListOpen(false)}
      />

      <div className="min-w-0 space-y-6">
        {notices.map((notice) => (
          <p
            key={notice}
            className="theme-panel border-glass-border bg-background/40 text-muted-foreground flex items-start gap-3 rounded-2xl border p-4 text-sm leading-6 font-medium"
          >
            <TriangleAlert size={16} className="text-primary mt-0.5 shrink-0" />
            {notice}
          </p>
        ))}

        {selected ? (
          <article className="space-y-6">
            <header className="space-y-3">
              <p className="text-primary text-[10px] font-black tracking-[0.3em] uppercase">
                {selected.number ? `Chapter ${selected.number}` : "The manual"}
              </p>
              <h2 className="text-foreground text-2xl font-black tracking-tight sm:text-3xl">
                {selected.title}
              </h2>
              {selected.summary ? (
                <p className="text-muted-foreground text-sm leading-7 font-medium">
                  {selected.summary}
                </p>
              ) : null}
            </header>

            {selected.available ? (
              <ManualMarkdown content={selected.body} chapterRoutes={chapterRoutes} />
            ) : (
              <p
                role="status"
                className="theme-panel border-glass-border bg-background/40 text-muted-foreground rounded-2xl border p-5 text-sm leading-7 font-medium"
              >
                {selected.problem ??
                  "This chapter could not be read, so there is nothing to show for it."}
              </p>
            )}

            <nav
              aria-label="Chapter navigation"
              className="border-glass-border flex items-stretch justify-between gap-4 border-t pt-6"
            >
              {previous ? (
                <Link
                  href={previous.href}
                  className="text-muted-foreground hover:text-primary flex min-w-0 items-center gap-2 rounded-xl px-2 py-2 text-xs font-bold transition-colors"
                >
                  <ChevronLeft size={14} className="shrink-0" />
                  <span className="min-w-0 truncate">{previous.title}</span>
                </Link>
              ) : (
                <span />
              )}
              {next ? (
                <Link
                  href={next.href}
                  className="text-muted-foreground hover:text-primary flex min-w-0 items-center gap-2 rounded-xl px-2 py-2 text-xs font-bold transition-colors"
                >
                  <span className="min-w-0 truncate">{next.title}</span>
                  <ChevronRight size={14} className="shrink-0" />
                </Link>
              ) : (
                <span />
              )}
            </nav>
          </article>
        ) : requestedSlug ? (
          <div
            role="status"
            className="theme-panel border-glass-border bg-background/40 space-y-3 rounded-2xl border p-6"
          >
            <p className="text-foreground text-lg font-black">
              There is no chapter called &ldquo;{requestedSlug}&rdquo; in this manual.
            </p>
            <p className="text-muted-foreground text-sm leading-7 font-medium">
              The manual holds {chapters.length} chapters. Choose one from the list to read it.
            </p>
          </div>
        ) : (
          <p
            role="status"
            className="theme-panel border-glass-border bg-background/40 text-muted-foreground rounded-2xl border p-6 text-sm leading-7 font-medium"
          >
            The manual could not be read, so there is no chapter to show.
          </p>
        )}
      </div>
    </div>
  );
}
