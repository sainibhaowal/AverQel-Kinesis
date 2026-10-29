import { redirect } from "next/navigation";
import { describe, expect, it, vi } from "vitest";

import FeedbackPage from "../app/dashboard/feedback/page";

vi.mock("next/navigation", () => ({
  redirect: vi.fn(),
}));

describe("feedback page navigation", () => {
  it("redirects to dashboard settings", () => {
    FeedbackPage();
    expect(redirect).toHaveBeenCalledWith("/dashboard/settings");
  });
});
