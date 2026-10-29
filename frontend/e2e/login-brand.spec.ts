import { expect, test } from "@playwright/test";

test("login page uses the AntiAtom icon and AverQel Kinesis wordmark", async ({ page }) => {
  await page.goto("/auth/login");

  const lockup = page.getByRole("img", { name: "Kinesis" });
  await expect(lockup).toBeVisible();
  await expect(lockup.locator('img[src*="AntiAtom_Icon.svg"]')).toBeVisible();
  await expect(lockup.getByText("Kinesis")).toBeVisible();
});

test("signup page uses the same AntiAtom lockup", async ({ page }) => {
  await page.goto("/auth/signup");

  const lockup = page.getByRole("img", { name: "Kinesis" });
  await expect(lockup).toBeVisible();
  await expect(lockup.locator('img[src*="AntiAtom_Icon.svg"]')).toBeVisible();
  await expect(lockup.getByText("Kinesis")).toBeVisible();
});
