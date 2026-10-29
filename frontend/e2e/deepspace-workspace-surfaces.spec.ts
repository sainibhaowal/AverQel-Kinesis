import { expect, test } from "./fixtures";

/**
 * The three surfaces a conversation starts with: its workspace files, a
 * terminal, and the in-app browser.
 *
 * The click-through is the proof: an empty conversation offers the three
 * cards, each card opens the side panel on its surface, and each surface
 * then does its own work.
 */
test.describe("DeepSpace workspace surfaces", () => {
  // A fresh isolated Next server needs extra time to compile this route.
  test.describe.configure({ timeout: 60_000 });

  test("offers the files, a terminal, and the browser on an empty conversation", async ({
    authenticatedPage,
  }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    await expect(authenticatedPage.getByRole("button", { name: /Workspace files/ })).toBeVisible();
    await expect(authenticatedPage.getByRole("button", { name: /New terminal/ })).toBeVisible();
    await expect(authenticatedPage.getByRole("button", { name: /Browser/ })).toBeVisible();
  });

  test("opens the workspace files and shows what is in the workspace", async ({
    authenticatedPage,
  }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    await authenticatedPage.getByRole("button", { name: /Workspace files/ }).click();

    const panel = authenticatedPage.getByRole("complementary", { name: "DeepSpace side panel" });
    await expect(panel).toBeVisible();
    await expect(panel.getByRole("tab", { name: "Files" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(panel.getByRole("button", { name: "README.md" })).toBeVisible();
    await expect(panel.getByRole("button", { name: "src" })).toBeVisible();

    await panel.getByRole("button", { name: "README.md" }).click();
    await expect(panel.getByText("# DeepSpace workspace")).toBeVisible();
  });

  test("starts a terminal and runs what the user types", async ({ authenticatedPage }) => {
    const received: string[] = [];
    await authenticatedPage.routeWebSocket(
      /\/api\/v1\/deepspace\/chats\/[^/]+\/terminals\/[^/]+\/stream/,
      (socket) => {
        socket.onMessage((message) => {
          if (typeof message !== "string") return;
          try {
            const packet = JSON.parse(message) as { type?: string; data?: string };
            if (packet.type === "input" && packet.data) {
              received.push(packet.data);
              socket.send(
                JSON.stringify({
                  type: "output",
                  data: "npm run dev\r\nready on http://localhost:3000\r\n",
                }),
              );
            }
          } catch {
            // The terminal sends only JSON packets in this test.
          }
        });
        socket.send(JSON.stringify({ type: "ready", shell: "bash", pty: true }));
      },
    );

    await authenticatedPage.goto("/dashboard/deepspace");

    await authenticatedPage.getByRole("button", { name: /New terminal/ }).click();

    const panel = authenticatedPage.getByRole("complementary", { name: "DeepSpace side panel" });
    await expect(panel.getByRole("tab", { name: "Terminal" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(panel.getByText(/Connected.*bash.*PTY/)).toBeVisible();

    const terminal = panel.getByRole("textbox", { name: "Interactive terminal" });
    await terminal.click();
    await authenticatedPage.keyboard.type("npm run dev");
    await expect.poll(() => received.join("")).toContain("npm run dev");
  });

  test("browses a page and moves back, forward, and reloads", async ({ authenticatedPage }) => {
    await authenticatedPage.goto("/dashboard/deepspace");

    await authenticatedPage.getByRole("button", { name: /Browser/ }).click();

    const panel = authenticatedPage.getByRole("complementary", { name: "DeepSpace side panel" });
    await expect(panel.getByRole("tab", { name: "Browser" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(panel.getByText("Enter an HTTP(S) address to start browsing")).toBeVisible();

    const url = panel.getByRole("textbox", { name: "Browser URL" });
    await url.fill("http://localhost:3000");
    await panel.getByRole("button", { name: "Go", exact: true }).click();

    const frame = panel.locator("iframe");
    await expect(frame).toHaveAttribute("src", "http://localhost:3000/");
    await expect(frame).toHaveAttribute("aria-label", "Browser preview");

    await url.fill("http://localhost:4000");
    await panel.getByRole("button", { name: "Go", exact: true }).click();
    await expect(frame).toHaveAttribute("src", "http://localhost:4000/");

    await panel.getByRole("button", { name: "Go back" }).click();
    await expect(frame).toHaveAttribute("src", "http://localhost:3000/");

    await panel.getByRole("button", { name: "Go forward" }).click();
    await expect(frame).toHaveAttribute("src", "http://localhost:4000/");

    await panel.getByRole("button", { name: "Reload the page" }).click();
    await expect(frame).toHaveAttribute("src", "http://localhost:4000/");
  });
});
