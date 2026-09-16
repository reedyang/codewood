import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const useAppMock = vi.fn();

vi.mock("../state/AppContext", () => ({
  useApp: () => useAppMock(),
}));

import { UpdateButton } from "./UpdateButton";

const startInstall = vi.fn();
const updateState = vi.fn();

function setHostBridge(present: boolean) {
  (window as unknown as { pywebview?: unknown }).pywebview = present
    ? { api: { update_state: updateState, start_update_install: startInstall } }
    : undefined;
}

function state(overrides: Record<string, unknown> = {}) {
  return {
    status: "idle",
    version: "",
    progress: 0,
    received: 0,
    total: 0,
    error: "",
    ...overrides,
  };
}

describe("UpdateButton", () => {
  beforeEach(() => {
    useAppMock.mockReset();
    startInstall.mockReset();
    updateState.mockReset();
    // Echo the key so assertions can match on translation ids.
    useAppMock.mockReturnValue({
      t: (key: string, params?: Record<string, unknown>) =>
        params ? `${key} ${JSON.stringify(params)}` : key,
    });
    setHostBridge(true);
  });

  afterEach(() => {
    setHostBridge(false);
    vi.useRealTimers();
  });

  it("renders nothing when no update is pending", async () => {
    updateState.mockResolvedValue(state({ status: "up-to-date" }));
    render(<UpdateButton />);
    await waitFor(() => expect(updateState).toHaveBeenCalled());
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders nothing without a host bridge", async () => {
    setHostBridge(false);
    render(<UpdateButton />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(updateState).not.toHaveBeenCalled();
  });

  it("stays hidden while the download is in flight", async () => {
    updateState.mockResolvedValue(
      state({ status: "downloading", version: "v0.2.0", progress: 0.42 }),
    );
    render(<UpdateButton />);
    await waitFor(() => expect(updateState).toHaveBeenCalled());
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("appears and launches the installer once the package is ready", async () => {
    updateState.mockResolvedValue(
      state({ status: "ready", version: "v0.2.0", progress: 1 }),
    );
    startInstall.mockResolvedValue(true);
    render(<UpdateButton />);
    const button = await screen.findByRole("button");
    expect(button.textContent).toContain("update.button");
    expect(button.getAttribute("title")).toContain("v0.2.0");

    await act(async () => {
      button.click();
    });
    expect(startInstall).toHaveBeenCalledTimes(1);
  });

  it("only launches the installer once", async () => {
    updateState.mockResolvedValue(
      state({ status: "ready", version: "v0.2.0", progress: 1 }),
    );
    startInstall.mockResolvedValue(true);
    render(<UpdateButton />);
    const button = await screen.findByRole("button");
    await act(async () => {
      button.click();
      button.click();
    });
    expect(startInstall).toHaveBeenCalledTimes(1);
  });

  it("appears when the host pushes update-ready", async () => {
    updateState.mockResolvedValue(state({ status: "downloading", progress: 0.1 }));
    render(<UpdateButton />);
    await waitFor(() => expect(updateState).toHaveBeenCalled());
    const callsBefore = updateState.mock.calls.length;

    updateState.mockResolvedValue(
      state({ status: "ready", version: "v0.9.0", progress: 1 }),
    );
    await act(async () => {
      window.dispatchEvent(new CustomEvent("codewood:update-ready", { detail: {} }));
    });
    await waitFor(() =>
      expect(updateState.mock.calls.length).toBeGreaterThan(callsBefore),
    );
    const button = await screen.findByRole("button");
    expect(button.getAttribute("title")).toContain("v0.9.0");
  });
});