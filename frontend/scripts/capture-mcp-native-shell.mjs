import { chromium } from "@playwright/test";
import path from "node:path";

const artifactDir = "/home/ravi/.gemini/antigravity-ide/brain/6ddb0029-fced-4515-bce6-80dd8b3ee108";

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1100 },
    deviceScaleFactor: 1.5,
  });
  const page = await context.newPage();

  page.on("pageerror", (err) => console.error("PAGE ERROR:", err));
  page.on("console", (msg) => {
    if (msg.type() === "error") console.error("CONSOLE ERROR:", msg.text());
  });

  console.log("Navigating to providers settings page...");
  await page.goto("http://localhost:10030/dashboard/settings/providers", {
    waitUntil: "networkidle",
    timeout: 30000,
  });

  await page.waitForTimeout(2000);

  // Switch to MCP Server tab
  const mcpTab = page.locator('button:has-text("MCP Server")').first();
  if (await mcpTab.isVisible()) {
    await mcpTab.click();
    await page.waitForTimeout(1000);
  }

  // 1. Dark mode - Empty state (System Navigator) with left inventory
  await page.evaluate(() => {
    document.documentElement.classList.add("dark");
    localStorage.setItem("theme", "dark");
  });
  await page.waitForTimeout(500);

  const mcpEmptyDarkPath = path.join(artifactDir, "mcp_native_shell_empty_dark.png");
  await page.screenshot({ path: mcpEmptyDarkPath, fullPage: false });
  console.log("Saved:", mcpEmptyDarkPath);

  // 2. Light mode - Empty state (System Navigator) with left inventory
  await page.evaluate(() => {
    document.documentElement.classList.remove("dark");
    localStorage.setItem("theme", "light");
  });
  await page.waitForTimeout(500);

  const mcpEmptyLightPath = path.join(artifactDir, "mcp_native_shell_empty_light.png");
  await page.screenshot({ path: mcpEmptyLightPath, fullPage: false });
  console.log("Saved:", mcpEmptyLightPath);

  // 3. Select Composio (or first item) to view setup panel in Dark Mode
  await page.evaluate(() => {
    document.documentElement.classList.add("dark");
    localStorage.setItem("theme", "dark");
  });
  await page.waitForTimeout(400);

  const composioItem = page.locator('button:has-text("Composio")').first();
  if (await composioItem.isVisible()) {
    await composioItem.click();
    await page.waitForTimeout(800);
    const mcpComposioDarkPath = path.join(artifactDir, "mcp_native_shell_composio_dark.png");
    await page.screenshot({ path: mcpComposioDarkPath, fullPage: false });
    console.log("Saved:", mcpComposioDarkPath);
  }



  // 4. Test Catalog [+] toggle
  const plusBtn = page.locator('button[aria-label*="provider families"], button[aria-label*="inventory"]').first();
  if (await plusBtn.isVisible()) {
    await plusBtn.click();
    await page.waitForTimeout(600);
    const mcpCatalogDarkPath = path.join(artifactDir, "mcp_native_shell_catalog_dark.png");
    await page.screenshot({ path: mcpCatalogDarkPath, fullPage: false });
    console.log("Saved:", mcpCatalogDarkPath);
  }

  await browser.close();
  console.log("All screenshots captured successfully!");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
