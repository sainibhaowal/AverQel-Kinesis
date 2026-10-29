import { Suspense } from "react";

import ComposioCallbackClient from "./ComposioCallbackClient";

export const metadata = { title: "Composio connection", referrer: "no-referrer" };

/** Composio redirects here after a user authorizes an app in its own browser flow. */
export default function ComposioCallbackPage() {
  return (
    <Suspense
      fallback={
        <main className="mcp-theme-scope mx-auto flex min-h-[50vh] max-w-2xl items-center px-6 py-16">
          <p className="text-sm text-white/65">Preparing Composio verification…</p>
        </main>
      }
    >
      <ComposioCallbackClient />
    </Suspense>
  );
}
