import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { IndexStatus } from "../api/types";
import { useApp } from "../state/AppContext";
import { StatusBar } from "./StatusBar";

vi.mock("../state/AppContext", () => ({
  useApp: vi.fn(),
}));

const mockedUseApp = vi.mocked(useApp);

function mockApp(status: IndexStatus) {
  const fetchIndexStatus = vi.fn().mockResolvedValue(status);
  mockedUseApp.mockReturnValue({
    state: {
      workspace: {
        id: "workspace-1",
        name: "Workspace",
      },
    },
    client: {
      fetchIndexStatus,
    },
  } as never);
  return fetchIndexStatus;
}

describe("StatusBar", () => {
  beforeEach(() => {
    mockedUseApp.mockReset();
  });

  it("renders scanning progress as a percentage label", async () => {
    mockApp({
      hidden: false,
      files_total: 120,
      workspace_name: "Workspace",
      is_default_workspace: false,
      refresh_phase: "scanning",
      refresh_progress_total: 0,
      refresh_progress_done: 42,
      refresh_progress_percent: 42,
    });

    const { getByText } = render(<StatusBar />);
    await waitFor(() => expect(getByText("Scanning 42%")).toBeInTheDocument());
  });

  it("renders indexing and saving progress as percentage labels", async () => {
    mockApp({
      hidden: false,
      files_total: 120,
      workspace_name: "Workspace",
      is_default_workspace: false,
      refresh_phase: "indexing",
      refresh_progress_total: 10,
      refresh_progress_done: 6,
      refresh_progress_percent: 68,
    });

    const indexing = render(<StatusBar />);
    await waitFor(() => expect(indexing.getByText("Indexing 68%")).toBeInTheDocument());
    indexing.unmount();

    mockApp({
      hidden: false,
      files_total: 120,
      workspace_name: "Workspace",
      is_default_workspace: false,
      refresh_phase: "saving",
      refresh_progress_total: 0,
      refresh_progress_done: 0,
      refresh_progress_percent: 100,
    });

    const saving = render(<StatusBar />);
    await waitFor(() => expect(saving.getByText("Saving 100%")).toBeInTheDocument());
  });

  it("renders the indexed file count when idle", async () => {
    mockApp({
      hidden: false,
      files_total: 12,
      workspace_name: "Workspace",
      is_default_workspace: false,
      refresh_phase: "",
      refresh_progress_total: 0,
      refresh_progress_done: 0,
      refresh_progress_percent: 0,
    });

    const { getByText } = render(<StatusBar />);
    await waitFor(() => expect(getByText("Index: 12 files")).toBeInTheDocument());
  });

  it("renders nothing when the backend hides index status", async () => {
    mockApp({
      hidden: true,
      files_total: 0,
      workspace_name: "",
      is_default_workspace: false,
      refresh_phase: "",
      refresh_progress_total: 0,
      refresh_progress_done: 0,
      refresh_progress_percent: 0,
    });

    const { container } = render(<StatusBar />);
    await waitFor(() => expect(container.firstChild).toBeNull());
  });
});
