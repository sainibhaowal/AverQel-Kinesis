"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, KeyRound, LoaderCircle, LogOut, RefreshCw } from "lucide-react";
import { fetchLocalCodexModels, fetchLocalCodexStatus } from "@/lib/providers-api";
import ProviderDropdown from "./ProviderDropdown";

type Props = {
  initialAccountId: string | null;
  onChange: (accountId: string | null, models: ElectronCodexModel[]) => void;
};

export default function OpenAICodexLoginPanel({ initialAccountId, onChange }: Props) {
  const [available, setAvailable] = useState(false);
  const [chatgptSignInAvailable, setChatgptSignInAvailable] = useState(false);
  const [accounts, setAccounts] = useState<ElectronCodexAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState(initialAccountId || "");
  const [models, setModels] = useState<ElectronCodexModel[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [cliMessage, setCliMessage] = useState("");

  const publish = useCallback(
    (accountId: string | null, accountModels: ElectronCodexModel[]) => {
      setSelectedAccountId(accountId || "");
      setModels(accountModels);
      onChange(accountId, accountModels);
    },
    [onChange],
  );

  const loadModels = useCallback(
    async (accountId: string) => {
      const electron = window.electron;
      if (electron?.codex) {
        const accountModels = await electron.codex.models(accountId);
        publish(accountId, accountModels);
        setError("");
      } else {
        const accountModels = await fetchLocalCodexModels();
        publish(accountId, accountModels);
        setError("");
      }
    },
    [publish],
  );

  useEffect(() => {
    let cancelled = false;
    const initialize = async () => {
      const electron = window.electron;
      if (!electron?.isElectron || !electron.codex) {
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
                "Codex login connects directly to your local /usr/local/bin/codex CLI session. Open the AverQel Desktop app (run ./start-local.sh desktop or pnpm electron) to link your logged-in ChatGPT account.",
            );
            return;
          }
          const matching = (status.accounts || []).find((account) => account.id === initialAccountId);
          const account =
            matching ||
            (status.accounts || []).find((candidate) => candidate.source === "codex_cli") ||
            (status.accounts && status.accounts.length === 1 ? status.accounts[0] : null);
          if (account) {
            setSelectedAccountId(account.id);
            if (status.models && status.models.length > 0) {
              publish(account.id, status.models);
            } else {
              await loadModels(account.id);
            }
          } else {
            publish(null, []);
          }
        } catch {
          if (!cancelled) {
            setAvailable(false);
            setError(
              "Codex login connects directly to your local /usr/local/bin/codex CLI session. Open the AverQel Desktop app (run ./start-local.sh desktop or pnpm electron) to link your logged-in ChatGPT account.",
            );
          }
        }
        return;
      }
      try {
        const status = await electron.codex.status();
        if (cancelled) return;
        setAvailable(status.available);
        setChatgptSignInAvailable(status.chatgptSignInAvailable ?? status.available);
        setAccounts(status.accounts);
        setCliMessage(status.cliMessage || "");
        if (!status.available) {
          setError(status.message || "Secure operating-system credential storage is unavailable.");
          return;
        }
        const matching = status.accounts.find((account) => account.id === initialAccountId);
        const account =
          matching ||
          status.accounts.find((candidate) => candidate.source === "codex_cli") ||
          (status.accounts.length === 1 ? status.accounts[0] : null);
        if (account) {
          setSelectedAccountId(account.id);
          await loadModels(account.id);
        } else {
          publish(null, []);
        }
      } catch (loadError) {
        if (!cancelled) {
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
  }, [initialAccountId, loadModels, publish]);

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

  const handleConnect = async (
    requestedSource?: "chatgpt" | "codex_cli",
    startNewChatGPTAccount = false,
  ) => {
    const electron = window.electron;
    if (!electron?.codex) {
      setBusy(true);
      setError("");
      setNotice("");
      try {
        const status = await fetchLocalCodexStatus();
        setAccounts(status.accounts || []);
        setChatgptSignInAvailable(status.chatgptSignInAvailable ?? false);
        setCliMessage(status.cliMessage || "");
        if (status.accounts && status.accounts.length > 0) {
          const account = status.accounts[0];
          setSelectedAccountId(account.id);
          const modelsList = status.models || (await fetchLocalCodexModels());
          publish(account.id, modelsList);
        } else {
          setError("No active Codex CLI login found on host.");
        }
      } catch (connectError) {
        setError(
          connectError instanceof Error ? connectError.message : "Codex CLI status check failed.",
        );
      } finally {
        setBusy(false);
      }
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const selected = accounts.find((account) => account.id === selectedAccountId);
      const source = requestedSource || selected?.source || "chatgpt";
      const accountId = startNewChatGPTAccount ? null : selectedAccountId || null;
      const result = await electron.codex.connect(accountId, source);
      const status = await electron.codex.status();
      setAccounts(status.accounts);
      setChatgptSignInAvailable(status.chatgptSignInAvailable ?? status.available);
      setCliMessage(status.cliMessage || "");
      publish(result.account.id, result.models);
      if (result.modelError) setError(result.modelError);
    } catch (connectError) {
      setError(connectError instanceof Error ? connectError.message : "ChatGPT sign-in failed.");
    } finally {
      setBusy(false);
    }
  };

  const handleDisconnect = async () => {
    const electron = window.electron;
    if (!selectedAccountId) return;
    if (!electron?.codex) {
      publish(null, []);
      setNotice("Codex CLI session unlinked from form.");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await electron.codex.disconnect(selectedAccountId);
      const status = await electron.codex.status();
      setAccounts(status.accounts);
      setChatgptSignInAvailable(status.chatgptSignInAvailable ?? status.available);
      setCliMessage(status.cliMessage || "");
      publish(null, []);
      setNotice(
        result.revoked
          ? "Removed from this desktop. OpenAI confirmed the revocation request."
          : "Removed from this desktop, but OpenAI could not confirm revocation. Check ChatGPT Settings → Usage to remove AverQel’s access.",
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
        Checking secure desktop sign-in…
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
            AverQel can use your existing Codex CLI ChatGPT sign-in. If it is not available, sign in
            with ChatGPT in your system browser. Credentials stay in the local sign-in store.
            DeepSpace prompts still pass through the AverQel server.
          </p>
        </div>
      </div>

      {!available ? (
        <p
          role="alert"
          className="rounded-xl border border-amber-500/40 bg-amber-50 dark:bg-amber-950/40 p-3 text-xs font-bold text-amber-900 dark:text-amber-200 shadow-2xs"
        >
          {error || "Secure operating-system credential storage is unavailable."}
        </p>
      ) : (
        <>
          {accounts.length > 0 && (
            <div className="space-y-1.5">
              <span className="text-slate-700 dark:text-slate-300 font-mono text-[10px] font-black tracking-[0.2em] uppercase">
                Connected ChatGPT account
              </span>
              <ProviderDropdown
                value={selectedAccountId}
                onChange={(val) => void handleSelect(val)}
                disabled={busy}
                placeholder="Choose a saved account"
                options={accounts.map((account) => ({
                  value: account.id,
                  label:
                    account.source === "codex_cli"
                      ? `Use signed-in Codex CLI account${account.planType ? ` (${account.planType.toUpperCase()})` : ""}`
                      : account.name || account.email || "ChatGPT account",
                  hint: account.source === "codex_cli" ? "Host CLI session" : "Browser session",
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
                  <LoaderCircle size={14} className="animate-spin text-emerald-600 dark:text-emerald-400" />
                ) : (
                  <RefreshCw size={14} />
                )}
                <span>{models.length > 0 ? "Refresh models" : "Retry model list"}</span>
              </button>
            ) : null}
            {selectedAccount ? (
              <button
                type="button"
                onClick={() => void handleConnect(selectedAccount.source || "chatgpt")}
                disabled={busy}
                className="inline-flex h-9.5 items-center gap-2 rounded-xl border border-emerald-500/50 bg-gradient-to-r from-emerald-600 to-teal-600 px-4 text-xs font-black !text-white shadow-xs hover:from-emerald-700 hover:to-teal-700 hover:shadow-sm transition disabled:opacity-50 dark:from-emerald-500 dark:to-teal-500 dark:!text-slate-950"
              >
                {busy ? (
                  <LoaderCircle size={14} className="animate-spin text-white dark:text-slate-950" />
                ) : (
                  <RefreshCw size={14} />
                )}
                <span>{selectedAccount.source === "codex_cli" ? "Use Codex CLI login" : "Sign in again"}</span>
              </button>
            ) : null}
            {selectedAccount?.source !== "codex_cli" && selectedAccountId ? (
              <button
                type="button"
                onClick={() => void handleDisconnect()}
                disabled={busy}
                className="inline-flex h-9.5 items-center gap-2 rounded-xl border border-rose-300 dark:border-rose-800 bg-rose-50 dark:bg-rose-950/40 px-4 text-xs font-bold text-rose-700 dark:text-rose-300 hover:bg-rose-100 dark:hover:bg-rose-900/50 transition disabled:opacity-50 shadow-2xs"
              >
                <LogOut size={14} />
                <span>Remove from this desktop</span>
              </button>
            ) : null}
            {chatgptSignInAvailable ? (
              <button
                type="button"
                onClick={() => void handleConnect("chatgpt", true)}
                disabled={busy}
                className="inline-flex h-9.5 items-center gap-2 rounded-xl border border-slate-300 dark:border-white/15 bg-white dark:bg-slate-800 px-4 text-xs font-bold text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-700 transition disabled:opacity-50 shadow-2xs"
              >
                {busy ? (
                  <LoaderCircle size={14} className="animate-spin text-emerald-600 dark:text-emerald-400" />
                ) : (
                  <KeyRound size={14} />
                )}
                <span>{selectedAccount?.source === "codex_cli"
                  ? "Sign in with ChatGPT instead"
                  : "Continue with ChatGPT"}</span>
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
