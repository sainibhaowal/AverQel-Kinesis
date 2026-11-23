import { useCallback, useEffect, useRef, type TouchEvent, type WheelEvent } from "react";

const FOLLOW_RESUME_DISTANCE_PX = 12;
const FOLLOW_SETTLE_DISTANCE_PX = 0.5;
const FOLLOW_TIME_CONSTANT_MS = 45;
const MAX_FRAME_DELTA_MS = 32;
const USER_SCROLL_DELTA_PX = 0.5;

type ScrollContainerRef = { current: HTMLDivElement | null };

export function useDeepSpaceThreadAutoFollow(
  scrollContainerRef: ScrollContainerRef,
  syncScrollMetrics: (container: HTMLDivElement) => void,
) {
  const shouldFollowRef = useRef(true);
  const animationFrameRef = useRef<number | null>(null);
  const previousFrameTimeRef = useRef<number | null>(null);
  const lastObservedScrollTopRef = useRef<number | null>(null);
  const lastTouchYRef = useRef<number | null>(null);

  const cancelAnimation = useCallback(() => {
    if (animationFrameRef.current !== null) {
      window.cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
    previousFrameTimeRef.current = null;
  }, []);

  const pauseAutoFollow = useCallback(() => {
    shouldFollowRef.current = false;
    cancelAnimation();
  }, [cancelAnimation]);

  const scheduleScrollToBottom = useCallback(() => {
    if (!shouldFollowRef.current || animationFrameRef.current !== null) return;

    const animate: FrameRequestCallback = (timestamp) => {
      animationFrameRef.current = null;
      const container = scrollContainerRef.current;
      if (!container || !shouldFollowRef.current) {
        previousFrameTimeRef.current = null;
        return;
      }

      const target = Math.max(0, container.scrollHeight - container.clientHeight);
      const distance = target - container.scrollTop;
      const reduceMotion =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      if (reduceMotion || Math.abs(distance) <= FOLLOW_SETTLE_DISTANCE_PX) {
        if (Math.abs(distance) > 0) {
          lastObservedScrollTopRef.current = target;
          container.scrollTop = target;
        }
        previousFrameTimeRef.current = null;
        syncScrollMetrics(container);
        return;
      }

      const previousTime = previousFrameTimeRef.current;
      const elapsed =
        previousTime === null
          ? 1000 / 60
          : Math.min(Math.max(timestamp - previousTime, 0), MAX_FRAME_DELTA_MS);
      previousFrameTimeRef.current = timestamp;

      // Time-based easing feels consistent at 60 Hz and high-refresh displays,
      // while continuously retargeting the latest streamed content height.
      const progress = 1 - Math.exp(-elapsed / FOLLOW_TIME_CONSTANT_MS);
      container.scrollTop += distance * progress;
      lastObservedScrollTopRef.current = container.scrollTop;
      syncScrollMetrics(container);

      const remaining = target - container.scrollTop;
      if (Math.abs(remaining) > FOLLOW_SETTLE_DISTANCE_PX) {
        animationFrameRef.current = window.requestAnimationFrame(animate);
      } else {
        container.scrollTop = target;
        lastObservedScrollTopRef.current = target;
        previousFrameTimeRef.current = null;
        syncScrollMetrics(container);
      }
    };

    animationFrameRef.current = window.requestAnimationFrame(animate);
  }, [scrollContainerRef, syncScrollMetrics]);

  const handleUserScroll = useCallback(
    (event: { currentTarget: HTMLDivElement }) => {
      const container = event.currentTarget;
      const previousTop = lastObservedScrollTopRef.current;
      const movedUp =
        previousTop !== null && container.scrollTop < previousTop - USER_SCROLL_DELTA_PX;
      const distanceToBottom =
        container.scrollHeight - container.scrollTop - container.clientHeight;
      const firstObservedScrollAwayFromBottom =
        previousTop === null && distanceToBottom > FOLLOW_RESUME_DISTANCE_PX;
      lastObservedScrollTopRef.current = container.scrollTop;

      if (movedUp || firstObservedScrollAwayFromBottom) {
        pauseAutoFollow();
      } else {
        if (!shouldFollowRef.current && distanceToBottom <= FOLLOW_RESUME_DISTANCE_PX) {
          shouldFollowRef.current = true;
          scheduleScrollToBottom();
        }
      }

      syncScrollMetrics(container);
    },
    [pauseAutoFollow, scheduleScrollToBottom, syncScrollMetrics],
  );

  const handleWheel = useCallback(
    (event: Pick<WheelEvent<HTMLDivElement>, "deltaY">) => {
      if (event.deltaY < 0) pauseAutoFollow();
    },
    [pauseAutoFollow],
  );

  const handleTouchStart = useCallback((event: TouchEvent<HTMLDivElement>) => {
    lastTouchYRef.current = event.touches[0]?.clientY ?? null;
  }, []);

  const handleTouchMove = useCallback(
    (event: TouchEvent<HTMLDivElement>) => {
      const currentY = event.touches[0]?.clientY;
      const previousY = lastTouchYRef.current;
      if (currentY !== undefined && previousY !== null && currentY > previousY + 2) {
        pauseAutoFollow();
      }
      lastTouchYRef.current = currentY ?? null;
    },
    [pauseAutoFollow],
  );

  const handleTouchEnd = useCallback(() => {
    lastTouchYRef.current = null;
  }, []);

  const handleScrollCapture = useCallback(
    (event: { target: EventTarget | null }) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest?.("[data-thinking-scroll='true']")) pauseAutoFollow();
    },
    [pauseAutoFollow],
  );

  const resetAutoFollow = useCallback(() => {
    shouldFollowRef.current = true;
    lastObservedScrollTopRef.current = scrollContainerRef.current?.scrollTop ?? null;
    scheduleScrollToBottom();
  }, [scheduleScrollToBottom, scrollContainerRef]);

  const scrollToLatest = useCallback(() => {
    resetAutoFollow();
  }, [resetAutoFollow]);

  useEffect(() => cancelAnimation, [cancelAnimation]);

  return {
    shouldFollowRef,
    scheduleScrollToBottom,
    resetAutoFollow,
    scrollToLatest,
    handleUserScroll,
    handleWheel,
    handleTouchStart,
    handleTouchMove,
    handleTouchEnd,
    handleScrollCapture,
  };
}
