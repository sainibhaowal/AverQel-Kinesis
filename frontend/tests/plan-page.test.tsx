import { redirect } from "next/navigation";
import { describe, expect, it, vi } from "vitest";

import PlanSettingsPage from "../app/dashboard/settings/plan/page";

vi.mock("next/navigation", () => ({
  redirect: vi.fn(),
}));

describe("plan settings page navigation", () => {
  it("redirects to providers settings", () => {
    PlanSettingsPage();
    expect(redirect).toHaveBeenCalledWith("/dashboard/settings/providers");
  });
});
