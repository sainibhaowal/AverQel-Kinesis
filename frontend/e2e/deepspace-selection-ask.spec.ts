import { expect, test } from "./fixtures";

/**
 * Selecting text inside DeepSpace must offer Ask DeepSeek and place the
 * passage in the composer without sending a turn.
 */
test.describe("DeepSpace Ask DeepSeek selection", () => {
  // A fresh isolated Next server compiles this route on its first load. That
  // can take over a minute on a local machine, so allow for the compile before
  // the interaction timeout starts to matter.
  test.describe.configure({ timeout: 180_000 });

  test("adds the selected text to the composer without submitting", async ({
    authenticatedPage,
  }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    const card = authenticatedPage.getByRole("button", { name: /Workspace files/ });
    await expect(card).toBeVisible();

    await card.evaluate((element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
      window.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
    });

    const ask = authenticatedPage.getByRole("button", {
      name: "Ask DeepSeek about the selected text",
    });
    await expect(ask).toBeVisible();

    const composer = authenticatedPage.getByPlaceholder("Message DeepSpace...");
    await expect(composer).toHaveValue("");

    await ask.click();

    await expect(composer).toHaveValue(/> Workspace files/);
    await expect(composer).toBeFocused();
    await expect(
      authenticatedPage.getByRole("button", {
        name: "Ask DeepSeek about the selected text",
      }),
    ).toBeHidden();
  });
});
