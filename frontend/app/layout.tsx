import type { Metadata } from "next";
import "./globals.css";
import { AuthProvider } from "./context/AuthContext";
import { ThemeProvider } from "./context/ThemeContext";
import ToastProvider from "@/app/components/layout/ToastProvider";
import CursorSweepProvider from "@/app/components/layout/CursorSweepProvider";
import { PRODUCT_NAME } from "@/lib/brand";
import {
  CONTENT_SECURITY_POLICY,
  DEVELOPMENT_CONTENT_SECURITY_POLICY,
} from "@/lib/content-security-policy";
import AverQelDialogHost from "@/app/components/ui/AverQelDialogHost";
import AppTooltipLayer from "@/app/components/ui/AppTooltipLayer";

export const metadata: Metadata = {
  title: PRODUCT_NAME,
  description: `${PRODUCT_NAME} combines grounded chat, DeepSpace execution, proactive workspaces, connector automation, and tenant-isolated security for grounded AI work.`,
  keywords: [
    "document intelligence",
    "agentic OS",
    "DeepSpace",
    "proactive workspace",
    "connector automation",
    "AI search",
    "enterprise AI",
    "tenant isolation",
  ],
  openGraph: {
    title: `${PRODUCT_NAME} | Agentic OS for documents, connectors, and autonomous work`,
    description:
      "Grounded chat, DeepSpace execution, proactive workspaces, and connector automation in one tenant-isolated product.",
    type: "website",
    siteName: PRODUCT_NAME,
  },
  icons: {
    icon: "/brand/AntiAtom_Icon.svg",
    shortcut: "/brand/AntiAtom_Icon.svg",
    apple: "/brand/AntiAtom_Icon.svg",
  },
};

const contentSecurityPolicy =
  process.env.NODE_ENV === "development"
    ? DEVELOPMENT_CONTENT_SECURITY_POLICY
    : CONTENT_SECURITY_POLICY;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Browser extensions can add attributes to <html> before React hydrates.
  // The document root is deliberately hydration-tolerant; application UI
  // remains checked normally below it.
  return (
    <html
      lang="en"
      className="dark scroll-smooth"
      data-scroll-behavior="smooth"
      suppressHydrationWarning
    >
      <head>
        <meta
          httpEquiv="Content-Security-Policy"
          content={contentSecurityPolicy}
        />
        <link rel="preload" as="image" href="/brand/AntiAtom_Icon.svg" type="image/svg+xml" />
      </head>
      <body className="antialiased" suppressHydrationWarning>
        <div
          id="averqel-electron-titlebar-spacer"
          aria-hidden="true"
          style={{ display: "none" }}
        />
        <ThemeProvider>
          <CursorSweepProvider />
          <ToastProvider>
            <AverQelDialogHost />
            <AppTooltipLayer />
            <AuthProvider>
              <div
                data-averqel-app-root
                className="flex h-full min-h-0 flex-1 flex-col overflow-hidden"
              >
                <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
              </div>
            </AuthProvider>
          </ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
