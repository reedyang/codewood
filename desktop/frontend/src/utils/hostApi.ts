/** Auto-update status reported by the desktop host (see desktop/host/updater.py). */
export interface UpdateState {
  status:
    | "idle"
    | "checking"
    | "up-to-date"
    | "downloading"
    | "ready"
    | "failed";
  version: string;
  progress: number;
  received: number;
  total: number;
  error: string;
}

/** Typed accessor for the pywebview host bridge (``window.pywebview.api``).
 *
 * Only the methods the web layer actually calls are declared; every member is
 * optional because the bridge is absent when the frontend runs in a plain
 * browser (dev server) rather than inside the desktop host. Callers must always
 * guard for ``undefined``.
 */
export interface HostBridgeApi {
  minimize?: () => void;
  toggle_maximize?: () => boolean | Promise<boolean>;
  close_window?: () => void;
  open_external?: (url: string) => boolean | Promise<boolean>;
  start_window_drag?: () => boolean | Promise<boolean>;
  host_platform?: () => string | Promise<string>;
  // Embedded browser overlay window controls.
  browser_overlay_set_bounds?: (
    x: number,
    y: number,
    width: number,
    height: number,
  ) => boolean | Promise<boolean>;
  browser_overlay_show?: () => boolean | Promise<boolean>;
  browser_overlay_hide?: () => boolean | Promise<boolean>;
  browser_overlay_command?: (
    action: string,
    url?: string,
    script?: string,
  ) => Record<string, unknown> | Promise<Record<string, unknown>>;
  /** Re-preview a local HTML file: host opens the overlay and navigates.
   *  Returns ``true`` on success. */
  browser_overlay_preview_path?: (
    path: string,
  ) => boolean | Promise<boolean>;
  /** Toggle mouse-event passthrough on the overlay window (macOS).
   *  When enabled, mouse events pass through to the main window so the user
   *  can interact with UI elements behind the overlay (e.g. panel resizer). */
  browser_overlay_set_passthrough?: (
    enabled: boolean,
  ) => boolean | Promise<boolean>;
  /** Open a native Save As dialog; returns the chosen file path or "". */
  save_file_dialog?: () => string | Promise<string>;
  /** Read the system clipboard as text (host-native, no browser permission
   *  prompt). Returns "" when the clipboard holds no text. */
  get_clipboard_text?: () => string | Promise<string>;
  /** Auto-update state, polled while a package downloads. ``status`` is one
   *  of "idle" | "checking" | "up-to-date" | "downloading" | "ready" |
   *  "failed"; ``progress`` is 0..1. */
  update_state?: () => UpdateState | Promise<UpdateState>;
  /** Launch the downloaded installer and quit Code Wood. */
  start_update_install?: () => boolean | Promise<boolean>;
  /** Current backend endpoint (port/token). Changes after a crash-restart,
   *  telling the frontend to rebuild its API client and reconnect. */
  backend_info?: () =>
    | { port: number; token: string }
    | Promise<{ port: number; token: string }>;
}

/** Return the host bridge if running inside the desktop host, else undefined. */
export function hostApi(): HostBridgeApi | undefined {
  return (window as unknown as { pywebview?: { api?: HostBridgeApi } }).pywebview
    ?.api;
}
