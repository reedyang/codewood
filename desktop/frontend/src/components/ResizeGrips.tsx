import { useEffect, useRef, useState } from "react";

type Dir = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

const DIRS: Dir[] = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

const MIN_WIDTH = 960;
const MIN_HEIGHT = 640;

interface HostGeometryApi {
  set_window_geometry?: (x: number, y: number, width: number, height: number) => void;
  start_window_resize?: (direction: Dir) => boolean | Promise<boolean>;
}

function hostApi(): HostGeometryApi | undefined {
  return (window as unknown as { pywebview?: { api?: HostGeometryApi } }).pywebview?.api;
}

/**
 * Edge/corner resize handles for the frameless WebView2 window. WebView2
 * swallows the native resize hit-test, so we drive resizing from JS using the
 * window's on-screen position (CSS pixels) and call back into the host.
 */
export function ResizeGrips() {
  const [native, setNative] = useState<boolean>(() => Boolean(hostApi()));
  const frameRef = useRef<number | null>(null);

  useEffect(() => {
    const onReady = () => setNative(true);
    window.addEventListener("pywebviewready", onReady);
    return () => window.removeEventListener("pywebviewready", onReady);
  }, []);

  if (!native) {
    return null;
  }

  const startResize = (dir: Dir) => (e: React.MouseEvent) => {
    const api = hostApi();
    if (e.button !== 0) {
      return;
    }
    // GTK/WSL: delegate resizing to the window manager so it tracks the
    // cursor correctly across mixed-DPI monitors. The host returns false on
    // Windows (and when GTK is unavailable), where we fall back to the
    // JS-computed geometry path below.
    // Capture the grab point eagerly so the async GTK probe below can still
    // fall back to a geometry resize anchored at the original press position.
    const grab = { mx: e.screenX, my: e.screenY };
    if (api?.start_window_resize) {
      e.preventDefault();
      void Promise.resolve(api.start_window_resize(dir)).then((handled) => {
        if (handled) {
          return;
        }
        beginGeometryResize(dir, grab.mx, grab.my);
      });
      return;
    }
    if (!api?.set_window_geometry) {
      return;
    }
    e.preventDefault();
    beginGeometryResize(dir, grab.mx, grab.my);
  };

  const beginGeometryResize = (dir: Dir, grabX: number, grabY: number) => {
    const api = hostApi();
    if (!api?.set_window_geometry) {
      return;
    }
    const start = {
      mx: grabX,
      my: grabY,
      x: window.screenX,
      y: window.screenY,
      w: window.innerWidth,
      h: window.innerHeight,
    };

    const apply = (mx: number, my: number) => {
      const dx = mx - start.mx;
      const dy = my - start.my;
      let { x, y, w, h } = start;
      if (dir.includes("e")) {
        w = start.w + dx;
      }
      if (dir.includes("s")) {
        h = start.h + dy;
      }
      if (dir.includes("w")) {
        w = start.w - dx;
        x = start.x + dx;
      }
      if (dir.includes("n")) {
        h = start.h - dy;
        y = start.y + dy;
      }
      // Clamp to the minimum size, keeping the anchored edge in place.
      if (w < MIN_WIDTH) {
        if (dir.includes("w")) {
          x -= MIN_WIDTH - w;
        }
        w = MIN_WIDTH;
      }
      if (h < MIN_HEIGHT) {
        if (dir.includes("n")) {
          y -= MIN_HEIGHT - h;
        }
        h = MIN_HEIGHT;
      }
      api.set_window_geometry?.(x, y, w, h);
    };

    const onMove = (ev: MouseEvent) => {
      const mx = ev.screenX;
      const my = ev.screenY;
      if (frameRef.current != null) {
        return;
      }
      frameRef.current = window.requestAnimationFrame(() => {
        frameRef.current = null;
        apply(mx, my);
      });
    };

    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      if (frameRef.current != null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  return (
    <div className="resize-grips" aria-hidden="true">
      {DIRS.map((dir) => (
        <div key={dir} className={`resize-grip resize-${dir}`} onMouseDown={startResize(dir)} />
      ))}
    </div>
  );
}
