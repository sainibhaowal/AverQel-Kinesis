import { redirect } from "next/navigation";
import { describe, expect, it, vi } from "vitest";

import StorageSettingsPage from "../app/dashboard/settings/storage/page";

vi.mock("next/navigation", () => ({
  redirect: vi.fn(),
}));

describe("storage settings page navigation", () => {
  it("redirects to providers settings", () => {
    StorageSettingsPage();
    expect(redirect).toHaveBeenCalledWith("/dashboard/settings/providers");
  });
});
