import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from "react";

interface HoverTooltipProps {
  /** Tooltip body. May span multiple lines. */
  content: ReactNode;
  /** Wrapped trigger element(s). */
  children: ReactNode;
  /** Hover delay before the tooltip appears, in ms. */
  delayMs?: number;
  className?: string;
}

/**
 * Lightweight hover tooltip that renders a fixed-position box so it is never
 * clipped by a scrolling ancestor's ``overflow``. Unlike the native ``title``
 * attribute it can render rich, multi-line, styled content.
 */
export function HoverTooltip({ content, children, delayMs = 400, className }: HoverTooltipProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const tipRef = useRef<HTMLDivElement | null>(null);
  const timerRef = useRef<number | null>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState({ left: 0, top: 0 });

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const show = useCallback(() => {
    clearTimer();
    timerRef.current = window.setTimeout(() => setOpen(true), delayMs);
  }, [clearTimer, delayMs]);

  const hide = useCallback(() => {
    clearTimer();
    setOpen(false);
  }, [clearTimer]);

  // Position the tooltip relative to the trigger once it is rendered (we need
  // its measured size to keep it inside the viewport).
  useLayoutEffect(() => {
    if (!open) {
      return;
    }
    const trigger = wrapRef.current;
    const tip = tipRef.current;
    if (!trigger || !tip) {
      return;
    }
    const tRect = trigger.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    const gap = 6;
    let left = tRect.left;
    // Prefer below the trigger; flip above when it would overflow the viewport.
    let top = tRect.bottom + gap;
    if (top + tipRect.height > window.innerHeight - 4) {
      top = Math.max(4, tRect.top - tipRect.height - gap);
    }
    if (left + tipRect.width > window.innerWidth - 4) {
      left = Math.max(4, window.innerWidth - tipRect.width - 4);
    }
    left = Math.max(4, left);
    setPos({ left, top });
  }, [open, content]);

  return (
    <div
      ref={wrapRef}
      className={`hover-tooltip-wrap ${className ?? ""}`}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocusCapture={show}
      onBlurCapture={hide}
    >
      {children}
      {open && content != null && (
        <div ref={tipRef} className="hover-tooltip" role="tooltip" style={{ left: pos.left, top: pos.top }}>
          {content}
        </div>
      )}
    </div>
  );
}
