import { afterEach, describe, expect, it } from "vitest";

import { getApiBaseUrl, resolveApiBaseUrl, resolveApiUrl } from "../lib/api";

describe("resolveApiBaseUrl", () => {
  const originalWindow = globalThis.window;
  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;
  const setWindow = (value: Window | undefined) => {
    Object.defineProperty(globalThis, "window", {
      value,
      configurable: true,
      writable: true,
    });
  };

  afterEach(() => {
    setWindow(originalWindow);
    if (originalApiUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL;
    else process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  });

  it("uses same-origin api path for local docker frontend", () => {
    setWindow({} as Window);
    expect(
      resolveApiBaseUrl({
        hostname: "127.0.0.1",
        protocol: "http:",
        port: "1030",
      }),
    ).toBe("/api/v1");
  });

  it("uses same-origin https api path for production-style local hostnames", () => {
    setWindow(undefined);
    expect(
      resolveApiBaseUrl({
        hostname: "averqel.ravi",
        protocol: "https:",
        port: "443",
      }),
    ).toBe("https://averqel.ravi/api/v1");
  });

  it("keeps local docker clients on same-origin routing even if an env url exists", () => {
    setWindow({} as Window);
    expect(
      resolveApiBaseUrl({
        hostname: "localhost",
        protocol: "http:",
        port: "1030",
        envUrl: "https://api.example.com/api/v1",
      }),
    ).toBe("/api/v1");
  });

  it("uses the local API for Electron when no desktop URL is configured", () => {
    setWindow({ electron: { isElectron: true } } as Window);
    delete process.env.NEXT_PUBLIC_API_URL;

    expect(getApiBaseUrl()).toBe("http://127.0.0.1:11000/api/v1");
    expect(
      resolveApiBaseUrl({
        hostname: "127.0.0.1",
        protocol: "http:",
        port: "10030",
      }),
    ).toBe("http://127.0.0.1:11000/api/v1");
  });

  it("rejects a non-loopback API URL in Electron", () => {
    setWindow({ electron: { isElectron: true } } as Window);
    process.env.NEXT_PUBLIC_API_URL = "https://averqel.com/api/v1";

    expect(() => getApiBaseUrl()).toThrow("local loopback address");
  });

  it("does not add the api prefix twice for a server-provided artifact URL", () => {
    setWindow({} as Window);

    expect(resolveApiUrl("/api/v1/deepspace/artifacts/artifact-1/content")).toBe(
      "/api/v1/deepspace/artifacts/artifact-1/content",
    );
  });

  it("keeps ordinary endpoint paths relative to the configured API root", () => {
    setWindow({} as Window);

    expect(resolveApiUrl("/deepspace/artifacts/artifact-1/content")).toBe(
      "/api/v1/deepspace/artifacts/artifact-1/content",
    );
  });
});
