import { redirect } from "next/navigation";
import { describe, expect, it, vi } from "vitest";

import NotificationSettingsPage from "../app/dashboard/settings/notifications/page";

vi.mock("next/navigation", () => ({
  redirect: vi.fn(),
}));

describe("notification preferences page navigation", () => {
  it("redirects to providers settings", () => {
    NotificationSettingsPage();
    expect(redirect).toHaveBeenCalledWith("/dashboard/settings/providers");
  });
});
