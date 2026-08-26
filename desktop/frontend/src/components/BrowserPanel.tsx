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

// Reply timeout for postMessage round-trips to a preview page's bridge script.
const BRIDGE_TIMEOUT_MS = 4000;

function normalizeUrl(raw: string): string {
  const s = String(raw || "").trim();
  if (!s) return "";
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(s) || s.startsWith("about:")) {
    return s;
  }
  return `https://${s}`;
}

/** The global embedded browser.
 *
 * Two rendering modes:
 *  - Overlay mode (desktop host, when ``browser_overlay_supported`` is true):
 *    the real content is a separate, tracked pywebview window positioned over
 *    the placeholder below. This loads external sites (no X-Frame-Options
 *    block) and lets the model read DOM/console/eval on ANY page through the
 *    host bridge. We only render the toolbar + a placeholder and report its
 *    on-screen rect to the host.
 *  - Iframe fallback (plain browser / unsupported platform): a single
 *    sandboxed iframe. External sites that forbid framing won't load and
 *    content reads only work for our own preview pages via a postMessage
 *    bridge.
 */
export function BrowserPanel({ active = true }: { active?: boolean }) {
  // Overlay capability is detected asynchronously once on mount. ``null`` =
  // unknown (render nothing content-wise yet), true = overlay, false = iframe.
  const [overlayMode, setOverlayMode] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    const api = hostApi();
    if (!api?.browser_overlay_supported) {
      setOverlayMode(false);
      return;
    }
    void Promise.resolve(api.browser_overlay_supported())
      .then((ok) => {
        if (!cancelled) setOverlayMode(Boolean(ok));
      })
      .catch(() => {
        if (!cancelled) setOverlayMode(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (overlayMode === true) {
    return <OverlayBrowser active={active} />;
  }
  // While detecting, fall through to the iframe renderer's chrome (toolbar +
  // empty viewport) so there's no flicker; once known false it stays iframe.
  return <IframeBrowser />;
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

/** Iframe fallback (unchanged behavior): a single sandboxed iframe. Navigation
 *  is fully controllable; content reads (DOM/console/eval) work only for our
 *  own preview pages, which embed a postMessage bridge — external cross-origin
 *  sites are not readable and such commands return an explicit error. */
function IframeBrowser() {
  const { t, subscribeBrowserCommand, sendBrowserResult, resolveBackendUrl } =
    useApp();
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [address, setAddress] = useState("");
  const [currentUrl, setCurrentUrl] = useState("");
  const isPreviewRef = useRef(false);
  const bridgePendingRef = useRef<
    Map<string, (payload: Record<string, unknown>) => void>
  >(new Map());

  const [frameSrc, setFrameSrc] = useState("about:blank");
  const [frameDoc, setFrameDoc] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const navigate = useCallback(
    (url: string, isPreview: boolean, doc?: string) => {
      isPreviewRef.current = isPreview;
      setCurrentUrl(url);
      setAddress(url);
      if (doc != null) {
        setFrameDoc(doc);
        setFrameSrc("about:blank");
      } else {
        setFrameDoc(null);
        setFrameSrc(url || "about:blank");
      }
      setReloadKey((k) => k + 1);
    },
    [],
  );

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const data = e.data as Record<string, unknown> | null;
      if (!data || typeof data !== "object") return;
      if (data["__codewoodBridge"] !== true) return;
      const nonce = String(data["nonce"] || "");
      const resolver = bridgePendingRef.current.get(nonce);
      if (resolver) {
        bridgePendingRef.current.delete(nonce);
        resolver(data);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  const askBridge = useCallback(
    (action: string, script?: string): Promise<Record<string, unknown>> => {
      return new Promise((resolve) => {
        const frame = iframeRef.current;
        if (!isPreviewRef.current || !frame || !frame.contentWindow) {
          resolve({
            ok: false,
            error: "cross-origin: not accessible in iframe mode",
          });
          return;
        }
        const nonce = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const timer = window.setTimeout(() => {
          if (bridgePendingRef.current.has(nonce)) {
            bridgePendingRef.current.delete(nonce);
            resolve({ ok: false, error: "bridge timeout" });
          }
        }, BRIDGE_TIMEOUT_MS);
        bridgePendingRef.current.set(nonce, (payload) => {
          window.clearTimeout(timer);
          resolve(payload);
        });
        try {
          frame.contentWindow.postMessage(
            { __codewoodBridge: true, nonce, action, script },
            "*",
          );
        } catch {
          window.clearTimeout(timer);
          bridgePendingRef.current.delete(nonce);
          resolve({ ok: false, error: "postMessage failed" });
        }
      });
    },
    [],
  );

  const runCommand = useCallback(
    async (cmd: BrowserCommand): Promise<Record<string, unknown>> => {
      const action = String(cmd.action || "");
      switch (action) {
        case "open": {
          const url = normalizeUrl(String(cmd.url || ""));
          if (!url) return { success: false, error: "missing url" };
          navigate(url, false);
          return { success: true, url };
        }
        case "open_preview": {
          const url = resolveBackendUrl(String(cmd.url || ""));
          if (!url) return { success: false, error: "missing url" };
          try {
            const res = await fetch(url);
            if (!res.ok) {
              return { success: false, error: `fetch failed: ${res.status}` };
            }
            const doc = await res.text();
            navigate(String(cmd.url || "preview"), true, doc);
            return { success: true, url: String(cmd.url || "") };
          } catch (e) {
            return { success: false, error: `fetch error: ${String(e)}` };
          }
        }
        case "close": {
          navigate("", false);
          return { success: true };
        }
        case "refresh": {
          if (currentUrl) {
            setReloadKey((k) => k + 1);
          }
          return { success: true, url: currentUrl };
        }
        case "get_url": {
          return { success: true, url: currentUrl };
        }
        case "read_dom": {
          const r = await askBridge("read_dom");
          return r["ok"]
            ? { success: true, dom: r["dom"] }
            : { success: false, error: String(r["error"] || "unavailable") };
        }
        case "read_console": {
          const r = await askBridge("read_console");
          return r["ok"]
            ? { success: true, console: r["console"] }
            : { success: false, error: String(r["error"] || "unavailable") };
        }
        case "eval": {
          const r = await askBridge("eval", String(cmd.script || ""));
          return r["ok"]
            ? { success: true, result: r["result"] }
            : { success: false, error: String(r["error"] || "unavailable") };
        }
        default:
          return { success: false, error: `unknown action: ${action}` };
      }
    },
    [askBridge, currentUrl, navigate, resolveBackendUrl],
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

  const onSubmitAddress = (e: React.FormEvent) => {
    e.preventDefault();
    const url = normalizeUrl(address);
    if (url) {
      navigate(url, false);
    }
  };

  return (
    <div className="browser-panel">
      <BrowserToolbar
        address={address}
        setAddress={setAddress}
        onSubmit={onSubmitAddress}
        onBack={() => {
          try {
            iframeRef.current?.contentWindow?.history.back();
          } catch {
            // Cross-origin history navigation may be blocked; ignore.
          }
        }}
        onForward={() => {
          try {
            iframeRef.current?.contentWindow?.history.forward();
          } catch {
            // Ignore.
          }
        }}
        onRefresh={() => {
          if (currentUrl) {
            setReloadKey((k) => k + 1);
          }
        }}
      />
      <div className="browser-viewport">
        <iframe
          key={reloadKey}
          ref={iframeRef}
          className="browser-frame"
          title="Embedded browser"
          sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-modals"
          {...(frameDoc != null ? { srcDoc: frameDoc } : { src: frameSrc })}
          style={{ display: currentUrl ? "block" : "none" }}
        />
        {!currentUrl && <div className="browser-empty">{t("browser.empty")}</div>}
      </div>
    </div>
  );
}
