import { CheckCircle2, Clock3, ShieldAlert } from "lucide-react";

import type { MCPHealth } from "@/lib/mcp-api";

function formatDate(value?: string | null): string {
  if (!value) return "Not verified yet";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Verification date unavailable"
    : new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeZone: "UTC" }).format(date);
}
export default function MCPHealthStatus({
  health,
  compact = false,
}: {
  health?: MCPHealth | null;
  compact?: boolean;
}) {
  const status = String(health?.status || "not_checked").toLowerCase();
  const healthy = status === "healthy" || status === "ok" || status === "available";
  const checking = status === "not_checked" || status === "unknown" || status === "pending";
  const Icon = healthy ? CheckCircle2 : checking ? Clock3 : ShieldAlert;
  const tone = healthy
    ? "text-emerald-600 dark:text-emerald-400"
    : checking
      ? "text-amber-600 dark:text-amber-400"
      : "text-red-600 dark:text-red-400";
  const label = healthy ? "Healthy" : checking ? "Not checked" : status.replaceAll("_", " ");
  return (
    <div className={`flex items-center gap-2 ${tone}`} title={health?.detail || undefined}>
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className={`font-semibold ${compact ? "text-xs" : "text-sm"}`}>{label}</span>
      <span className="text-xs font-medium text-muted-foreground">· {formatDate(health?.last_checked_at)}</span>
    </div>
  );
}
