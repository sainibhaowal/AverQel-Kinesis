import { render, screen } from "@testing-library/react";

import { AverQelLoader } from "../app/components/ui/AverQelLoading";

describe("AverQelLoader", () => {
  it("uses valid phrasing markup while retaining its accessible status label", () => {
    render(<AverQelLoader label="Loading data" />);

    const loader = screen.getByRole("status", { name: "Loading data" });
    expect(loader.tagName).toBe("SPAN");
    expect(loader.querySelector("div")).toBeNull();
    expect(loader.querySelectorAll(".averqel-loading-indicator__box")).toHaveLength(6);
  });
});
