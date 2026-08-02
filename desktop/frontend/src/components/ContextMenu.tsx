import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";

export interface MenuItem {
  id: string;
  label: string;
  danger?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}

interface ContextMenuProps {
  x: number;
  y: number;
  items: MenuItem[];
  onClose: () => void;
}

export function ContextMenu({ x, y, items, onClose }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  // The app zooms by scaling .window-root (transform: scale(zoomLevel)), and a
  // CSS transform turns position:fixed descendants into fixed-positioning
  // relative to that scaled box in UNSCALED coordinates. clientX/clientY are
  // viewport (scaled) pixels, so they must be divided by the zoom factor to
  // land on the clicked point. The viewport-clamp below stays in viewport
  // pixels (the measured rect is already scaled) and only the final placement
  // is converted back into the local frame.
  const { zoomLevel } = useApp();
  const [pos, setPos] = useState({ left: x / zoomLevel, top: y / zoomLevel });

  // Keep the menu fully inside the viewport.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) {
      return;
    }
    const rect = el.getBoundingClientRect();
    let left = x;
    let top = y;
    if (left + rect.width > window.innerWidth) {
      left = Math.max(4, window.innerWidth - rect.width - 4);
    }
    if (top + rect.height > window.innerHeight) {
      top = Math.max(4, window.innerHeight - rect.height - 4);
    }
    setPos({ left: left / zoomLevel, top: top / zoomLevel });
  }, [x, y, zoomLevel]);

  useEffect(() => {
    const onPointer = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("mousedown", onPointer);
    window.addEventListener("keydown", onKey);
    window.addEventListener("blur", onClose);
    return () => {
      window.removeEventListener("mousedown", onPointer);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", onClose);
    };
  }, [onClose]);

  return (
    <div
      className="context-menu"
      ref={ref}
      role="menu"
      style={{ left: pos.left, top: pos.top }}
    >
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="menuitem"
          className={`context-menu-item ${item.danger ? "danger" : ""}`}
          disabled={item.disabled}
          onClick={() => {
            item.onSelect();
            onClose();
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
