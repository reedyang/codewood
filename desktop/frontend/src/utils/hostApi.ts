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
  browser_overlay_supported?: () => boolean | Promise<boolean>;
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
}

/** Return the host bridge if running inside the desktop host, else undefined. */
export function hostApi(): HostBridgeApi | undefined {
  return (window as unknown as { pywebview?: { api?: HostBridgeApi } }).pywebview
    ?.api;
}
