import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { Icon } from "./Icon";

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

/** The global embedded browser. A single sandboxed iframe with a toolbar.
 *  Navigation is fully controllable (here and by the model via SSE commands);
 *  content reads (DOM/console/eval) work only for our own preview pages, which
 *  embed a postMessage bridge — external cross-origin sites are not readable
 *  and such commands return an explicit error. */
export function BrowserPanel() {
  const { t, subscribeBrowserCommand, sendBrowserResult, resolveBackendUrl } =
    useApp();
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [address, setAddress] = useState("");
  const [currentUrl, setCurrentUrl] = useState("");
  // Whether the currently loaded page is one of our own preview pages (only
  // those carry the bridge script and are therefore readable).
  const isPreviewRef = useRef(false);
  // Pending bridge round-trips keyed by a local nonce.
  const bridgePendingRef = useRef<
    Map<string, (payload: Record<string, unknown>) => void>
  >(new Map());

  // ``frameSrc`` is the controlled iframe source. We drive it through state so
  // navigation works even on the very first open (when the iframe element may
  // not be in the DOM yet) and survives re-renders. ``reloadKey`` lets refresh
  // force a reload even when the URL is unchanged.
  const [frameSrc, setFrameSrc] = useState("about:blank");
  // For our own preview pages we load the HTML via ``srcdoc`` instead of a
  // cross-origin http URL: WebView2 blocks a file:// document from framing a
  // http://127.0.0.1 page (mixed-content), and srcdoc content is same-origin
  // with the parent so the preview bridge (and even direct DOM access) works.
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

  // Bridge replies from preview pages.
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

  // Ask the current preview page's bridge for something; reject for non-preview
  // (cross-origin) pages where the bridge is absent.
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

  // Execute a backend-issued command and return its result payload.
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
          // A preview page served by our backend (carries the bridge script).
          // Fetch its HTML and load it via srcdoc (same-origin with the parent),
          // sidestepping the file://->http mixed-content block in WebView2.
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

  // Wire backend commands to execution + result post-back.
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
      <form className="browser-toolbar" onSubmit={onSubmitAddress}>
        <button
          type="button"
          className="browser-btn"
          title={t("browser.back")}
          aria-label={t("browser.back")}
          onClick={() => {
            try {
              iframeRef.current?.contentWindow?.history.back();
            } catch {
              // Cross-origin history navigation may be blocked; ignore.
            }
          }}
        >
          <Icon name="arrow-left" size={14} />
        </button>
        <button
          type="button"
          className="browser-btn"
          title={t("browser.forward")}
          aria-label={t("browser.forward")}
          onClick={() => {
            try {
              iframeRef.current?.contentWindow?.history.forward();
            } catch {
              // Ignore.
            }
          }}
        >
          <Icon name="arrow-right" size={14} />
        </button>
        <button
          type="button"
          className="browser-btn"
          title={t("browser.refresh")}
          aria-label={t("browser.refresh")}
          onClick={() => {
            if (currentUrl) {
              setReloadKey((k) => k + 1);
            }
          }}
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
