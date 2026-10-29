import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SettingsPage from "../app/dashboard/settings/page";

const authState = vi.hoisted(() => ({
  roles: ["user"] as string[],
}));

vi.mock("@/app/context/AuthContext", () => ({
  useAuth: () => ({
    user: {
      id: "user-1",
      tenant_id: "tenant-1",
      roles: authState.roles,
    },
  }),
}));

describe("settings page", () => {
  beforeEach(() => {
    authState.roles = ["user"];
  });

  it("shows providers in settings for user role", () => {
    render(<SettingsPage />);

    expect(screen.getByRole("link", { name: "Back To DeepSpace" })).toHaveAttribute(
      "href",
      "/dashboard/deepspace",
    );
    expect(screen.getByRole("link", { name: /providers/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /profile/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /plan & storage/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /storage details/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /notifications/i })).not.toBeInTheDocument();
  });

  it("shows providers in settings for editor role", () => {
    authState.roles = ["editor"];

    render(<SettingsPage />);

    expect(screen.getByRole("link", { name: /providers/i })).toBeInTheDocument();
  });
});
