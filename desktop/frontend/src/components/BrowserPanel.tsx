import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";
import { hostApi } from "../utils/hostApi";

/** A backend-issued browser command delivered over SSE. */
interface BrowserCommand {
  action?: unknown;
  url?: unknown;
  requestId?: unknown;
  script?: unknown;
}

function normalizeUrl(raw: string): string {
  const s = String(raw || "").trim();
  if (!s) return "";
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(s) || s.startsWith("about:")) {
    return s;
  }
  return `https://${s}`;
}

/** The global embedded browser. Uses a tracked host overlay window positioned
 *  over the placeholder below. This loads external sites (no X-Frame-Options
 *  block) and lets the model read DOM/console/eval on ANY page through the
 *  host bridge. */
export function BrowserPanel({ active = true }: { active?: boolean }) {
  return <OverlayBrowser active={active} />;
}

/** Toolbar shared by both modes. */
function BrowserToolbar({
  address,
  setAddress,
  onSubmit,
  onBack,
  onForward,
  onRefresh,
}: {
  address: string;
  setAddress: (v: string) => void;
  onSubmit: (e: React.FormEvent) => void;
  onBack: () => void;
  onForward: () => void;
  onRefresh: () => void;
}) {
  const { t } = useApp();
  return (
    <form className="browser-toolbar" onSubmit={onSubmit}>
      <button
        type="button"
        className="browser-btn"
        title={t("browser.back")}
        aria-label={t("browser.back")}
        onClick={onBack}
      >
        <Icon name="arrow-left" size={14} />
      </button>
      <button
        type="button"
        className="browser-btn"
        title={t("browser.forward")}
        aria-label={t("browser.forward")}
        onClick={onForward}
      >
        <Icon name="arrow-right" size={14} />
      </button>
      <button
        type="button"
        className="browser-btn"
        title={t("browser.refresh")}
        aria-label={t("browser.refresh")}
        onClick={onRefresh}
      >
        <Icon name="spinner" size={14} />
      </button>
      <input
        className="browser-address"
        value={address}
        placeholder={t("browser.address")}
        onChange={(e) => setAddress(e.target.value)}
        spellCheck={false}
      />
      <button type="submit" className="browser-btn browser-go">
        {t("browser.go")}
      </button>
    </form>
  );
}

/** Overlay mode: real content lives in a tracked host window; we report the
 *  placeholder rect and route commands through the host bridge. */
