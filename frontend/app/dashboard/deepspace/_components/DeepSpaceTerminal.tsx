"use client";

import { useEffect, useRef, useState } from "react";
import type { FitAddon } from "@xterm/addon-fit";
import type { Terminal as XtermTerminal } from "@xterm/xterm";

import { terminalWebSocketUrl } from "../_lib/workspace-panel-api";

interface DeepSpaceTerminalProps {
  /** The conversation that owns the terminal session. */
  conversationId: string;
  /** The terminal session to attach to. */
  sessionId: string;
  /** The working directory returned while listing the session. */
  initialCwd?: string;
}

type TerminalConnectionState = "connecting" | "connected" | "closed" | "error";

/**
 * Attach xterm.js to one server-side PTY.
 *
 * The browser sends exactly the bytes xterm emits for keyboard input and the
 * server sends raw PTY output back over the authenticated WebSocket.  This
 * keeps ANSI escape sequences, cursor movement, colors, interactive programs,
 * Ctrl-key sequences, and terminal resize events intact.
 */
export default function DeepSpaceTerminal({
  conversationId,
  sessionId,
  initialCwd,
}: DeepSpaceTerminalProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const terminalRef = useRef<XtermTerminal | null>(null);
  const [state, setState] = useState<TerminalConnectionState>("connecting");
  const [shell, setShell] = useState<string | null>(null);
  const [pty, setPty] = useState<boolean | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [cwd, setCwd] = useState<string | null>(initialCwd ?? null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let disposed = false;
    let fitAddon: FitAddon | null = null;
    let inputDisposable: { dispose: () => void } | null = null;
    let resizeDisposable: { dispose: () => void } | null = null;
    let resizeObserver: ResizeObserver | null = null;

    const setSafeState = (next: TerminalConnectionState) => {
      if (!disposed) setState(next);
    };

    const sendResize = (columns: number, rows: number) => {
      const socket = socketRef.current;
      if (socket?.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({ type: "resize", columns, rows }));
    };

    const connect = async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
      ]);
      if (disposed || !hostRef.current) return;

      const terminal = new Terminal({
        allowProposedApi: false,
        convertEol: false,
        cursorBlink: true,
        fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, monospace',
        fontSize: 12,
        scrollback: 10_000,
        theme: {
          background: "#08100d",
          foreground: "#d8e5df",
          cursor: "#25d6a2",
          cursorAccent: "#08100d",
          selectionBackground: "rgba(37, 214, 162, 0.28)",
          black: "#08100d",
          brightBlack: "#52635c",
          red: "#ff6b76",
          brightRed: "#ff8991",
          green: "#3fe08e",
          brightGreen: "#76f5b0",
          yellow: "#f4c95d",
          brightYellow: "#ffe18a",
          blue: "#7db7ff",
          brightBlue: "#9ac7ff",
          magenta: "#d19cff",
          brightMagenta: "#e1baff",
          cyan: "#55d6e8",
          brightCyan: "#91ecf5",
          white: "#d8e5df",
          brightWhite: "#f5fffb",
        },
      });
      const addon = new FitAddon();
      terminal.loadAddon(addon);
      terminal.open(hostRef.current);
      terminalRef.current = terminal;
      fitAddon = addon;

      const fit = () => {
        if (disposed) return;
        try {
          addon.fit();
        } catch {
          // The host can be between layout passes while the side panel opens.
        }
      };

      const socket = new WebSocket(terminalWebSocketUrl(conversationId, sessionId));
      socket.binaryType = "arraybuffer";
      socketRef.current = socket;

      inputDisposable = terminal.onData((data) => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "input", data }));
        }
      });
      resizeDisposable = terminal.onResize(({ cols, rows }) => sendResize(cols, rows));
      if (typeof ResizeObserver !== "undefined") {
        resizeObserver = new ResizeObserver(fit);
        resizeObserver.observe(host);
      }
      requestAnimationFrame(fit);

      socket.onopen = () => {
        setSafeState("connected");
        setMessage(null);
        fit();
        terminal.focus();
      };
      socket.onmessage = (event) => {
        if (disposed) return;
        if (typeof event.data !== "string") {
          terminal.write(new Uint8Array(event.data as ArrayBuffer));
          return;
        }
        let packet: unknown;
        try {
          packet = JSON.parse(event.data) as unknown;
        } catch {
          terminal.write(event.data);
          return;
        }
        if (!packet || typeof packet !== "object") return;
        const record = packet as Record<string, unknown>;
        if (record.type === "output" && typeof record.data === "string") {
          terminal.write(record.data);
        } else if (record.type === "ready") {
          setShell(typeof record.shell === "string" ? record.shell : null);
          setPty(record.pty === true);
          setCwd(typeof record.cwd === "string" ? record.cwd : initialCwd ?? null);
          setSafeState("connected");
          fit();
        } else if (record.type === "exit") {
          setSafeState("closed");
          setMessage("The shell session has exited.");
        } else if (record.type === "error" && typeof record.message === "string") {
          setMessage(record.message);
        }
      };
      socket.onerror = () => {
        setSafeState("error");
        setMessage("The terminal stream could not connect.");
      };
      socket.onclose = () => {
        if (!disposed) setSafeState("closed");
      };
    };

    void connect().catch((cause: unknown) => {
      if (disposed) return;
      setSafeState("error");
      setMessage(cause instanceof Error ? cause.message : "The terminal could not start.");
    });

    return () => {
      disposed = true;
      resizeObserver?.disconnect();
      inputDisposable?.dispose();
      resizeDisposable?.dispose();
      fitAddon?.dispose();
      socketRef.current?.close();
      socketRef.current = null;
      terminalRef.current?.dispose();
      terminalRef.current = null;
    };
  }, [conversationId, initialCwd, sessionId]);

  const statusLabel =
    state === "connecting"
      ? "Connecting"
      : state === "connected"
        ? `Connected${shell ? ` · ${shell}` : ""}${pty ? " · PTY" : ""}`
        : state === "error"
          ? "Connection error"
          : "Session closed";

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-[#08100d]">
      <div className="border-border-subtle text-muted-foreground flex shrink-0 items-center gap-2 border-b px-3 py-1 text-[10px]">
        <span
          className={`h-1.5 w-1.5 rounded-full ${
            state === "connected"
              ? "bg-emerald-400"
              : state === "error"
                ? "bg-rose-400"
                : "bg-amber-300"
          }`}
        />
        <span>{statusLabel}</span>
        {cwd ? (
          <span className="text-primary/70 min-w-0 truncate" title={cwd}>
            · {cwd}
          </span>
        ) : null}
        {message ? <span className="truncate text-rose-200">{message}</span> : null}
      </div>
      <div
        ref={hostRef}
        role="textbox"
        aria-label="Interactive terminal"
        tabIndex={0}
        onClick={() => terminalRef.current?.focus()}
        className="min-h-0 flex-1 overflow-hidden px-1 py-1 outline-none"
      />
    </div>
  );
}
