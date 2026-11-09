"use client";

import { AverQelLoader } from "@/app/components/ui/AverQelLoading";

import { Check, KeyRound, LogOut, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import {
  disconnectOpenAIOAuthAccount,
  fetchLocalCodexModels,
  fetchLocalCodexStatus,
  fetchOpenAIOAuthFlowStatus,
  startOpenAIOAuth,
} from "@/lib/providers-api";
import ProviderDropdown from "./ProviderDropdown";

type Props = {
  initialAccountId: string | null;
  onChange: (accountId: string | null, models: CodexModel[]) => void;
};

const wait = (milliseconds: number) =>
  new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));

export default function OpenAICodexLoginPanel({ initialAccountId, onChange }: Props) {
  const [available, setAvailable] = useState(false);
  const [chatgptSignInAvailable, setChatgptSignInAvailable] = useState(false);
  const [accounts, setAccounts] = useState<CodexAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState(initialAccountId || "");
  const [models, setModels] = useState<CodexModel[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [cliMessage, setCliMessage] = useState("");

  const publish = useCallback(
    (accountId: string | null, accountModels: CodexModel[]) => {
      setSelectedAccountId(accountId || "");
      setModels(accountModels);
      onChange(accountId, accountModels);
    },
    [onChange],
  );

  const loadModels = useCallback(
    async (accountId: string) => {
      const accountModels = await fetchLocalCodexModels(accountId);
      publish(accountId, accountModels);
      setError("");
    },
    [publish],
  );

  const reloadStatus = useCallback(async () => {
    const status = await fetchLocalCodexStatus();
    setAvailable(status.available);
    setChatgptSignInAvailable(status.chatgptSignInAvailable ?? false);
    setAccounts(status.accounts || []);
    setCliMessage(status.cliMessage || "");
    return status;
  }, []);

  useEffect(() => {
    let cancelled = false;
    const initialize = async () => {
      try {
        const status = await fetchLocalCodexStatus();
        if (cancelled) return;
        setAvailable(status.available);
        setChatgptSignInAvailable(status.chatgptSignInAvailable ?? false);
        setAccounts(status.accounts || []);
        setCliMessage(status.cliMessage || "");
        if (!status.available) {
          setError(
            status.message ||
              "The local Codex provider is available only in the standalone Kinesis backend.",
          );
          return;
        }
        const matching = (status.accounts || []).find(
          (account) => account.id === initialAccountId,
        );
        const account =
          matching ||
          (status.accounts || []).find((candidate) => candidate.source === "codex_cli") ||
          status.accounts?.[0];
        if (account) {
          setSelectedAccountId(account.id);
          const accountModels =
            account.source === "codex_cli" && status.models?.length
              ? status.models
              : await fetchLocalCodexModels(account.id);
          if (!cancelled) publish(account.id, accountModels);
        } else {
          publish(null, []);
        }
      } catch (loadError) {
        if (!cancelled) {
          setAvailable(false);
          setError(
            loadError instanceof Error
              ? loadError.message
              : "Codex account status could not be loaded.",
          );
        }
      }
    };
    void initialize();
    return () => {
      cancelled = true;
    };
  }, [initialAccountId, publish]);

  const handleSelect = async (accountId: string) => {
    setSelectedAccountId(accountId);
    setError("");
    setNotice("");
    if (!accountId) {
      publish(null, []);
      return;
    }
    setBusy(true);
    try {
      await loadModels(accountId);
    } catch (loadError) {
      publish(accountId, []);
      setError(
        loadError instanceof Error
          ? loadError.message
          : "The account model list could not be loaded.",
      );
    } finally {
      setBusy(false);
    }
  };

  const handleChatGPTSignIn = async (newAccount: boolean) => {
    const selected = accounts.find((account) => account.id === selectedAccountId);
    const accountId =
      !newAccount && selected?.source === "chatgpt" ? selectedAccountId : null;
    const electron = window.electron;
    const browserWindow =
      !electron?.openExternal && typeof window.open === "function"
        ? window.open("about:blank", "_blank")
        : null;
    if (!electron?.openExternal && !browserWindow) {
      setError("Allow pop-ups for AverQel so ChatGPT sign-in can open in your browser.");
      return;
    }

    setBusy(true);
    setError("");
    setNotice("");
    try {
      const start = await startOpenAIOAuth(accountId);
      if (!start.available || !start.authorization_url || !start.flow_id) {
        throw new Error(start.message || "ChatGPT browser sign-in is unavailable.");
      }
      if (electron?.openExternal) {
        const opened = await electron.openExternal(start.authorization_url);
        if (!opened) throw new Error("The system browser could not be opened.");
        browserWindow?.close();
      } else if (browserWindow) {
        browserWindow.location.href = start.authorization_url;
      }

      const deadline = Date.now() + 5 * 60_000;
      let accountIdFromFlow: string | null = null;
      while (Date.now() < deadline) {
        const flow = await fetchOpenAIOAuthFlowStatus(start.flow_id);
        if (flow.status === "complete") {
          accountIdFromFlow = flow.account_id;
          break;
        }
        if (flow.status === "failed") {
          throw new Error(flow.message || "ChatGPT sign-in was not completed.");
        }
        await wait(1_200);
      }
      if (!accountIdFromFlow) {
        throw new Error("ChatGPT sign-in timed out. Start again when you are ready.");
      }

      const status = await reloadStatus();
      const connected = status.accounts?.find((account) => account.id === accountIdFromFlow);
      if (!connected) throw new Error("The signed-in ChatGPT account could not be loaded.");
      await loadModels(connected.id);
      setNotice("ChatGPT account connected to the local Kinesis backend.");
    } catch (connectError) {
      browserWindow?.close();
      setError(
        connectError instanceof Error ? connectError.message : "ChatGPT sign-in failed.",
      );
    } finally {
      setBusy(false);
    }
  };

  const handleUseCli = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const status = await reloadStatus();
      const cliAccount = status.accounts?.find(
        (account) => account.id === selectedAccountId && account.source === "codex_cli",
      );
      if (!status.available || !cliAccount) {
        throw new Error(
          status.cliMessage ||
            "The selected Codex CLI login is no longer available. Refresh and try again.",
        );
      }
      await loadModels(cliAccount.id);
    } catch (connectError) {
      setError(
        connectError instanceof Error ? connectError.message : "Codex CLI status check failed.",
      );
    } finally {
      setBusy(false);
    }
  };

  const handleDisconnect = async () => {
    const selected = accounts.find((account) => account.id === selectedAccountId);
    if (!selected || selected.source !== "chatgpt") return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await disconnectOpenAIOAuthAccount(selectedAccountId);
      await reloadStatus();
      publish(null, []);
      setNotice(
        result.revoked
          ? "Removed from this computer. OpenAI confirmed the revocation request."
          : "Removed from this computer, but OpenAI could not confirm revocation. Check ChatGPT Settings → Usage to remove AverQel’s access.",
      );
    } catch (disconnectError) {
      setError(
        disconnectError instanceof Error
          ? disconnectError.message
          : "ChatGPT account could not be removed.",
      );
    } finally {
      setBusy(false);
    }
  };

  const selectedAccount = accounts.find((account) => account.id === selectedAccountId) || null;

  if (!available && !error) {
    return (
      <div className="text-muted-foreground rounded-xl border border-glass-border bg-surface-1/70 p-4 text-xs">
        Checking the local Codex provider…
      </div>
    );
  }

  return (
    <section className="space-y-4 rounded-2xl border border-slate-200 dark:border-teal-900/40 bg-white dark:bg-slate-900/90 p-4 sm:p-5 shadow-sm">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 rounded-xl bg-gradient-to-br from-emerald-600 to-teal-600 p-2.5 text-white shadow-xs dark:from-emerald-500 dark:to-teal-500 dark:text-slate-950">
          <KeyRound size={16} />
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="text-slate-900 dark:text-white text-sm font-black">
            ChatGPT account for Codex models
          </h3>
          <p className="text-slate-600 dark:text-slate-300 mt-1 text-xs leading-5">
            Use a signed-in local Codex CLI account or sign in with ChatGPT in your browser. The
            local backend encrypts browser credentials and sends model requests directly to OpenAI.
          </p>
        </div>
      </div>

      {error && !available ? (
        <p
          role="alert"
          className="rounded-xl border border-amber-500/40 bg-amber-50 dark:bg-amber-950/40 p-3 text-xs font-bold text-amber-900 dark:text-amber-200 shadow-2xs"
        >
          {error}
        </p>
      ) : (
        <>
          {accounts.length > 0 && (
            <div className="space-y-1.5">
              <span className="text-slate-700 dark:text-slate-300 font-mono text-[10px] font-black tracking-[0.2em] uppercase">
                Available accounts
              </span>
              <ProviderDropdown
                value={selectedAccountId}
                onChange={(value) => void handleSelect(value)}
                disabled={busy}
                placeholder="Choose a saved account"
                options={accounts.map((account) => ({
                  value: account.id,
                  label:
                    account.source === "codex_cli"
                      ? account.planType
                        ? "Use signed-in Codex CLI account (" + account.planType.toUpperCase() + ")"
                        : "Use signed-in Codex CLI account"
                      : account.name || account.email || "ChatGPT account",
                  hint:
                    account.source === "codex_cli"
                      ? "Local CLI session"
                      : "Backend browser session",
                }))}
                className="bg-white dark:bg-slate-900 border-slate-200 dark:border-white/10 h-10 rounded-xl shadow-2xs text-xs font-semibold"
              />
            </div>
          )}

          {selectedAccountId && models.length > 0 ? (
            <div className="flex items-center gap-2 text-xs font-bold text-emerald-800 dark:text-emerald-300 bg-emerald-50 dark:bg-emerald-950/40 border border-emerald-300 dark:border-emerald-500/30 px-3 py-1.5 rounded-xl shadow-2xs">
              <Check size={14} className="text-emerald-700 dark:text-emerald-400" />
              <span>{models.length} model options listed; access is confirmed when a model is used</span>
            </div>
          ) : null}

          {cliMessage ? (
            <p className="text-slate-900 dark:text-slate-100 font-mono rounded-xl border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-slate-800/80 p-3 text-xs shadow-2xs">
              {cliMessage}
            </p>
          ) : null}

          <div className="flex flex-wrap gap-2 pt-1">
            {selectedAccountId ? (
              <button
                type="button"
                onClick={() => void handleSelect(selectedAccountId)}
                disabled={busy}
                className="inline-flex h-9.5 items-center gap-2 rounded-xl border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-800 px-4 text-xs font-bold text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-700 transition disabled:opacity-50 shadow-2xs"
              >
                {busy ? (
                  <AverQelLoader size={14} className="animate-spin text-emerald-600 dark:text-emerald-400" />
                ) : (
                  <RefreshCw size={14} />
                )}
                <span>{models.length > 0 ? "Refresh models" : "Retry model list"}</span>
              </button>
            ) : null}
            {selectedAccount?.source === "codex_cli" ? (
              <button
                type="button"
                onClick={() => void handleUseCli()}
                disabled={busy}
                className="inline-flex h-9.5 items-center gap-2 rounded-xl border border-emerald-500/50 bg-gradient-to-r from-emerald-600 to-teal-600 px-4 text-xs font-black !text-white shadow-xs hover:from-emerald-700 hover:to-teal-700 hover:shadow-sm transition disabled:opacity-50 dark:from-emerald-500 dark:to-teal-500 dark:!text-slate-950"
              >
                <Check size={14} />
                <span>Use Codex CLI login</span>
              </button>
            ) : null}
            {selectedAccount?.source === "chatgpt" ? (
              <>
                <button
                  type="button"
                  onClick={() => void handleChatGPTSignIn(false)}
                  disabled={busy}
                  className="inline-flex h-9.5 items-center gap-2 rounded-xl border border-emerald-500/50 bg-gradient-to-r from-emerald-600 to-teal-600 px-4 text-xs font-black !text-white shadow-xs hover:from-emerald-700 hover:to-teal-700 hover:shadow-sm transition disabled:opacity-50 dark:from-emerald-500 dark:to-teal-500 dark:!text-slate-950"
                >
                  {busy ? (
                    <AverQelLoader size={14} className="animate-spin text-white dark:text-slate-950" />
                  ) : (
                    <RefreshCw size={14} />
                  )}
                  <span>Sign in again</span>
                </button>
                <button
                  type="button"
                  onClick={() => void handleDisconnect()}
                  disabled={busy}
                  className="inline-flex h-9.5 items-center gap-2 rounded-xl border border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-950/40 px-4 text-xs font-bold text-rose-700 dark:text-rose-300 hover:bg-rose-100 dark:hover:bg-rose-900/50 transition disabled:opacity-50 shadow-2xs"
                >
                  <LogOut size={14} />
                  <span>Remove from this computer</span>
                </button>
              </>
            ) : null}
            {chatgptSignInAvailable ? (
              <button
                type="button"
                onClick={() => void handleChatGPTSignIn(true)}
                disabled={busy}
                className="inline-flex h-9.5 items-center gap-2 rounded-xl border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-800 px-4 text-xs font-bold text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-700 transition disabled:opacity-50 shadow-2xs"
              >
                {busy ? (
                  <AverQelLoader size={14} className="animate-spin text-emerald-600 dark:text-emerald-400" />
                ) : (
                  <KeyRound size={14} />
                )}
                <span>
                  {selectedAccount?.source === "codex_cli"
                    ? "Sign in with ChatGPT instead"
                    : "Continue with ChatGPT"}
                </span>
              </button>
            ) : null}
          </div>
          {error ? (
            <p
              role="alert"
              className="rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-xs font-medium text-rose-700 dark:text-rose-200"
            >
              {error}
            </p>
          ) : null}
          {notice ? (
            <p
              role="status"
              className="rounded-xl border border-primary/30 bg-primary/10 p-3 text-xs font-medium text-primary dark:text-teal-200"
            >
              {notice}
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}
