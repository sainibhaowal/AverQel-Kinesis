import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render } from "@testing-library/react";
import { expect, it } from "vitest";

import AverQelLogo from "../app/components/ui/AverQelLogo";

const globalStyles = readFileSync(resolve(process.cwd(), "app/globals.css"), "utf8");

it("keeps the nav icon and wordmark as a close, vertically aligned lockup", () => {
  render(<AverQelLogo size="nav" />);

  const lockup = document.querySelector<HTMLElement>("[data-brand-lockup]");
  expect(lockup).not.toBeNull();
  if (!lockup) return;
  expect(lockup).toHaveClass("averqel-logo", "averqel-logo--nav");
  expect(lockup).toHaveStyle({ columnGap: "0rem" });
  expect(lockup.querySelector(".averqel-logo__wordmark")).toBeInTheDocument();
  expect(lockup.querySelector(".averqel-logo__wordmark > div")).toHaveClass(
    "flex",
    "items-center",
    "leading-none",
  );
  expect(lockup.querySelector('img[src="/brand/AntiAtom_Icon.svg"]')).toBeInTheDocument();
  expect(lockup.querySelector('img[src="/brand/AntiAtom_Icon.svg"]')).toHaveAttribute("width", "104");
  expect(lockup.querySelector('img[src="/brand/AntiAtom_Icon.svg"]')).toHaveAttribute("height", "52");
  expect(lockup.querySelector(".forge-wordmark__text")).toHaveTextContent("Kinesis");
});

it("uses the same tight canvas overlap for the compact footer lockup", () => {
  render(<AverQelLogo size="footer" />);

  const lockup = document.querySelector<HTMLElement>("[data-brand-lockup]");
  expect(lockup).not.toBeNull();
  if (!lockup) return;
  expect(lockup).toHaveClass("averqel-logo", "averqel-logo--footer");
  expect(lockup).toHaveStyle({ columnGap: "0rem" });
  expect(lockup.querySelector(".averqel-logo__wordmark")).toBeInTheDocument();
  expect(lockup.querySelector(".averqel-logo__wordmark > div")).toHaveClass(
    "flex",
    "items-center",
    "leading-none",
  );
});

it("centers the mark in a square slot when the sidebar is collapsed", () => {
  render(<AverQelLogo size="footer" showWordmark wordmarkVisible={false} />);

  const lockup = document.querySelector<HTMLElement>("[data-brand-lockup]");
  expect(lockup).not.toBeNull();
  if (!lockup) return;
  expect(lockup).toHaveClass("averqel-logo--mark-only");
  expect(lockup.querySelector(".averqel-logo__mark-frame")).toHaveClass(
    "flex",
    "items-center",
    "justify-center",
  );
  expect(globalStyles).toContain(
    ".averqel-logo--footer.averqel-logo--mark-only .averqel-logo__wordmark",
  );
});

it("uses the Kinesis text treatment for the wordmark", () => {
  expect(globalStyles).toContain(".forge-wordmark__text");
  expect(globalStyles).toContain("font-family: var(--font-display)");
});
