import { afterEach, describe, expect, it } from "vitest";
import { hostApi } from "./hostApi";

// The host bridge is injected by the desktop host at runtime as
// ``window.pywebview.api``. These tests cover the accessor's contract: it must
// return undefined when running outside the host (plain browser / dev server)
// and surface the overlay-control methods when present.

afterEach(() => {
  delete (window as unknown as { pywebview?: unknown }).pywebview;
});

describe("hostApi", () => {
  it("returns undefined when the pywebview bridge is absent", () => {
    expect(hostApi()).toBeUndefined();
  });

  it("returns the api object when the bridge is present", () => {
    const api = {
      browser_overlay_supported: () => true,
      browser_overlay_set_bounds: () => true,
    };
    (window as unknown as { pywebview?: { api?: unknown } }).pywebview = { api };
    expect(hostApi()).toBe(api);
    expect(hostApi()?.browser_overlay_supported?.()).toBe(true);
  });

  it("tolerates a bridge object without an api member", () => {
    (window as unknown as { pywebview?: unknown }).pywebview = {};
    expect(hostApi()).toBeUndefined();
  });
});
