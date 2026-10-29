import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(scriptDirectory, "..");
const workspaceRoot = path.resolve(desktopRoot, "..", "..");
const sourceRoot = path.join(workspaceRoot, "frontend", "public", "brand");
const targetRoot = path.join(desktopRoot, "assets", "brand");
const brandAssets = ["AverQel_Text.svg", "AntiAtom_Icon.svg"];

mkdirSync(targetRoot, { recursive: true });

for (const fileName of brandAssets) {
  const source = path.join(sourceRoot, fileName);
  const target = path.join(targetRoot, fileName);
  if (!existsSync(source)) {
    throw new Error(`The canonical AverQel AntiAtom brand asset is missing: ${source}`);
  }
  copyFileSync(source, target);
}

console.log(
  `Staged ${brandAssets.length} canonical AverQel AntiAtom brand assets for Electron.`,
);
