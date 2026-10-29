import { Suspense } from "react";

import { DocsShell } from "../_components/DocsShell";
import DeepSpaceManual from "../_components/DeepSpaceManual";
import { readDeepSpaceManual } from "./manual-data";

function ManualFallback() {
  return (
    <div className="theme-panel border-glass-border bg-background/40 text-muted-foreground rounded-3xl border p-8 text-sm leading-7 font-medium">
      Loading the manual…
    </div>
  );
}

export default function DeepSpaceManualPage() {
  const { chapters, notices } = readDeepSpaceManual();

  return (
    <DocsShell
      title="The DeepSpace manual"
      intro="The complete DeepSpace manual: one numbered chapter per subject, each with the use case, why it matters, how it works, and what to watch for. The chapters are read from the manual folder in the repository, so this page always shows the written manual."
    >
      <Suspense fallback={<ManualFallback />}>
        <DeepSpaceManual chapters={chapters} notices={notices} />
      </Suspense>
    </DocsShell>
  );
}
