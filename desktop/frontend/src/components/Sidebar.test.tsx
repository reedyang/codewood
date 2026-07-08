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
      unreadChatIds: {},
      t: (key: string) => key,
      runCommand,
      switchToChat,
      newChat: vi.fn(async () => undefined),
      deleteChat: vi.fn(async () => undefined),
      openWorkspaceInExplorer: vi.fn(async () => true),
      deleteWorkspace: vi.fn(async () => true),
      toggleWorkspacePin: vi.fn(),
      toggleChatPin: vi.fn(),
      toggleChatArchive: vi.fn(async () => undefined),
      archiveChats: vi.fn(async () => undefined),
      toggleWorkspaceExpanded: vi.fn(),
      refreshWorkspaceChats: vi.fn(async () => undefined),
      client: {
        exportChat: vi.fn(async () => true),
      },
    });

    render(<Sidebar collapsed={false} onOpenSettings={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "Existing Chat" }));

    expect(switchToChat).toHaveBeenCalledWith("chat-3", "ws-2");
    expect(runCommand).not.toHaveBeenCalled();
  });
});
