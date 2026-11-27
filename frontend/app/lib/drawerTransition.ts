import type { Transition } from "framer-motion";

// Sidebar drawer transition: animates only when expanding from the left.
// Collapsing is instant (duration 0) — animation runs on expand only.
export const drawerTransition = (isOpen: boolean): Transition =>
  isOpen ? { duration: 0.2, ease: [0.22, 1, 0.36, 1] } : { duration: 0 };
