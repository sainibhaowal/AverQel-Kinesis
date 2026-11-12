import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import NotificationCenter from "../app/components/layout/NotificationCenter";

const { fetchWithAuth, routerPush } = vi.hoisted(() => ({
  fetchWithAuth: vi.fn(),
  routerPush: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ fetchWithAuth }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/dashboard",
  useRouter: () => ({ push: routerPush }),
}));

describe("NotificationCenter desktop layout", () => {
  beforeEach(() => {
    fetchWithAuth.mockReset();
    fetchWithAuth.mockResolvedValue(new Response("[]", { status: 200 }));
  });

  it("positions the drawer and backdrop below the desktop title bar", async () => {
    render(<NotificationCenter />);

    await waitFor(() => expect(fetchWithAuth).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Notifications" }));
    await screen.findByRole("heading", { name: "Notifications" });

    const chromeOffset = "var(--averqel-desktop-chrome-height, 0px)";
    const layers = Array.from(document.body.querySelectorAll<HTMLElement>("div.fixed"));
    const offsetLayers = layers.filter((layer) => layer.style.top === chromeOffset);

    expect(offsetLayers).toHaveLength(2);
    expect(offsetLayers.every((layer) => layer.classList.contains("bottom-0"))).toBe(true);
  });
});
