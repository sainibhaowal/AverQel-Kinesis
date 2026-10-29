import { ChevronDown } from "lucide-react";
import { useEffect, useId, useState } from "react";

type ToolDescription = {
  name: string;
  description?: string | null;
};

const SUMMARY_LIMIT = 180;

function compactText(value: string): string {
  return value
    .replace(/<example[\s\S]*$/i, "")
    .replace(/<\/?[a-z][^>]*>/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function summarizeToolDescription(description?: string | null): string {
  const text = compactText(description || "");
  if (!text) return "No short description provided. Open details for the provider documentation.";

  const firstSentence = text.match(/^.*?[.!?](?:\s|$)/)?.[0]?.trim() || text;
  if (firstSentence.length <= SUMMARY_LIMIT) return firstSentence;
  return `${firstSentence.slice(0, SUMMARY_LIMIT - 1).trimEnd()}…`;
}

export default function MCPToolDescription({ tool }: { tool: ToolDescription }) {
  const [expanded, setExpanded] = useState(false);
  const [renderDetails, setRenderDetails] = useState(false);
  const detailsId = useId();
  const description = tool.description?.trim() || "";

  useEffect(() => {
    if (expanded) return;
    const timeout = window.setTimeout(() => setRenderDetails(false), 160);
    return () => window.clearTimeout(timeout);
  }, [expanded]);

  return (
    <div className="min-w-0">
      <div className="flex items-start gap-3">
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={detailsId}
          aria-label={`${expanded ? "Hide" : "Show"} full details for ${tool.name}`}
          onClick={() => {
            if (!expanded) setRenderDetails(true);
            setExpanded((current) => !current);
          }}
          className="mcp-tool-toggle mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-lg border border-glass-border bg-surface-1 text-muted-foreground transition hover:border-emerald-500/40 hover:bg-emerald-500/10 hover:text-emerald-600 dark:hover:text-emerald-400 focus-visible:ring-2 focus-visible:ring-emerald-500/60 focus-visible:outline-none shadow-sm"
        >
          <ChevronDown
            className={`h-3.5 w-3.5 transition-transform duration-200 ${expanded ? "rotate-180" : ""}`}
            aria-hidden="true"
          />
        </button>
        <div className="min-w-0">
          <p className="truncate font-mono text-sm font-bold text-emerald-600 dark:text-emerald-400" title={tool.name}>
            {tool.name}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            {summarizeToolDescription(tool.description)}
          </p>
        </div>
      </div>
      {renderDetails ? (
        <div
          id={detailsId}
          aria-hidden={!expanded}
          className={`mcp-tool-details ${expanded ? "mcp-tool-details-open" : ""}`}
        >
          <div className="mcp-tool-details-inner">
            <div className="mcp-tool-docs mt-3 max-h-72 overflow-y-auto rounded-xl border border-emerald-500/25 bg-emerald-500/[0.04] dark:bg-emerald-950/20 p-3.5 shadow-inner">
              <p className="mcp-tool-docs-label mb-2 text-[10px] font-bold tracking-wider text-emerald-700 dark:text-emerald-300 uppercase">
                Provider documentation
              </p>
              <p className="text-xs leading-relaxed break-words whitespace-pre-wrap text-foreground/85">
                {description || "No additional documentation is available for this tool."}
              </p>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
