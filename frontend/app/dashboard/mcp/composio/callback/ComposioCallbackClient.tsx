"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { completeComposioOAuth } from "@/lib/mcp-api";

/**
 * Verifies a Composio redirect and returns the browser to the MCP page.
 *
 * Composio sends ``toolkit_slug``, ``connected_account_id``, and ``status``
 * back to the callback URL. The redirect parameters are removed from browser
 * history before the authenticated ownership check runs.
 */
export default function ComposioCallbackClient() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const handled = useRef(false);
  const toolkitSlug = searchParams.get("toolkit_slug");
  const connectedAccountId = searchParams.get("connected_account_id");
  const callbackStatus = searchParams.get("status");
  const [message, setMessage] = useState("Verifying your Composio app connection…");
  const [error, setError] = useState<string | null>(null);
  const incompleteCallback =
    !toolkitSlug ||
    !connectedAccountId ||
    (callbackStatus !== "success" && callbackStatus !== "failed");

  useEffect(() => {
    if (handled.current) return;
    handled.current = true;

    window.history.replaceState({}, "", window.location.pathname);

    if (
      !toolkitSlug ||
      !connectedAccountId ||
      (callbackStatus !== "success" && callbackStatus !== "failed")
    ) {
      return;
    }

    void completeComposioOAuth({
      toolkitSlug,
      connectedAccountId,
      status: callbackStatus as "success" | "failed",
    })
      .then(({ server_id: serverId, status }) => {
        if (status === "failed") {
          setError("Composio authorization was canceled or did not complete. You can try again.");
          return;
        }
        setMessage("Connection verified. Loading the app’s MCP tools…");
        router.replace(
          `/dashboard/mcp?${new URLSearchParams({ mcp_status: "connected", server_id: serverId })}`,
        );
      })
      .catch((reason: unknown) => {
        setError(
          reason instanceof Error
            ? reason.message
            : "The connection could not be verified. Return to the same signed-in AverQel account and try again.",
        );
      });
  }, [router, searchParams, toolkitSlug, connectedAccountId, callbackStatus]);

  const visibleError =
    error ??
    (incompleteCallback
      ? "Composio did not return a complete connection result. Start the connection again."
      : null);

  return (
    <main className="mcp-theme-scope mx-auto flex min-h-[50vh] max-w-2xl items-center px-6 py-16">
      <section className="w-full rounded-2xl border border-white/10 bg-white/[0.03] p-6">
        <h1 className="text-xl font-semibold text-white">Composio connection</h1>
        {visibleError ? (
          <div className="mt-4 space-y-4" role="alert">
            <p className="text-sm leading-6 text-red-200">{visibleError}</p>
            <a
              href="/dashboard/mcp"
              className="inline-flex rounded-lg border border-white/15 px-4 py-2 text-sm text-white/80 hover:bg-white/5"
            >
              Return to MCP Marketplace
            </a>
          </div>
        ) : (
          <p className="mt-3 text-sm leading-6 text-white/65" role="status">
            {message}
          </p>
        )}
      </section>
    </main>
  );
}
