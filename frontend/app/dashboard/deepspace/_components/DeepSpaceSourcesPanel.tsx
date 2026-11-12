"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { ArrowUpRight, ChevronDown, ChevronUp, X } from "lucide-react";

import type { TimelineStep } from "../_lib/deepspace-stream";

export interface DeepSpaceSource {
  id: string;
  title: string;
  url: string;
  snippet?: string;
  favicon?: string;
  publishedDate?: string;
  retrievalStatus?: string;
}

type SourceMetadata = Omit<DeepSpaceSource, "id">;

const SOURCE_REFERENCE_PATTERN =
  /\[(?:R|#)?(\d+)\]\s*\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)(?:\s*[—–-]\s*(read in full|search snippet only|blocked(?:; not read)?|unavailable(?:; not read)?))?/gi;

function safeWebUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value.trim());
    if ((url.protocol !== "https:" && url.protocol !== "http:") || url.username || url.password) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

function sourceUrlKey(value: string): string {
  const url = new URL(value);
  url.hash = "";
  if (url.pathname.length > 1) url.pathname = url.pathname.replace(/\/+$/, "");
  return url.toString();
}

function safeFaviconUrl(value: unknown): string | undefined {
  const safeUrl = safeWebUrl(value);
  if (!safeUrl) return undefined;
  if (new URL(safeUrl).protocol !== "https:") return undefined;
  const host = new URL(safeUrl).hostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host.endsWith(".local") ||
    host.endsWith(".internal") ||
    host.includes(":")
  ) {
    return undefined;
  }
  const octets = host.split(".").map(Number);
  if (
    octets.length === 4 &&
    octets.every((part) => Number.isInteger(part) && part >= 0 && part <= 255)
  ) {
    const [first, second] = octets;
    if (
      first === 0 ||
      first === 10 ||
      first === 127 ||
      (first === 169 && second === 254) ||
      (first === 172 && second >= 16 && second <= 31) ||
      (first === 192 && second === 168) ||
      (first === 100 && second >= 64 && second <= 127)
    ) {
      return undefined;
    }
  }
  return safeUrl;
}

function readToolSourceMetadata(timeline: TimelineStep[] = []): Map<string, SourceMetadata> {
  const metadata = new Map<string, SourceMetadata>();
  for (const step of timeline) {
    if (!step.toolName || !["web_search", "url_read", "web_fetch"].includes(step.toolName)) {
      continue;
    }
    if (!step.toolOutput) continue;
    try {
      const payload = JSON.parse(step.toolOutput) as Record<string, unknown>;
      const rawCitations = Array.isArray(payload.citations)
        ? payload.citations
        : Array.isArray(payload.results)
          ? payload.results
          : [];
      for (const rawCitation of rawCitations) {
        if (!rawCitation || typeof rawCitation !== "object") continue;
        const citation = rawCitation as Record<string, unknown>;
        const url = safeWebUrl(citation.url);
        if (!url) continue;
        const key = sourceUrlKey(url);
        const previous = metadata.get(key);
        const retrievalStatus =
          typeof citation.retrieval_status === "string"
            ? citation.retrieval_status
            : step.toolName === "web_search"
              ? "search_snippet_only"
              : undefined;
        const isFetched = retrievalStatus === "read_full";
        const isUnavailable = retrievalStatus === "blocked" || retrievalStatus === "unavailable";
        metadata.set(key, {
          url,
          title:
            (isFetched && typeof citation.title === "string" ? citation.title : undefined) ||
            previous?.title ||
            (typeof citation.title === "string" ? citation.title : undefined) ||
            url,
          snippet:
            (isFetched && typeof citation.snippet === "string" ? citation.snippet : undefined) ||
            previous?.snippet ||
            (typeof citation.snippet === "string" ? citation.snippet : undefined),
          favicon:
            (typeof citation.favicon === "string" ? safeFaviconUrl(citation.favicon) : undefined) ||
            previous?.favicon,
          publishedDate:
            (typeof citation.published_date === "string" ? citation.published_date : undefined) ||
            previous?.publishedDate,
          retrievalStatus:
            (isFetched || isUnavailable ? retrievalStatus : previous?.retrievalStatus) ||
            retrievalStatus,
        });
      }
    } catch {
      // Older or pruned timeline entries may not contain JSON source results.
    }
  }
  return metadata;
}

