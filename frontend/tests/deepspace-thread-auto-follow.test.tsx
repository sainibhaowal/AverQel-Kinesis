import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useDeepSpaceThreadAutoFollow } from "../app/dashboard/deepspace/_hooks/useDeepSpaceThreadAutoFollow";

function createScrollableContainer(initialHeight = 1_100, initialTop = 400) {
  const container = document.createElement("div");
  let scrollHeight = initialHeight;
  let scrollTop = initialTop;
  Object.defineProperties(container, {
    clientHeight: { configurable: true, value: 500 },
    scrollHeight: {
      configurable: true,
      get: () => scrollHeight,
    },
    scrollTop: {
      configurable: true,
      get: () => scrollTop,
      set: (value: number) => {
        scrollTop = value;
      },
    },
  });
  return {
    container,
    setScrollHeight: (height: number) => {
      scrollHeight = height;
    },
  };
}

function mockAnimationFrames() {
  const callbacks = new Map<number, FrameRequestCallback>();
  let nextId = 1;
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    const id = nextId++;
    callbacks.set(id, callback);
    return id;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation((id) => {
    callbacks.delete(id);
  });

  return {
    flush(timestamp: number) {
      const next = callbacks.entries().next().value as [number, FrameRequestCallback] | undefined;
      if (!next) return false;
      callbacks.delete(next[0]);
      act(() => next[1](timestamp));
      return true;
    },
    get pendingCount() {
      return callbacks.size;
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DeepSpace thread auto-follow", () => {
  it("smoothly follows a response while retargeting streamed height changes", () => {
    const frames = mockAnimationFrames();
    const { container, setScrollHeight } = createScrollableContainer();
    const scrollContainerRef = { current: container };
    const syncScrollMetrics = vi.fn();
    const { result } = renderHook(() =>
      useDeepSpaceThreadAutoFollow(scrollContainerRef, syncScrollMetrics),
    );

    act(() => result.current.scheduleScrollToBottom());
    expect(frames.flush(16.67)).toBe(true);
    expect(container.scrollTop).toBeGreaterThan(400);
    expect(container.scrollTop).toBeLessThan(600);

    setScrollHeight(1_300);
    act(() => result.current.scheduleScrollToBottom());
    for (let frame = 2; frame < 80 && frames.pendingCount > 0; frame += 1) {
      frames.flush(frame * 16.67);
    }

    expect(container.scrollTop).toBe(800);
    expect(syncScrollMetrics).toHaveBeenCalled();
  });

  it("stops when the reader scrolls up and resumes at the latest message", () => {
    const frames = mockAnimationFrames();
    const { container, setScrollHeight } = createScrollableContainer(1_100, 200);
    const scrollContainerRef = { current: container };
    const { result } = renderHook(() => useDeepSpaceThreadAutoFollow(scrollContainerRef, vi.fn()));

    act(() => result.current.scheduleScrollToBottom());
    frames.flush(16.67);
    expect(result.current.shouldFollowRef.current).toBe(true);

    act(() => result.current.handleWheel({ deltaY: -80 }));
    expect(result.current.shouldFollowRef.current).toBe(false);
    expect(frames.pendingCount).toBe(0);

    container.scrollTop = 150;
    act(() => result.current.handleUserScroll({ currentTarget: container }));
    setScrollHeight(1_400);
    act(() => result.current.scheduleScrollToBottom());
    expect(frames.pendingCount).toBe(0);
    expect(container.scrollTop).toBe(150);

    container.scrollTop = 900;
    act(() => result.current.handleUserScroll({ currentTarget: container }));
    expect(result.current.shouldFollowRef.current).toBe(true);
    for (let frame = 2; frame < 80 && frames.pendingCount > 0; frame += 1) {
      frames.flush(frame * 16.67);
    }
    expect(container.scrollTop).toBe(900);
  });

  it("does not pull the reader down when their first scroll starts away from the bottom", () => {
    const frames = mockAnimationFrames();
    const { container } = createScrollableContainer(1_100, 200);
    const { result } = renderHook(() =>
      useDeepSpaceThreadAutoFollow({ current: container }, vi.fn()),
    );

    act(() => result.current.handleUserScroll({ currentTarget: container }));
    expect(result.current.shouldFollowRef.current).toBe(false);

    act(() => result.current.scheduleScrollToBottom());
    expect(frames.pendingCount).toBe(0);
    expect(container.scrollTop).toBe(200);
  });

  it("honors reduced motion when returning to the latest message", () => {
    const frames = mockAnimationFrames();
    vi.spyOn(window, "matchMedia").mockReturnValue({ matches: true } as MediaQueryList);
    const { container } = createScrollableContainer(1_100, 0);
    const { result } = renderHook(() =>
      useDeepSpaceThreadAutoFollow({ current: container }, vi.fn()),
    );

    act(() => result.current.scrollToLatest());
    expect(frames.flush(16.67)).toBe(true);
    expect(container.scrollTop).toBe(600);
    expect(frames.pendingCount).toBe(0);
  });
});
