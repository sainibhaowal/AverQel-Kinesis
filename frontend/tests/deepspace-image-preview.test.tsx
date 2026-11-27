import { fireEvent, render, screen } from "@testing-library/react";

import { InteractiveImagePreview } from "@/app/dashboard/deepspace/_components/DeepSpaceLibraryPreview";

describe("DeepSpace interactive image preview", () => {
  it("zooms with the wheel and exposes reset/transform controls", () => {
    render(<InteractiveImagePreview source="data:image/png;base64,AA==" alt="Test image" />);

    const viewport = screen.getByRole("application");
    const image = screen.getByAltText("Test image");
    fireEvent.wheel(viewport, { deltaY: -100, clientX: 120, clientY: 80 });

    expect(image.style.transform).toContain("scale(1.1)");
    fireEvent.click(screen.getByRole("button", { name: "Rotate right" }));
    expect(image.style.transform).toContain("rotate(90deg)");

    fireEvent.click(screen.getByRole("button", { name: "Fit image to preview" }));
    expect(image.style.transform).toContain("scale(1)");
  });

  it("supports keyboard panning for accessible image navigation", () => {
    render(<InteractiveImagePreview source="data:image/png;base64,AA==" />);
    const viewport = screen.getByRole("application");
    const image = screen.getByAltText("Image preview");
    fireEvent.keyDown(viewport, { key: "ArrowLeft" });

    expect(image.style.transform).toContain("translate3d(32px, 0px, 0)");
  });

  it("prevents default and stops propagation on wheel so the outer page never scrolls", () => {
    let parentWheelCalled = false;
    render(
      <div
        onWheel={() => {
          parentWheelCalled = true;
        }}
      >
        <InteractiveImagePreview source="data:image/png;base64,AA==" alt="Shot" />
      </div>,
    );

    const viewport = screen.getByRole("application");
    const wheelEvent = new WheelEvent("wheel", {
      deltaY: -120,
      clientX: 100,
      clientY: 100,
      bubbles: true,
      cancelable: true,
    });
    const dispatched = viewport.dispatchEvent(wheelEvent);

    expect(dispatched).toBe(false);
    expect(wheelEvent.defaultPrevented).toBe(true);
    expect(parentWheelCalled).toBe(false);
  });
});
