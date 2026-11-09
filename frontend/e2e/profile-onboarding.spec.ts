import { expect, test } from "./fixtures";

test("first-run welcome saves identity and appearance before entering the workspace", async ({
  authenticatedPage,
}) => {
  let profile = {
    user_id: "e2e-local-user",
    tenant_id: "e2e-local-tenant",
    collection_code: "E2E",
    email: "e2e@example.org",
    display_name: null as string | null,
    avatar: null as string | null,
    roles: ["user"],
    status: "active",
    created_at: new Date().toISOString(),
    last_login_at: null,
    totp_enabled: false,
    onboarding_completed: false,
  };

  await authenticatedPage.route("**/api/v1/auth/profile", async (route) => {
    if (route.request().method() === "PUT") {
      const update = route.request().postDataJSON() as Partial<typeof profile>;
      profile = { ...profile, ...update };
    }
    await route.fulfill({ json: profile });
  });

  await authenticatedPage.goto("/dashboard/settings/profile");
  const welcomeDialog = authenticatedPage.locator('[aria-labelledby="welcome-flow-title"]');
  await expect(welcomeDialog).toBeVisible();
  await expect(welcomeDialog.getByRole("heading", { name: /Welcome to Kinesis|Welcome to AverQel/ })).toBeVisible();

  await welcomeDialog.getByRole("button", { name: "Continue" }).click();
  await expect(welcomeDialog.getByRole("heading", { name: /Your work/ })).toBeVisible();
  await welcomeDialog.getByRole("button", { name: "Continue" }).click();
  await expect(
    welcomeDialog.getByRole("heading", { name: "Make it feel like yours" }),
  ).toBeVisible();

  await welcomeDialog.getByLabel("Display name").fill("Ravi Singh");
  await welcomeDialog.getByRole("button", { name: "Orbit avatar" }).click();
  await welcomeDialog.getByRole("button", { name: "Light" }).click();
  await expect
    .poll(() => authenticatedPage.evaluate(() => localStorage.getItem("averqel-theme")))
    .toBe("light");
  await expect(welcomeDialog).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await welcomeDialog.getByRole("button", { name: "Save and continue" }).click();

  await expect(
    welcomeDialog.getByRole("heading", { name: "Connect the AI you want to use" }),
  ).toBeVisible();
  await welcomeDialog.getByRole("button", { name: "Skip setup" }).click();
  await expect(welcomeDialog).toHaveCount(0);

  expect(profile.display_name).toBe("Ravi Singh");
  expect(profile.avatar).toMatch(/^data:image\/svg\+xml/);
  expect(profile.onboarding_completed).toBe(true);


});
