import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { hostApi, type UpdateState } from "../utils/hostApi";
import { Icon } from "./Icon";

/** Interval between polls while a download is running, and while idle. */
const ACTIVE_POLL_MS = 800;
const IDLE_POLL_MS = 15000;

/** Statuses in which a transfer is still in flight and worth polling fast. */
const ACTIVE_STATUSES = new Set(["checking", "downloading"]);

/**
 * Title-bar "Update" button.
 *
 * Deliberately hidden until the package has fully downloaded — the transfer
 * runs silently in the background. Once the host reports the package ready the
 * button appears with the release version; clicking it launches the installer
 * and quits Code Wood. Renders nothing when the host bridge is absent
 * (plain-browser development) or no update is ready.
 */
export function UpdateButton() {
  const { t } = useApp();
  const [state, setState] = useState<UpdateState | null>(null);
  const launchedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const poll = async () => {
      let next = IDLE_POLL_MS;
      const api = hostApi();
      if (!api || typeof api.update_state !== "function") {
        return;
      }
      try {
        const info = await api.update_state!();
        if (cancelled) {
          return;
        }
        setState(info);
        if (info && ACTIVE_STATUSES.has(info.status)) {
          next = ACTIVE_POLL_MS;
        }
      } catch {
        // Host bridge busy/unavailable; retry on the next tick.
      }
      if (!cancelled) {
        timer = window.setTimeout(() => void poll(), next);
      }
    };

    const onReady = () => {
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
      void poll();
    };

    // pywebview injects its API after the frontend has mounted on startup.
    // Keep listening even when the first hostApi() lookup returns undefined.
    window.addEventListener("pywebviewready", onReady);
    window.addEventListener("codewood:update-ready", onReady);
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
      window.removeEventListener("pywebviewready", onReady);
      window.removeEventListener("codewood:update-ready", onReady);
    };
  }, []);

  // The download is silent: nothing is shown until the package is complete.
  if (!state || state.status !== "ready") {
    return null;
  }

  const version = state.version || "";
  const title = t("update.ready", { version });

  const install = () => {
    const api = hostApi();
    if (!api?.start_update_install || launchedRef.current) {
      return;
    }
    launchedRef.current = true;
    void Promise.resolve(api.start_update_install()).catch(() => {
      launchedRef.current = false;
    });
  };

  return (
    <button
      className="update-btn"
      aria-label={title}
      title={title}
      onClick={install}
    >
      <Icon name="update" size={14} />
      <span className="update-btn-label">{t("update.button")}</span>
    </button>
  );
}
