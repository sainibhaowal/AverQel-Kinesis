import { describe, expect, it } from "vitest";
import { drawerTransition } from "@/app/lib/drawerTransition";

describe("sidebar drawer transition (expand-only animation)", () => {
  it("animates when expanding (drawer slides in from the left)", () => {
    const transition = drawerTransition(true);
    expect(transition).toEqual({ duration: 0.2, ease: [0.22, 1, 0.36, 1] });
  });

  it("is instant when collapsing (no animation on close)", () => {
    const transition = drawerTransition(false);
    expect(transition).toEqual({ duration: 0 });
  });
});
