export {};

declare global {
  type CodexAccount = {
    id: string;
    email: string | null;
    name: string | null;
    connected: boolean;
    source?: "chatgpt" | "codex_cli";
    planType?: string | null;
  };

  type CodexModel = {
    id: string;
    name: string;
    kind: "chat";
    supportedReasoningEfforts?: string[];
    reasoningEffortValues?: Record<string, string>;
  };

  type ElectronCodexAccount = CodexAccount;
  type ElectronCodexModel = CodexModel;

  type ElectronLocalRuntimeStreamFrame = {
    streamId: string;
    kind: "event" | "error" | "closed";
    event?: Record<string, unknown>;
    message?: string;
    httpStatus?: number;
    code?: string;
  };

  interface Window {
    electron?: {
      isElectron: true;
      window: {
        minimize: () => Promise<void>;
        close: () => Promise<void>;
        toggleMaximize: () => Promise<boolean>;
        isMaximized: () => Promise<boolean>;
        isDevToolsEnabled?: () => Promise<boolean>;
        unlockDevTools?: () => Promise<boolean>;
        toggleDevTools?: () => Promise<boolean>;
      };
      openExternal: (url: string) => Promise<boolean>;
      fileDrag?: {
        prepare: (input: {
          name: string;
          contentType: string;
          bytes: Uint8Array;
        }) => Promise<{ handle: string; name: string }>;
        start: (handle: string) => Promise<boolean>;
        cancel: (handle: string) => Promise<boolean>;
        release: (handle: string) => Promise<boolean>;
      };
      localRuntime?: {
        status: () => Promise<{ available: boolean; providers: string[]; active_streams: number }>;
        startStream: (input: {
          streamId: string;
          providerType: "lmstudio" | "ollama";
          baseUrl: string;
          operation?: "chat" | "health" | "models" | "embed" | "pull";
          request?: Record<string, unknown>;
        }) => Promise<unknown>;
        cancelStream: (streamId: string) => Promise<boolean>;
        onStreamEvent: (callback: (frame: ElectronLocalRuntimeStreamFrame) => void) => () => void;
      };
    };
  }
}
