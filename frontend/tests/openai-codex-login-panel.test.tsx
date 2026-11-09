import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import OpenAICodexLoginPanel from "@/app/components/providers/OpenAICodexLoginPanel";

const cliAccount: ElectronCodexAccount = {
  id: "0123456789abcdef0123456789abcdef",
  email: null,
  name: "Codex CLI ChatGPT login",
  connected: true,
  source: "codex_cli",
  planType: "plus",
};
const browserAccount: ElectronCodexAccount = {
  id: "fedcba9876543210fedcba9876543210",
  email: "user@example.test",
  name: "User",
  connected: true,
  source: "chatgpt",
};
const model: ElectronCodexModel = { id: "gpt-5-codex", name: "GPT-5 Codex", kind: "chat" };

describe("OpenAICodexLoginPanel", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete window.electron;
  });

  it("uses the local Codex API and keeps browser sign-in as an explicit fallback", async () => {
    const localStatus = {
      available: true,
      chatgptSignInAvailable: true,
      accounts: [cliAccount],
      models: [model],
      cliMessage: "Logged in using ChatGPT",
    };
    const localStatusFetch = vi.spyOn(global, "fetch").mockImplementation((input) => {
      if (String(input).includes("/providers/codex-local/status")) {
        return Promise.resolve(
          new Response(JSON.stringify(localStatus), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(new Response("[]", { status: 200 }));
    });
    const onChange = vi.fn();

    render(<OpenAICodexLoginPanel initialAccountId={null} onChange={onChange} />);

    await waitFor(() => expect(localStatusFetch).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Use Codex CLI login" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Sign in with ChatGPT instead" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "Use signed-in Codex CLI account (PLUS)" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Use Codex CLI login" }));
    await waitFor(() => expect(localStatusFetch).toHaveBeenCalledTimes(2));
    expect(onChange).toHaveBeenCalledWith(cliAccount.id, [model]);
  });

  it("explains when local removal succeeded but OpenAI revocation was not confirmed", async () => {
    const localStatus = {
      available: true,
      chatgptSignInAvailable: true,
      accounts: [browserAccount],
    };
    const fetchSpy = vi.spyOn(global, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/providers/codex-local/status")) {
        return Promise.resolve(new Response(JSON.stringify(localStatus), { status: 200 }));
      }
      if (url.includes("/providers/codex-local/models")) {
        return Promise.resolve(new Response(JSON.stringify([model]), { status: 200 }));
      }
      if (url.includes(`/providers/oauth/openai/accounts/${browserAccount.id}/disconnect`)) {
        return Promise.resolve(
          new Response(JSON.stringify({ account_id: browserAccount.id, revoked: false }), {
            status: 200,
          }),
        );
      }
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    render(<OpenAICodexLoginPanel initialAccountId={browserAccount.id} onChange={vi.fn()} />);
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Remove from this computer" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "OpenAI could not confirm revocation",
    );
    expect(
      fetchSpy.mock.calls.some(
        ([input, init]) =>
          String(input).includes(
            `/providers/oauth/openai/accounts/${browserAccount.id}/disconnect`,
          ) && init?.method === "POST",
      ),
    ).toBe(true);
  });

  it("detects local Codex CLI session via backend in browser mode", async () => {
    const mockStatus = {
      available: true,
      chatgptSignInAvailable: false,
      accounts: [cliAccount],
      models: [model],
      cliMessage: "Logged in using ChatGPT",
    };
    vi.spyOn(global, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/providers/codex-local/status")) {
        return Promise.resolve(
          new Response(JSON.stringify(mockStatus), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (url.includes("/providers/codex-local/models")) {
        return Promise.resolve(
          new Response(JSON.stringify([model]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    const onChange = vi.fn();
    render(<OpenAICodexLoginPanel initialAccountId={null} onChange={onChange} />);

    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "Use signed-in Codex CLI account (PLUS)" }),
      ).toBeInTheDocument();
    });
    expect(onChange).toHaveBeenCalledWith(cliAccount.id, [model]);
    expect(screen.getByText("Logged in using ChatGPT")).toBeInTheDocument();
  });
});
