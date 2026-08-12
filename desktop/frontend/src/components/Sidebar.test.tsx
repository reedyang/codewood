import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const useAppMock = vi.fn();

vi.mock("../state/AppContext", () => ({
  useApp: () => useAppMock(),
}));

import { Sidebar } from "./Sidebar";

describe("Sidebar workspace routing", () => {
  beforeEach(() => {
    useAppMock.mockReset();
    vi.useRealTimers();
  });

  it("passes the clicked workspace id when selecting a chat from an optimistic workspace view", () => {
    const switchToChat = vi.fn(async () => undefined);
    const runCommand = vi.fn(async () => undefined);

    useAppMock.mockReturnValue({
      state: {
        workspace: {
          id: "ws-1",
          name: "Workspace A",
          root: "D:/workspace-a",
        },
        workspaces: [{
          id: "ws-2",
          name: "Workspace B",
          root: "D:/workspace-b",
          active: false,
          isDefault: false,
        }],
      },
      activeWorkspaceId: "ws-2",
      activeChatId: "chat-2",
      activeChats: [{
        id: "chat-2",
        name: "New Chat",
        active: true,
        archived: false,
      }, {
        id: "chat-3",
        name: "Existing Chat",
        active: false,
        archived: false,
      }],
      uiPrefs: {
        pinnedWorkspaceIds: [],
        pinnedChatIds: [],
        workspaceOrder: [],
      },
      workspaceChats: {
        "ws-2": [{
          id: "chat-3",
          name: "Existing Chat",
          active: false,
          archived: false,
        }],
      },
      expandedWorkspaceIds: ["ws-2"],
      busyByChat: {},
      runningChatStartedAtByChat: {},
      unreadChatIds: {},
      now: Date.parse("2026-07-08T15:20:00"),
      t: (key: string) => key,
      runCommand,
      switchToChat,
      newChat: vi.fn(async () => undefined),
      deleteChat: vi.fn(async () => undefined),
      openWorkspaceInExplorer: vi.fn(async () => true),
      deleteWorkspace: vi.fn(async () => true),
      toggleWorkspacePin: vi.fn(),
      toggleChatPin: vi.fn(),
      reorderWorkspace: vi.fn(),
      toggleChatArchive: vi.fn(async () => undefined),
      archiveChats: vi.fn(async () => undefined),
      toggleWorkspaceExpanded: vi.fn(),
      refreshWorkspaceChats: vi.fn(async () => undefined),
      client: {
        exportChat: vi.fn(async () => true),
        renameWorkspace: vi.fn(async () => true),
      },
    });

    render(<Sidebar collapsed={false} onOpenSettings={() => {}} />);

    // The backend still reports ws-1 while the optimistic UI focus is ws-2.
    // Its live list must not replace ws-2's cached rows during that handoff.
    expect(screen.queryByRole("button", { name: "New Chat" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Existing Chat" }));

    expect(switchToChat).toHaveBeenCalledWith("chat-3", "ws-2");
    expect(runCommand).not.toHaveBeenCalled();
  });

  it("shows elapsed time only for the running chat and relative updated time for idle chats", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-08T15:20:00"));

    useAppMock.mockReturnValue({
      state: {
        workspace: {
          id: "ws-2",
          name: "Workspace B",
          root: "D:/workspace-b",
        },
        workspaces: [{
          id: "ws-2",
          name: "Workspace B",
          root: "D:/workspace-b",
          active: true,
          isDefault: false,
        }],
      },
      activeWorkspaceId: "ws-2",
      activeChatId: "chat-2",
      activeChats: [{
        id: "chat-2",
        name: "Running Chat",
        active: true,
        archived: false,
        updatedAt: "2026-07-08 15:15:00",
        running: true,
      }, {
        id: "chat-3",
        name: "Idle Chat",
        active: false,
        archived: false,
        updatedAt: "2026-07-08 15:14:30",
      }],
      uiPrefs: {
        pinnedWorkspaceIds: [],
        pinnedChatIds: [],
        workspaceOrder: [],
      },
      workspaceChats: {},
      expandedWorkspaceIds: ["ws-2"],
      busyByChat: {
        "ws-2\u0000chat-2": true,
      },
      runningChatStartedAtByChat: {
        "ws-2\u0000chat-2": Date.parse("2026-07-08T15:19:30"),
      },
      unreadChatIds: {},
      now: Date.parse("2026-07-08T15:20:00"),
      t: (key: string) => key,
      runCommand: vi.fn(async () => undefined),
      switchToChat: vi.fn(async () => undefined),
      newChat: vi.fn(async () => undefined),
      deleteChat: vi.fn(async () => undefined),
      openWorkspaceInExplorer: vi.fn(async () => true),
      deleteWorkspace: vi.fn(async () => true),
      toggleWorkspacePin: vi.fn(),
      toggleChatPin: vi.fn(),
      reorderWorkspace: vi.fn(),
      toggleChatArchive: vi.fn(async () => undefined),
      archiveChats: vi.fn(async () => undefined),
      toggleWorkspaceExpanded: vi.fn(),
      refreshWorkspaceChats: vi.fn(async () => undefined),
      client: {
        exportChat: vi.fn(async () => true),
        renameWorkspace: vi.fn(async () => true),
      },
    });

    const { container } = render(<Sidebar collapsed={false} onOpenSettings={() => {}} />);

    expect(screen.getByText("30s")).toBeInTheDocument();
    expect(screen.getByText("5m")).toBeInTheDocument();
    expect(screen.getAllByText("30s")).toHaveLength(1);
    expect(container.querySelector(".chat-busy-dot")).toBeTruthy();
  });

  it("keeps concurrent running chats ordered by task start time while their updatedAt flips", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-08T15:20:00"));

    const mock = (chat2UpdatedAt: string) => ({
      state: {
        workspace: {
          id: "ws-2",
          name: "Workspace B",
          root: "D:/workspace-b",
        },
        workspaces: [{
          id: "ws-2",
          name: "Workspace B",
          root: "D:/workspace-b",
          active: true,
          isDefault: false,
        }],
      },
      activeWorkspaceId: "ws-2",
      activeChatId: "chat-2",
      activeChats: [{
        id: "chat-2",
        name: "Older Task",
        active: true,
        archived: false,
        // Mid-run sync bumped this chat's updatedAt past the other task's,
        // which used to make it jump to the top and then flip back.
        updatedAt: chat2UpdatedAt,
        running: true,
      }, {
        id: "chat-3",
        name: "Newer Task",
        active: false,
        archived: false,
        updatedAt: "2026-07-08 15:19:40",
        running: true,
      }],
      uiPrefs: {
        pinnedWorkspaceIds: [],
        pinnedChatIds: [],
        workspaceOrder: [],
      },
      workspaceChats: {},
      expandedWorkspaceIds: ["ws-2"],
      busyByChat: {
        "ws-2\u0000chat-2": true,
        "ws-2\u0000chat-3": true,
      },
      runningChatStartedAtByChat: {
        "ws-2\u0000chat-2": Date.parse("2026-07-08T15:19:30"),
        "ws-2\u0000chat-3": Date.parse("2026-07-08T15:19:50"),
      },
      unreadChatIds: {},
      now: Date.parse("2026-07-08T15:20:00"),
      t: (key: string) => key,
      runCommand: vi.fn(async () => undefined),
      switchToChat: vi.fn(async () => undefined),
      newChat: vi.fn(async () => undefined),
      deleteChat: vi.fn(async () => undefined),
      openWorkspaceInExplorer: vi.fn(async () => true),
      deleteWorkspace: vi.fn(async () => true),
      toggleWorkspacePin: vi.fn(),
      toggleChatPin: vi.fn(),
      reorderWorkspace: vi.fn(),
      toggleChatArchive: vi.fn(async () => undefined),
      archiveChats: vi.fn(async () => undefined),
      toggleWorkspaceExpanded: vi.fn(),
      refreshWorkspaceChats: vi.fn(async () => undefined),
      client: {
        exportChat: vi.fn(async () => true),
        renameWorkspace: vi.fn(async () => true),
      },
    });

    const rowNames = () =>
      Array.from(document.querySelectorAll(".tree-row.chat-row .tree-name"))
        .map((el) => el.textContent?.trim() ?? "");

    // chat-2 has the NEWEST updatedAt, but it started first, so it must stay
    // below chat-3. The order is driven by task start time, not the live
    // updatedAt that keeps bumping while the task runs.
    useAppMock.mockReturnValue(mock("2026-07-08 15:20:00"));
    const { rerender } = render(<Sidebar collapsed={false} onOpenSettings={() => {}} />);
    expect(rowNames()).toEqual(["Newer Task", "Older Task"]);

    // Another mid-run sync bumps chat-2's updatedAt even further; the order
    // must not flip back and forth.
    useAppMock.mockReturnValue(mock("2026-07-08 15:20:30"));
    rerender(<Sidebar collapsed={false} onOpenSettings={() => {}} />);
    expect(rowNames()).toEqual(["Newer Task", "Older Task"]);
  });

  it("resets the visible chat count after collapsing and re-expanding a workspace", () => {
    const chats = Array.from({ length: 7 }, (_, i) => ({
      id: `chat-${i}`,
      name: `Chat ${i + 1}`,
      active: false,
      archived: false,
    }));
    const baseMock = () => ({
      state: {
        workspace: {
          id: "ws-1",
          name: "Workspace A",
          root: "D:/workspace-a",
        },
        workspaces: [{
          id: "ws-1",
          name: "Workspace A",
          root: "D:/workspace-a",
          active: true,
          isDefault: false,
        }],
      },
      activeWorkspaceId: "ws-1",
      activeChatId: "chat-0",
      activeChats: chats,
      uiPrefs: {
        pinnedWorkspaceIds: [],
        pinnedChatIds: [],
        workspaceOrder: [],
      },
      workspaceChats: {},
      busyByChat: {},
      runningChatStartedAtByChat: {},
      unreadChatIds: {},
      now: Date.parse("2026-07-08T15:20:00"),
      t: (key: string) => key,
      runCommand: vi.fn(async () => undefined),
      switchToChat: vi.fn(async () => undefined),
      newChat: vi.fn(async () => undefined),
      deleteChat: vi.fn(async () => undefined),
      openWorkspaceInExplorer: vi.fn(async () => true),
      deleteWorkspace: vi.fn(async () => true),
      toggleWorkspacePin: vi.fn(),
      toggleChatPin: vi.fn(),
      reorderWorkspace: vi.fn(),
      toggleChatArchive: vi.fn(async () => undefined),
      archiveChats: vi.fn(async () => undefined),
      toggleWorkspaceExpanded: vi.fn(),
      refreshWorkspaceChats: vi.fn(async () => undefined),
      client: {
        exportChat: vi.fn(async () => true),
        renameWorkspace: vi.fn(async () => true),
      },
    });

    useAppMock.mockReturnValue({ ...baseMock(), expandedWorkspaceIds: ["ws-1"] });
    const { rerender } = render(<Sidebar collapsed={false} onOpenSettings={() => {}} />);

    expect(screen.getAllByRole("button").filter((b) => b.textContent?.startsWith("Chat "))).toHaveLength(5);
    expect(screen.getByText("sidebar.loadMore")).toBeInTheDocument();

    fireEvent.click(screen.getByText("sidebar.loadMore"));
    expect(screen.getAllByRole("button").filter((b) => b.textContent?.startsWith("Chat "))).toHaveLength(7);
    expect(screen.queryByText("sidebar.loadMore")).not.toBeInTheDocument();

    useAppMock.mockReturnValue({ ...baseMock(), expandedWorkspaceIds: [] });
    rerender(<Sidebar collapsed={false} onOpenSettings={() => {}} />);
    expect(screen.queryAllByRole("button").filter((b) => b.textContent?.startsWith("Chat "))).toHaveLength(0);

    useAppMock.mockReturnValue({ ...baseMock(), expandedWorkspaceIds: ["ws-1"] });
    rerender(<Sidebar collapsed={false} onOpenSettings={() => {}} />);
    expect(screen.getAllByRole("button").filter((b) => b.textContent?.startsWith("Chat "))).toHaveLength(5);
    expect(screen.getByText("sidebar.loadMore")).toBeInTheDocument();
  });
});
