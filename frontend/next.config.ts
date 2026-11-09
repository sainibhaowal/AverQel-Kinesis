import path from "node:path";
import type { NextConfig } from "next";
import {
  CONTENT_SECURITY_POLICY_HEADER,
  DEVELOPMENT_CONTENT_SECURITY_POLICY_HEADER,
} from "./lib/content-security-policy";

const contentSecurityPolicyHeader =
  process.env.NODE_ENV === "development"
    ? DEVELOPMENT_CONTENT_SECURITY_POLICY_HEADER
    : CONTENT_SECURITY_POLICY_HEADER;
const isDesktopStaticExport = process.env.BUILD_TARGET === "desktop";

const nextConfig: NextConfig = {
  reactCompiler: true,
  output: isDesktopStaticExport
    ? "export"
    : process.env.STANDALONE === "1"
      ? "standalone"
      : undefined,
  images: {
    unoptimized: true,
  },
  // Static exports cannot apply Next server headers/rewrites. The Electron
  // loopback server supplies the same security headers for packaged pages.
  ...(isDesktopStaticExport
    ? {}
    : {
        async headers() {
          return [
            {
              source: "/(.*)",
              headers: [
                {
                  key: "Content-Security-Policy",
                  value: contentSecurityPolicyHeader,
                },
                { key: "X-Content-Type-Options", value: "nosniff" },
              ],
            },
          ];
        },
        async rewrites() {
          if (process.env.PLAYWRIGHT_E2E !== "1") {
            return [];
          }
          return [
            {
              source: "/api/v1/:path*",
              // The isolated Playwright server must never proxy into a developer's
              // running API. Keep normal web/desktop requests on the local API.
              destination:
                process.env.PLAYWRIGHT_E2E === "1"
                  ? "http://127.0.0.1:9/api/v1/:path*"
                  : "http://127.0.0.1:10000/api/v1/:path*",
            },
          ];
        },
      }),
  // Playwright's local browser uses the loopback origin for Next assets.
  allowedDevOrigins: ["127.0.0.1"],
  // Explicitly opt in to the Next 16 default bundler while retaining the
  // webpack fallback used by the desktop build and CI compatibility checks.
  turbopack: {
    root: path.resolve(__dirname),
  },
  // Vega renders charts in the browser. Its optional Node canvas adapter is not
  // needed in the server bundle and is intentionally unavailable in production.
  webpack: (config) => {
    config.resolve ??= {};
    config.resolve.alias = {
      ...(config.resolve.alias ?? {}),
      canvas: false,
    };
    return config;
  },
};

export default nextConfig;