function OverlayBrowser({ active }: { active: boolean }) {
  const { t, subscribeBrowserCommand, sendBrowserResult, resolveBackendUrl } =
    useApp();
  const placeholderRef = useRef<HTMLDivElement | null>(null);
  const [address, setAddress] = useState("");
  const [currentUrl, setCurrentUrl] = useState("");
  // rAF token so a burst of resize/scroll events collapses to one bounds push.
  const rafRef = useRef<number | null>(null);

  const pushBounds = useCallback(() => {
    const api = hostApi();
    const el = placeholderRef.current;
    if (!api?.browser_overlay_set_bounds || !el) return;
    if (rafRef.current != null) return;
    rafRef.current = window.requestAnimationFrame(() => {
      rafRef.current = null;
      const el2 = placeholderRef.current;
      if (!el2) return;
      const r = el2.getBoundingClientRect();
      // Hide the overlay if the placeholder is collapsed/offscreen (e.g. the
      // panel is animating closed) rather than positioning a stray window.
      if (r.width < 2 || r.height < 2) {
        void api.browser_overlay_hide?.();
        return;
      }
      const left = r.left;
      const top = r.top;
      const width = Math.max(1, r.right - left);
      const height = Math.max(1, r.bottom - top);
      void api.browser_overlay_set_bounds?.(left, top, width, height);
    });
  }, []);

  // Track placeholder geometry only while the Browser tab is active AND a page
  // is loaded: the overlay window is shown over the placeholder, which only
  // occupies layout space when there's content. The overlay is a real OS
  // window kept alive in the background; we just show/position it when this
  // tab is active and hide it (without unloading the page) otherwise — so
  // switching to To-dos and back keeps the page running and re-reveals it.
  const hasPage = Boolean(currentUrl);
  const visible = active && hasPage;
  useLayoutEffect(() => {
    const api = hostApi();
    if (!visible) {
      // Tab inactive or no page: hide the overlay but keep its page loaded.
      void api?.browser_overlay_hide?.();
      return;
    }
    const el = placeholderRef.current;
    if (!el) return;
    void api?.browser_overlay_show?.();
    // Push bounds now and again after layout/paint settles. On the very first
    // reveal the placeholder's final rect isn't known until after the browser
    // lays the panel out, so a single synchronous push can land the overlay at
    // a stale (offset) position; the deferred pushes correct it.
    pushBounds();
    const raf1 = window.requestAnimationFrame(() => pushBounds());
    const t1 = window.setTimeout(() => pushBounds(), 60);
    const t2 = window.setTimeout(() => pushBounds(), 200);
    const ro = new ResizeObserver(() => pushBounds());
    ro.observe(el);
    const onWin = () => pushBounds();
    window.addEventListener("resize", onWin);
    window.addEventListener("scroll", onWin, true);
    return () => {
      window.cancelAnimationFrame(raf1);
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      ro.disconnect();
      window.removeEventListener("resize", onWin);
      window.removeEventListener("scroll", onWin, true);
      if (rafRef.current != null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      void hostApi()?.browser_overlay_hide?.();
    };
  }, [pushBounds, visible]);

  // Follow the panel live while a vertical divider is being dragged (the
  // ``body.resizing-x`` state). Unlike an iframe, a real overlay window has no
  // pointer-capture problem, so instead of hiding it (which the user perceives
  // as the browser vanishing) we keep re-reporting the placeholder rect on
  // each animation frame for the duration of the drag. We observe the body
  // class via a MutationObserver since the drag toggles it imperatively.
  useEffect(() => {
    const api = hostApi();
    if (!api) return;
    let followRaf: number | null = null;
    const follow = () => {
      if (!document.body.classList.contains("resizing-x")) {
        followRaf = null;
        // Final settle once the drag ends.
        pushBounds();
        return;
      }
      pushBounds();
      followRaf = window.requestAnimationFrame(follow);
    };
    const apply = () => {
      const dragging = document.body.classList.contains("resizing-x");
      if (dragging && followRaf == null) {
        followRaf = window.requestAnimationFrame(follow);
      }
    };
    const mo = new MutationObserver(apply);
    mo.observe(document.body, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => {
      mo.disconnect();
      if (followRaf != null) window.cancelAnimationFrame(followRaf);
    };
  }, [pushBounds]);

  // Run a backend-issued command against the overlay window via the host
  // bridge and return its result payload.
  const runCommand = useCallback(
    async (cmd: BrowserCommand): Promise<Record<string, unknown>> => {
      const api = hostApi();
      if (!api?.browser_overlay_command) {
        return { success: false, error: "overlay unavailable" };
      }
      const action = String(cmd.action || "");
      let url = "";
      if (action === "open") {
        url = normalizeUrl(String(cmd.url || ""));
      } else if (action === "open_preview") {
        url = resolveBackendUrl(String(cmd.url || ""));
      }
      const script = action === "eval" ? String(cmd.script || "") : "";
      const result = (await Promise.resolve(
        api.browser_overlay_command(action, url, script),
      )) as Record<string, unknown>;
      // Mirror the address bar for navigation results.
      if (
        (action === "open" || action === "open_preview") &&
        result &&
        result["success"]
      ) {
        const u = String(result["url"] || url || "");
        setCurrentUrl(u);
        setAddress(action === "open" ? u : String(cmd.url || ""));
      }
      if (action === "close") {
        setCurrentUrl("");
        setAddress("");
      }
      return result;
    },
    [resolveBackendUrl],
  );

  useEffect(() => {
    const unsub = subscribeBrowserCommand((cmd) => {
      const requestId = String((cmd as BrowserCommand).requestId || "");
      void runCommand(cmd as BrowserCommand).then((result) => {
        if (requestId) {
          void sendBrowserResult(requestId, result);
        }
      });
    });
    return unsub;
  }, [subscribeBrowserCommand, sendBrowserResult, runCommand]);

  const navigateTo = useCallback(
    (raw: string) => {
      const url = normalizeUrl(raw);
      if (!url) return;
      const api = hostApi();
      void api?.browser_overlay_command?.("open", url);
      setCurrentUrl(url);
      setAddress(url);
    },
    [],
  );

  const onSubmitAddress = (e: React.FormEvent) => {
    e.preventDefault();
    navigateTo(address);
  };

  return (
    <div className="browser-panel">
      <BrowserToolbar
        address={address}
        setAddress={setAddress}
        onSubmit={onSubmitAddress}
        onBack={() =>
          void hostApi()?.browser_overlay_command?.(
            "eval",
            "",
            "history.back()",
          )
        }
        onForward={() =>
          void hostApi()?.browser_overlay_command?.(
            "eval",
            "",
            "history.forward()",
          )
        }
        onRefresh={() => void hostApi()?.browser_overlay_command?.("refresh")}
      />
      <div className="browser-viewport">
        {currentUrl ? (
          // The overlay OS window is positioned over this placeholder; it only
          // occupies layout space while a page is loaded.
          <div ref={placeholderRef} className="browser-overlay-placeholder" />
        ) : (
          <div className="browser-empty">{t("browser.empty")}</div>
        )}
      </div>
    </div>
  );
}