export function extractDeepSpaceSources(
  markdown: string,
  timeline: TimelineStep[] = [],
): { content: string; sources: DeepSpaceSource[] } {
  const headingPattern = /(^|\n)#{1,6}\s+sources\s*(?:\n|$)/gim;
  const headings = [...markdown.matchAll(headingPattern)];
  const heading = headings[headings.length - 1];
  if (!heading) return { content: markdown, sources: [] };

  const headingStart = heading.index + (heading[1] === "\n" ? 1 : 0);
  const sectionStart = heading.index + heading[0].length;
  const references = [...markdown.slice(sectionStart).matchAll(SOURCE_REFERENCE_PATTERN)];
  const sources: DeepSpaceSource[] = [];
  const metadata = readToolSourceMetadata(timeline);
  const seen = new Map<string, number>();

  for (const reference of references) {
    const url = safeWebUrl(reference[3]);
    if (!url) continue;
    const key = sourceUrlKey(url);
    const fetched = metadata.get(key);
    const statusText = reference[4]?.toLowerCase();
    const citationStatus = statusText?.startsWith("read in full")
      ? "read_full"
      : statusText?.startsWith("search snippet only")
        ? "search_snippet_only"
        : statusText?.startsWith("blocked")
          ? "blocked"
          : statusText?.startsWith("unavailable")
            ? "unavailable"
            : undefined;
    const source: DeepSpaceSource = {
      id: `source-${reference[1]}`,
      title:
        (fetched?.retrievalStatus === "read_full" ? fetched.title : undefined) ||
        reference[2].replace(/[*_`]/g, "").trim() ||
        fetched?.title ||
        url,
      url,
      snippet: fetched?.snippet,
      favicon: fetched?.favicon,
      publishedDate: fetched?.publishedDate,
      retrievalStatus: fetched?.retrievalStatus || citationStatus,
    };
    const existingIndex = seen.get(key);
    if (existingIndex === undefined) {
      seen.set(key, sources.length);
      sources.push(source);
      continue;
    }
    const existing = sources[existingIndex];
    if (!existing) continue;
    sources[existingIndex] = {
      ...existing,
      ...(source.retrievalStatus === "read_full" ? source : {}),
      snippet: source.retrievalStatus === "read_full" ? source.snippet : existing.snippet,
      favicon: source.favicon || existing.favicon,
      publishedDate: source.publishedDate || existing.publishedDate,
    };
  }

  if (!sources.length) return { content: markdown, sources: [] };
  return { content: markdown.slice(0, headingStart).trimEnd(), sources };
}

function domainFor(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./i, "");
  } catch {
    return url;
  }
}

function initialFor(domain: string): string {
  return (
    domain
      .replace(/^www\./i, "")
      .charAt(0)
      .toUpperCase() || "W"
  );
}

function avatarColor(domain: string): string {
  const colors = ["#2563eb", "#0f766e", "#7c3aed", "#b45309", "#be123c", "#0369a1"];
  let hash = 0;
  for (let index = 0; index < domain.length; index += 1) {
    hash = (hash * 31 + domain.charCodeAt(index)) | 0;
  }
  return colors[Math.abs(hash) % colors.length] ?? colors[0];
}

function SourceAvatar({ source, small = false }: { source: DeepSpaceSource; small?: boolean }) {
  const [imageFailed, setImageFailed] = useState(false);
  const domain = domainFor(source.url);
  const favicon = safeFaviconUrl(source.favicon);
  const size = small ? "h-5 w-5 text-[10px]" : "h-9 w-9 text-sm";
  return (
    <span
      className={`relative inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full font-semibold text-white ${size}`}
      style={{ backgroundColor: avatarColor(domain) }}
      aria-hidden="true"
    >
      {initialFor(domain)}
      {favicon && !imageFailed ? (
        // Provider-supplied favicons are optional; the domain initial remains visible on failure.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={favicon}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
          className="bg-card absolute inset-0 h-full w-full rounded-full object-contain p-0.5"
          onError={() => setImageFailed(true)}
        />
      ) : null}
    </span>
  );
}

function retrievalLabel(status?: string): string | null {
  switch (status) {
    case "read_full":
      return "Read in full";
    case "search_snippet_only":
      return "Search snippet";
    case "blocked":
    case "unavailable":
      return "Not fetched";
    default:
      return null;
  }
}

function SourceCard({ source }: { source: DeepSpaceSource }) {
  const [expanded, setExpanded] = useState(false);
  const domain = domainFor(source.url);
  const label = retrievalLabel(source.retrievalStatus);
  const hasExpandableSnippet = Boolean(source.snippet && source.snippet.length > 180);

  return (
    <article className="border-border bg-surface-1 rounded-xl border p-3.5 shadow-sm">
      <div className="flex items-center gap-2.5">
        <SourceAvatar source={source} />
        <div className="min-w-0 flex-1">
          <a
            href={source.url}
            target="_blank"
            rel="noreferrer noopener"
            className="text-foreground/75 inline-flex max-w-full items-center gap-1 truncate text-xs font-semibold underline-offset-2 hover:underline"
            title={source.url}
          >
            <span className="truncate">{domain}</span>
            <ArrowUpRight size={12} className="shrink-0 opacity-60" aria-hidden="true" />
          </a>
          {source.publishedDate ? (
            <p className="text-muted-foreground mt-0.5 truncate text-[10px]">
              Published {source.publishedDate}
            </p>
          ) : null}
        </div>
        {label ? (
          <span className="border-border bg-card text-muted-foreground shrink-0 rounded-full border px-2 py-1 text-[10px] font-medium">
            {label}
          </span>
        ) : null}
      </div>
      <h3 className="mt-3 text-sm leading-5 font-semibold">
        <a
          href={source.url}
          target="_blank"
          rel="noreferrer noopener"
          className="text-foreground decoration-foreground/30 hover:decoration-foreground underline underline-offset-2"
        >
          {source.title}
        </a>
      </h3>
      {source.snippet ? (
        <>
          <p
            className={`text-muted-foreground mt-2 text-xs leading-5 ${expanded ? "whitespace-pre-wrap" : "line-clamp-3"}`}
          >
            {source.snippet}
          </p>
          {hasExpandableSnippet ? (
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => setExpanded((value) => !value)}
              className="text-foreground/70 hover:text-foreground mt-2 inline-flex items-center gap-1 text-xs font-semibold"
            >
              {expanded ? "Show less" : "Show more"}
              {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            </button>
          ) : null}
        </>
      ) : null}
    </article>
  );
}

export default function DeepSpaceSourcesPanel({
  sources,
  surface = false,
}: {
  sources: DeepSpaceSource[];
  surface?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const panelId = `deepspace-sources-${useId().replace(/:/g, "")}`;
  const previewSources = sources.slice(0, 3);
  const prefersReducedMotion = useReducedMotion();

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
      if (event.key !== "Tab") return;
      const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", closeOnEscape);
    closeRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, [open]);

  const panel =
    typeof document !== "undefined"
      ? createPortal(
          <AnimatePresence>
            {open ? (
              <motion.div
                key={panelId}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: prefersReducedMotion ? 0 : 0.18, ease: "easeOut" }}
                className="fixed inset-0 z-[120] bg-black/40 backdrop-blur-[2px]"
                style={{ top: "var(--averqel-desktop-chrome-height, 0px)" }}
                onMouseDown={(event) => {
                  if (event.target === event.currentTarget) setOpen(false);
                }}
              >
                <motion.aside
                  ref={panelRef}
                  id={panelId}
                  role="dialog"
                  aria-modal="true"
                  aria-labelledby={`${panelId}-title`}
                  initial={{ x: prefersReducedMotion ? 0 : "100%" }}
                  animate={{ x: 0 }}
                  exit={{ x: prefersReducedMotion ? 0 : "100%" }}
                  transition={{
                    type: "tween",
                    duration: prefersReducedMotion ? 0 : 0.24,
                    ease: [0.22, 1, 0.36, 1],
                  }}
                  className="border-border bg-card text-card-foreground absolute inset-y-0 right-0 flex w-[min(28rem,100vw)] flex-col overflow-hidden rounded-l-2xl border-l shadow-2xl"
                >
                  <header className="border-border bg-surface-1 flex shrink-0 items-center justify-between gap-3 border-b px-5 py-4">
                    <div className="min-w-0">
                      <h2 id={`${panelId}-title`} className="text-base font-semibold">
                        Sources
                      </h2>
                      <p className="text-muted-foreground mt-0.5 text-xs">
                        {sources.length} cited {sources.length === 1 ? "source" : "sources"}
                      </p>
                    </div>
                    <button
                      ref={closeRef}
                      type="button"
                      onClick={() => setOpen(false)}
                      aria-label="Close sources panel"
                      className="text-muted-foreground hover:bg-surface-2 hover:text-foreground focus-visible:ring-primary/50 rounded-lg p-2 transition-colors focus-visible:ring-2 focus-visible:outline-none"
                    >
                      <X size={18} />
                    </button>
                  </header>
                  <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4 sm:p-5">
                    {sources.map((source) => (
                      <SourceCard key={`${source.id}-${source.url}`} source={source} />
                    ))}
                  </div>
                </motion.aside>
              </motion.div>
            ) : null}
          </AnimatePresence>,
          document.body,
        )
      : null;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-label={`Open ${sources.length} cited ${sources.length === 1 ? "source" : "sources"}`}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(true)}
        className={`focus-visible:ring-primary/50 inline-flex items-center gap-2 rounded-full border px-2.5 py-1.5 text-xs font-medium shadow-sm transition-colors focus-visible:ring-2 focus-visible:outline-none ${surface ? "border-border bg-card text-card-foreground hover:bg-surface-1" : "border-white/10 bg-neutral-900 text-neutral-100 hover:bg-neutral-800"}`}
      >
        <span className="flex items-center -space-x-1.5" aria-hidden="true">
          {previewSources.map((source) => (
            <span key={`${source.id}-${source.url}`} className="border-card rounded-full border-2">
              <SourceAvatar source={source} small />
            </span>
          ))}
        </span>
        <span>Sources</span>
      </button>
      {panel}
    </>
  );
}
