import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import AverQelWordmark from "../app/components/ui/AverQelWordmark";

it("renders AverQel with Kinesis on a second line", () => {
  render(<AverQelWordmark />);

  const wordmark = screen.getByLabelText("AverQel Kinesis");
  expect(wordmark).toHaveClass("averqel-wordmark", "averqel-wordmark--nav");
  expect(wordmark).toHaveAttribute("role", "img");
  expect(wordmark.querySelector('image[href="/brand/AverQel_Text.svg"]')).toBeInTheDocument();
  expect(wordmark.querySelector(".forge-wordmark__text")).toHaveTextContent("Kinesis");
});

it("supports the compact mobile size", () => {
  render(<AverQelWordmark size="mobile" />);

  expect(screen.getByLabelText("AverQel Kinesis")).toHaveClass("averqel-wordmark--mobile");
});
