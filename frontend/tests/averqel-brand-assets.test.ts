import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, it } from "vitest";

const publicRoot = resolve(process.cwd(), "public");

it("keeps the AntiAtom mark as the shared icon asset", () => {
  const icon = readFileSync(resolve(publicRoot, "brand/AntiAtom_Icon.svg"), "utf8");
  const logoComponent = readFileSync(
    resolve(process.cwd(), "app/components/ui/AverQelLogo.tsx"),
    "utf8",
  );
  const layout = readFileSync(resolve(process.cwd(), "app/layout.tsx"), "utf8");

  expect(icon).toContain('<svg xmlns="http://www.w3.org/2000/svg"');
  expect(icon).toContain('viewBox="0 0 500 500"');
  expect(logoComponent).toContain('src="/brand/AntiAtom_Icon.svg"');
  expect(layout).toContain('icon: "/brand/AntiAtom_Icon.svg"');
});

it("keeps AverQel above the Kinesis word throughout shared brand components", () => {
  const wordmark = readFileSync(
    resolve(process.cwd(), "app/components/ui/AverQelWordmark.tsx"),
    "utf8",
  );
  const logoComponent = readFileSync(
    resolve(process.cwd(), "app/components/ui/AverQelLogo.tsx"),
    "utf8",
  );
  const titleBar = readFileSync(resolve(process.cwd(), "components/layout/TitleBar.tsx"), "utf8");
  const docsShell = readFileSync(
    resolve(process.cwd(), "app/documentation/_components/DocsShell.tsx"),
    "utf8",
  );
  const deepSpaceMark = readFileSync(
    resolve(process.cwd(), "app/components/ui/DeepSpaceMark.tsx"),
    "utf8",
  );
  const dashboardLayout = readFileSync(resolve(process.cwd(), "app/dashboard/layout.tsx"), "utf8");
  const exactBrand = readFileSync(
    resolve(process.cwd(), "app/components/ui/AverQelExactBrand.tsx"),
    "utf8",
  );
  const qelConnectMark = readFileSync(
    resolve(process.cwd(), "app/components/ui/QelConnectMark.tsx"),
    "utf8",
  );
  const globalStyles = readFileSync(resolve(process.cwd(), "app/globals.css"), "utf8");

  expect(wordmark).toContain('aria-label={PRODUCT_NAME}');
  expect(wordmark).toContain('href={TEXT_LOGO_SOURCE}');
  expect(wordmark).toContain('className="forge-wordmark__text"');
  expect(logoComponent).toContain('import AverQelWordmark from "./AverQelWordmark"');
  expect(logoComponent).toContain("<AverQelWordmark size={wordmarkSize} />");
  expect(logoComponent).toContain("data-brand-lockup");
  expect(logoComponent).toContain("markWidth: 104");
  expect(logoComponent).toContain('gap: "0rem"');
  expect(titleBar).toContain('import AverQelWordmark from "@/app/components/ui/AverQelWordmark"');
  expect(titleBar).toContain('<AverQelWordmark size="footer" />');
  expect(docsShell).toContain('import AverQelLogo from "../../components/ui/AverQelLogo"');
  expect(docsShell).not.toContain("AverQel OS");
  expect(deepSpaceMark).toContain(
    'source = "/DeepSpace Logo/DeepSpace_Logo_Only_NoBlack_Exact.svg"',
  );
  expect(dashboardLayout).toContain(
    'source="/DeepSpace Logo/DeepSpace_Logo_Only_NoBlack_Exact.svg"',
  );
  expect(exactBrand).toContain('src="/brand/AntiAtom_Icon.svg"');
  expect(exactBrand).toContain('src="/brand/AverQel_Text.svg"');
  expect(exactBrand).toContain("PRODUCT_NAME");
  expect(globalStyles).toContain(".forge-wordmark__text");
  expect(qelConnectMark).toContain("qel-connect-mark");
  expect(globalStyles).toContain(".qel-connect-mark");
  expect(globalStyles).toContain("filter: brightness(0.62) saturate(1.2) contrast(1.18);");
  expect(globalStyles).toContain(".dark .qel-connect-mark");
  expect(globalStyles).toContain(".dark .qel-connect-mark {\n  filter: none;");
});

it("animates only the non-Q AverQel letters", () => {
  const wordmarkAsset = readFileSync(resolve(publicRoot, "brand/AverQel_Text.svg"), "utf8");

  expect(wordmarkAsset).toContain("@keyframes averqel-rgb-letter-wave");
  expect(wordmarkAsset).toContain(".averqel-letter--a { animation-delay: -2.5s; }");
  expect(wordmarkAsset).toContain(".averqel-letter--l { animation-delay: 0s; }");
  expect(wordmarkAsset).not.toContain("averqel-rgb-flow");
  expect(wordmarkAsset).toContain('fill="url(#q)"');
  expect(wordmarkAsset).not.toContain("averqel-letter--q");
});

it("does not leave the retired root logo aliases as active brand files", () => {
  expect(existsSync(resolve(publicRoot, "logo_icon.svg"))).toBe(false);
  expect(existsSync(resolve(publicRoot, "logo_icon.png"))).toBe(false);
  expect(existsSync(resolve(publicRoot, "brand/logo_icon.svg"))).toBe(false);
  expect(existsSync(resolve(publicRoot, "brand/logo_icon.png"))).toBe(false);
  expect(existsSync(resolve(publicRoot, "brand/logo_icon_512.png"))).toBe(false);
  for (const retiredAsset of ["Forge_Icon.svg", "AverQel_Icon.svg", "AverQel_Exact_Logo.svg"]) {
    expect(existsSync(resolve(publicRoot, "brand", retiredAsset))).toBe(false);
  }
});

it("keeps the Qel Connect mark on the supplied telemetry parabola asset", () => {
  const mark = readFileSync(
    resolve(publicRoot, "AverQel Logo/deepspace-telemetry-parabola.svg"),
    "utf8",
  );
  const markComponent = readFileSync(
    resolve(process.cwd(), "app/components/ui/QelConnectMark.tsx"),
    "utf8",
  );
  const brand = readFileSync(resolve(process.cwd(), "lib/brand.ts"), "utf8");

  expect(mark).toContain("<svg");
  expect(brand).toContain(
    'QEL_CONNECT_MARK_SOURCE = "/AverQel Logo/deepspace-telemetry-parabola.svg"',
  );
  expect(markComponent).toContain('aria-hidden="true"');
});
