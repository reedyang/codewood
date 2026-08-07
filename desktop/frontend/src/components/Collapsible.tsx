import {
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type TransitionEvent as ReactTransitionEvent,
} from "react";

/** Duration (ms) of the open/close height animation. Keep in sync with the
 *  ``.collapsible`` transition in styles.css. */
export const COLLAPSE_ANIM_MS = 240;

/** An animated expand/collapse region.
 *
 *  Content is only mounted while open (or while the close animation is still
 *  running), so collapsed transcripts keep heavy tool-output / diff nodes out
 *  of the DOM. The height animation uses the ``grid-template-rows: 0fr ->
 *  1fr`` technique: the wrapper is a grid whose single row animates between
 *  collapsed and content height, while the inner wrapper clips overflow.
 *
 *  - ``open`` true  -> content mounts, then grows open (animated).
 *  - ``open`` false -> content shrinks closed (animated), then unmounts.
 *  - Mounting with ``open`` already true renders expanded immediately (no
 *    grow animation), which is what the task-settle transition wants: show the
 *    expanded "Worked for" block first, then animate the collapse.
 */
export function Collapsible({
  open,
  children,
  className = "",
  onInnerMount,
}: {
  open: boolean;
  children: ReactNode;
  className?: string;
  /** Called once right after the inner content node has been inserted into
   *  the DOM (e.g. to scroll newly expanded content into view). */
  onInnerMount?: () => void;
}) {
  const [mounted, setMounted] = useState(open);
  const [shown, setShown] = useState(open);
  const prevMountedRef = useRef(mounted);

  // Keep the content mounted while open; unmounting happens after the close
  // animation finishes (see transitionend handler + fallback timer below).
  useEffect(() => {
    if (open) {
      setMounted(true);
    }
  }, [open]);

  // Two-phase open: render collapsed first, then add the ``.open`` class on
  // the next frame so the browser animates 0fr -> 1fr instead of jumping.
  useEffect(() => {
    if (!mounted) {
      return;
    }
    if (open) {
      const raf = requestAnimationFrame(() => setShown(true));
      return () => cancelAnimationFrame(raf);
    }
    setShown(false);
  }, [mounted, open]);

  // Safety net: unmount shortly after the close animation would have
  // finished, for environments that never fire transitionend (jsdom tests,
  // reduced-motion users). The transitionend handler normally unmounts.
  useEffect(() => {
    if (!mounted || open) {
      return;
    }
    const timer = setTimeout(() => setMounted(false), COLLAPSE_ANIM_MS + 120);
    return () => clearTimeout(timer);
  }, [mounted, open]);

  // Notify the parent once the inner content has actually been inserted.
  useEffect(() => {
    if (mounted && !prevMountedRef.current && onInnerMount) {
      onInnerMount();
    }
    prevMountedRef.current = mounted;
  }, [mounted, onInnerMount]);

  if (!mounted) {
    return null;
  }

  const handleTransitionEnd = (e: ReactTransitionEvent<HTMLDivElement>) => {
    // Only the wrapper's own grid-template-rows transition matters; child
    // transitions (chevrons, etc.) bubble up and must not unmount early.
    if (e.target !== e.currentTarget) {
      return;
    }
    if (!open) {
      setMounted(false);
    }
  };

  return (
    <div
      className={`collapsible${shown ? " open" : ""}${className ? ` ${className}` : ""}`}
      onTransitionEnd={handleTransitionEnd}
    >
      <div className="collapsible-inner">{children}</div>
    </div>
  );
}
