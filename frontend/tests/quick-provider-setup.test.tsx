import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import QuickProviderSetup from "../app/components/onboarding/QuickProviderSetup";

const mocks = vi.hoisted(() => ({
  listSupportedProviderTypes: vi.fn(),
  previewProviderModels: vi.fn(),
  createProvider: vi.fn(),
}));

vi.mock("@/lib/providers-api", () => mocks);
vi.mock("@/app/components/providers/OpenAICodexLoginPanel", () => ({
  default: () => <div>Desktop Codex sign-in</div>,
}));

const openAi = {
  provider_type: "openai",
  display_name: "OpenAI API",
  auth_modes: ["api_key", "codex_desktop"],
  supports_chat: true,
  supports_embeddings: true,
  supports_reranking: false,
  supports_web_search: false,
  supports_model_listing: true,
  supports_model_install: false,
  supports_account_linking: false,
  is_local: false,
};

afterEach(() => {
  mocks.listSupportedProviderTypes.mockReset();
  mocks.previewProviderModels.mockReset();
  mocks.createProvider.mockReset();
  Reflect.deleteProperty(window, "electron");
});

describe("QuickProviderSetup", () => {
  it("discovers a model and saves a named provider with that default model", async () => {
    mocks.listSupportedProviderTypes.mockResolvedValue([openAi]);
    mocks.previewProviderModels.mockResolvedValue([
      {
        id: null,
        provider_config_id: null,
        model_name: "gpt-example",
        model_kind: "chat",
        display_name: "GPT Example",
        context_window: null,
        capabilities_json: {},
        is_available: true,
        last_seen_at: null,
      },
    ]);
    mocks.createProvider.mockResolvedValue({
      display_name: "Work OpenAI",
      default_chat_model: "gpt-example",
    });
    const onConfigured = vi.fn();
    const onBusyChange = vi.fn();

    render(
      <QuickProviderSetup
        onConfigured={onConfigured}
        onOpenSettings={vi.fn()}
        onBusyChange={onBusyChange}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /provider.*openai api/i }));
    expect(await screen.findByRole("listbox", { name: "Provider" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "OpenAI API" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.change(screen.getByLabelText("Connection name"), {
      target: { value: "Work OpenAI" },
    });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "secret-test-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Find chat models" }));

    await screen.findByRole("option", { name: "GPT Example" });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(mocks.createProvider).toHaveBeenCalledOnce());
    expect(mocks.createProvider).toHaveBeenCalledWith(
      expect.objectContaining({
        provider_type: "openai",
        display_name: "Work OpenAI",
        auth_mode: "api_key",
        api_key: "secret-test-key",
        supports_chat: true,
        default_chat_model: "gpt-example",
      }),
    );
    expect(onConfigured).toHaveBeenCalledWith("Work OpenAI");
    expect(onBusyChange.mock.calls).toEqual([[true], [false]]);
    expect(await screen.findByRole("status")).toHaveTextContent("connected with gpt-example");
  });

  it("allows local model connections in the offline build", async () => {
    mocks.listSupportedProviderTypes.mockResolvedValue([
      {
        ...openAi,
        provider_type: "ollama",
        display_name: "Ollama",
        auth_modes: ["local_no_key"],
        supports_embeddings: true,
        supports_model_install: true,
        is_local: true,
      },
    ]);

    render(
      <QuickProviderSetup onConfigured={vi.fn()} onOpenSettings={vi.fn()} onBusyChange={vi.fn()} />,
    );

    expect(
      await screen.findByText(/averqel connects directly to this local runtime/i),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Runtime URL")).not.toBeDisabled();
  });

  it("shows local providers in the offline web picker", async () => {
    mocks.listSupportedProviderTypes.mockResolvedValue([
      openAi,
      {
        ...openAi,
        provider_type: "ollama",
        display_name: "Ollama",
        auth_modes: ["local_no_key"],
        is_local: true,
      },
    ]);

    render(
      <QuickProviderSetup onConfigured={vi.fn()} onOpenSettings={vi.fn()} onBusyChange={vi.fn()} />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /provider.*openai api/i }));
    const ollamaOption = screen.getByRole("option", { name: "Ollama" });
    expect(ollamaOption).not.toBeDisabled();
    expect(ollamaOption).toHaveAttribute("aria-disabled", "false");
    expect(screen.getByRole("button", { name: /provider.*openai api/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("saves local setup without a desktop runtime target", async () => {
    Object.defineProperty(window, "electron", {
      configurable: true,
      value: { isElectron: true, codex: {}, localRuntime: {} },
    });
    const ollama = {
      ...openAi,
      provider_type: "ollama",
      display_name: "Ollama",
      auth_modes: ["local_no_key"],
      supports_embeddings: true,
      supports_model_install: true,
      is_local: true,
    };
    mocks.listSupportedProviderTypes.mockResolvedValue([ollama]);
    mocks.previewProviderModels.mockResolvedValue([
      {
        id: null,
        provider_config_id: null,
        model_name: "llama3.2",
        model_kind: "chat",
        display_name: "Llama 3.2",
        context_window: null,
        capabilities_json: {},
        is_available: true,
        last_seen_at: null,
      },
    ]);
    mocks.createProvider.mockResolvedValue({
      display_name: "Ollama",
      default_chat_model: "llama3.2",
    });

    render(
      <QuickProviderSetup onConfigured={vi.fn()} onOpenSettings={vi.fn()} onBusyChange={vi.fn()} />,
    );

    expect(await screen.findByLabelText("Runtime URL")).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Find chat models" }));
    await screen.findByRole("option", { name: "Llama 3.2" });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(mocks.createProvider).toHaveBeenCalledOnce());
    expect(mocks.createProvider).toHaveBeenCalledWith(
      expect.objectContaining({
        provider_type: "ollama",
      }),
    );
    expect(mocks.createProvider.mock.calls[0]?.[0]).not.toHaveProperty("metadata_json");
  });
});
