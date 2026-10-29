import { describe, expect, it } from "vitest";

import {
  CONTENT_SECURITY_POLICY,
  DEVELOPMENT_CONTENT_SECURITY_POLICY,
} from "../lib/content-security-policy";

describe("renderer content security policy", () => {
  it("does not enable dynamic code evaluation", () => {
    expect(CONTENT_SECURITY_POLICY).not.toContain("unsafe-eval");
    expect(CONTENT_SECURITY_POLICY).not.toContain("frame-ancestors");
    expect(CONTENT_SECURITY_POLICY).toContain("script-src 'self' 'unsafe-inline'");
  });

  it("allows React development evaluation only in the development policy", () => {
    expect(DEVELOPMENT_CONTENT_SECURITY_POLICY).toContain("'unsafe-eval'");
    expect(DEVELOPMENT_CONTENT_SECURITY_POLICY).toContain(
      "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
    );
  });

  it("allows only the local runtime ports plus secure remote connections", () => {
    expect(CONTENT_SECURITY_POLICY).toContain("http://127.0.0.1:*");
    expect(CONTENT_SECURITY_POLICY).toContain("ws://127.0.0.1:*");
    expect(CONTENT_SECURITY_POLICY).toContain("https:");
    expect(CONTENT_SECURITY_POLICY).toContain("wss:");
    expect(CONTENT_SECURITY_POLICY).toContain("object-src 'none'");
  });
});
