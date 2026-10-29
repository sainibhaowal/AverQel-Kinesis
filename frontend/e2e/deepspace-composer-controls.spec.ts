import { expect, test } from "./fixtures";

/**
 * The composer's own controls: the workspace a conversation works in, the
 * agent mode it runs in, and the keyboard that reaches the workspace's
 * buttons.
 */
test.describe("DeepSpace composer controls", () => {
  // A fresh isolated Next server needs extra time to compile this route.
  test.describe.configure({ timeout: 60_000 });

  test("offers the workspaces and the agent modes in the composer", async ({
    authenticatedPage,
  }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    await expect(
      authenticatedPage.getByRole("button", { name: "Workspace", exact: true }),
    ).toBeVisible();
    await expect(authenticatedPage.getByRole("button", { name: "Agent mode" })).toBeVisible();
  });

  test("offers the file and the clipboard buttons in the composer", async ({
    authenticatedPage,
  }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    await expect(
      authenticatedPage.getByRole("button", { name: "Attach files from your device" }),
    ).toBeVisible();
    await expect(
      authenticatedPage.getByRole("button", { name: "Paste from the clipboard" }),
    ).toBeVisible();
  });

  test("switches the conversation to another workspace", async ({ authenticatedPage }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    await authenticatedPage.getByRole("button", { name: "Workspace", exact: true }).click();
    await expect(
      authenticatedPage.getByRole("menuitemradio", { name: /default-workspace/ }),
    ).toBeVisible();

    const request = authenticatedPage.waitForRequest(
      (call) => call.url().includes("/workspace") && call.method() === "PUT",
    );
    await authenticatedPage.getByRole("menuitemradio", { name: /default-workspace/ }).click();

    const sent = await request;
    expect(sent.postDataJSON()).toEqual({ workspace: "default" });
  });

  test("creates a workspace from the composer", async ({ authenticatedPage }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    await authenticatedPage.getByRole("button", { name: "Workspace", exact: true }).click();
    await authenticatedPage.getByRole("menuitem", { name: /Add workspace/ }).click();

    const picker = authenticatedPage.getByRole("dialog", { name: "Select Workspace Directory" });
    await expect(picker).toBeVisible();
    await picker.getByRole("button", { name: "billing-service" }).click();

    const request = authenticatedPage.waitForRequest(
      (call) => call.url().includes("/workspaces") && call.method() === "POST",
    );
    await picker.getByRole("button", { name: "Open", exact: true }).click();

    const sent = await request;
    expect(sent.postDataJSON()).toEqual({
      name: "billing-service",
      path: "/home/e2e/billing-service",
    });
  });

  test("switches the conversation to another agent mode", async ({ authenticatedPage }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    await authenticatedPage.getByRole("button", { name: "Agent mode" }).click();
    await expect(
      authenticatedPage.getByRole("menuitemradio", { name: /Programmatic tool calling/ }),
    ).toBeVisible();

    const request = authenticatedPage.waitForRequest(
      (call) => call.url().includes("/preset") && call.method() === "PUT",
    );
    await authenticatedPage
      .getByRole("menuitemradio", { name: /Programmatic tool calling/ })
      .click();

    const sent = await request;
    expect(sent.postDataJSON()).toEqual({ preset: "ptc" });
  });

  test("reaches the split view with the keyboard", async ({ authenticatedPage }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    const split = authenticatedPage.getByRole("button", { name: "Split view" });
    await expect(split).toBeVisible();

    await authenticatedPage.keyboard.press("Control+Backslash");
    await expect(split).toHaveAttribute("aria-pressed", "true");
  });
});
