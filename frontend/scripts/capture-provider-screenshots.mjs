import { chromium } from "@playwright/test";
import path from "node:path";
import fs from "node:fs";

const artifactDir = "/home/ravi/.gemini/antigravity-ide/brain/6ddb0029-fced-4515-bce6-80dd8b3ee108";

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1400, height: 1200 },
    deviceScaleFactor: 1.5,
  });
  const page = await context.newPage();

  console.log("Navigating to providers settings page...");
  await page.goto("http://localhost:10030/dashboard/settings/providers", {
    waitUntil: "networkidle",
    timeout: 30000,
  });

  // Wait for provider list to load
  await page.waitForTimeout(2000);

  // 1. Light Mode - Codex
  await page.evaluate(() => {
    document.documentElement.classList.remove("dark");
    localStorage.setItem("theme", "light");
  });
  await page.waitForTimeout(500);

  // Click on Codex Login tab if present
  const codexTab = page.locator('button[role="tab"]:has-text("Codex Login")');
  if (await codexTab.isVisible()) {
    await codexTab.click();
    await page.waitForTimeout(500);
  }

  const codexLightPath = path.join(artifactDir, "openai_codex_vibrant_light.png");
  await page.screenshot({ path: codexLightPath, fullPage: false });
  console.log("Saved:", codexLightPath);

  // Scroll down to capture sticky footer and cards
  await page.evaluate(() => window.scrollTo(0, 500));
  await page.waitForTimeout(400);
  const codexLightFooterPath = path.join(artifactDir, "openai_codex_vibrant_light_footer.png");
  await page.screenshot({ path: codexLightFooterPath, fullPage: false });
  console.log("Saved:", codexLightFooterPath);

  // 2. Dark Mode - Codex
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.documentElement.classList.add("dark");
    localStorage.setItem("theme", "dark");
  });
  await page.waitForTimeout(500);

  const codexDarkPath = path.join(artifactDir, "openai_codex_vibrant_dark.png");
  await page.screenshot({ path: codexDarkPath, fullPage: false });
  console.log("Saved:", codexDarkPath);

  await page.evaluate(() => window.scrollTo(0, 500));
  await page.waitForTimeout(400);
  const codexDarkFooterPath = path.join(artifactDir, "openai_codex_vibrant_dark_footer.png");
  await page.screenshot({ path: codexDarkFooterPath, fullPage: false });
  console.log("Saved:", codexDarkFooterPath);

  // Switch to Reranker tab
  await page.evaluate(() => window.scrollTo(0, 0));
  const rerankerTab = page.locator('button:has-text("Reranker")').first();
  if (await rerankerTab.isVisible()) {
    await rerankerTab.click();
    await page.waitForTimeout(800);
  }

  // Click on AverQel Server ReRanker or explore hub
  const rerankerItem = page.locator('button:has-text("AverQel"), div:has-text("AverQel Server ReRanker")').first();
  if (await rerankerItem.isVisible()) {
    await rerankerItem.click();
    await page.waitForTimeout(800);
  }

  // 3. Dark Mode - Reranker
  const rerankerDarkPath = path.join(artifactDir, "reranker_hub_vibrant_dark.png");
  await page.screenshot({ path: rerankerDarkPath, fullPage: false });
  console.log("Saved:", rerankerDarkPath);

  await page.evaluate(() => window.scrollTo(0, 600));
  await page.waitForTimeout(400);
  const rerankerDarkScrolledPath = path.join(artifactDir, "reranker_hub_vibrant_dark_scrolled.png");
  await page.screenshot({ path: rerankerDarkScrolledPath, fullPage: false });
  console.log("Saved:", rerankerDarkScrolledPath);

  // 4. Light Mode - Reranker
  await page.evaluate(() => {
    window.scrollTo(0, 0);
    document.documentElement.classList.remove("dark");
    localStorage.setItem("theme", "light");
  });
  await page.waitForTimeout(500);

  const rerankerLightPath = path.join(artifactDir, "reranker_hub_vibrant_light.png");
  await page.screenshot({ path: rerankerLightPath, fullPage: false });
  console.log("Saved:", rerankerLightPath);

  await page.evaluate(() => window.scrollTo(0, 600));
  await page.waitForTimeout(400);
  const rerankerLightScrolledPath = path.join(artifactDir, "reranker_hub_vibrant_light_scrolled.png");
  await page.screenshot({ path: rerankerLightScrolledPath, fullPage: false });
  console.log("Saved:", rerankerLightScrolledPath);

  await browser.close();
  console.log("Done!");
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
