import { render } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import ToastProvider from "../app/components/layout/ToastProvider";

describe("ToastProvider", () => {
  it("does not render the client-only toaster during SSR", () => {
    const markup = renderToString(<ToastProvider>App</ToastProvider>);

    expect(markup).toContain("App");
    expect(markup).not.toContain("data-rht-toaster");
  });

  it("places desktop notifications below the window controls strip", () => {
    const { container } = render(<ToastProvider>App</ToastProvider>);
    const toaster = container.querySelector<HTMLElement>("[data-rht-toaster]");

    expect(toaster?.style.top).toBe("calc(var(--averqel-desktop-chrome-height, 0px) + 16px)");
  });
});
