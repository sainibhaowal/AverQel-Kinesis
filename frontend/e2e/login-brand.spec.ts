import { expect, test, type Page } from "@playwright/test";

async function expectKinesisBrand(page: Page): Promise<void> {
  const lockup = page.locator("[data-brand-lockup]");
  await expect(lockup).toBeVisible();
  await expect(lockup.locator('img[src*="AntiAtom_Icon.svg"]')).toBeVisible();
  await expect(lockup.getByText("Kinesis")).toBeVisible();
}

test("local app entry shows the Kinesis brand lockup", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/dashboard\/deepspace$/);
  await expectKinesisBrand(page);
});

test("dashboard keeps the same accessible Kinesis lockup", async ({ page }) => {
  await page.goto("/dashboard");
  await expectKinesisBrand(page);
});
