import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppState, ServerEvent, Turn } from "../api/types";

const apiMock = vi.hoisted(() => {
  let eventHandler: ((event: ServerEvent) => void) | null = null;
  const getState = vi.fn<() => Promise<AppState>>();
  const connectEvents = vi.fn((handler: (event: ServerEvent) => void) => {
    eventHandler = handler;
    return { close: vi.fn() } as unknown as EventSource;
  });
  const getChatHistory = vi.fn(async () => ({ turns: [], start: 0, total: 0 }));
  const listWorkspaceChats = vi.fn(async () => []);
  return {
    getState,
    connectEvents,
    getChatHistory,
    listWorkspaceChats,
    emit(event: ServerEvent) {
      if (!eventHandler) {
        throw new Error("Event handler not connected");
      }
      eventHandler(event);
    },
    reset() {
      eventHandler = null;
      getState.mockReset();
      connectEvents.mockClear();
      getChatHistory.mockClear();
      listWorkspaceChats.mockClear();
    },
  };
});

vi.mock("../api/client", () => ({
  ApiClient: class {
    getState = apiMock.getState;
    connectEvents = apiMock.connectEvents;
    getChatHistory = apiMock.getChatHistory;
    listWorkspaceChats = apiMock.listWorkspaceChats;
  },
}));

import { AppProvider, useApp } from "./AppContext";

function buildState(): AppState {
  return {
    app: { name: "Code Wood", version: "test" },
    workspace: {
      id: "ws-1",
      name: "Workspace",
      root: "D:/workspace",
      workDirectory: "D:/workspace",
    },
    workspaces: [{
      id: "ws-1",
      name: "Workspace",
      root: "D:/workspace",
      active: true,
      isDefault: false,
    }],
    chats: [{
      index: 0,
      id: "chat-1",
      name: "Chat 1",
      messageCount: 0,
      active: true,
      running: false,
      archived: false,
      planMode: false,
      model: "provider:model",
    }],
    activeChatId: "chat-1",
    model: {
      current: "provider:model",
      available: ["provider:model"],
      ready: true,
      reasoningEffort: "",
      reasoningEfforts: [],
    },
    language: "en",
    executionPolicy: "default",
  };
}

function TurnsProbe() {
  const { turns } = useApp();
  return <pre data-testid="turns">{JSON.stringify(turns)}</pre>;
}

describe("AppContext thinking rounds", () => {
  beforeEach(() => {
    apiMock.reset();
    apiMock.getState.mockResolvedValue(buildState());
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    );
    window.localStorage.clear();
  });

  it("keeps consecutive tool-loop thinking in separate live rounds", async () => {
    render(
      <AppProvider>
        <TurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "Investigate", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_start",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "thinking",
        data: { text: "first thought", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "tool output 1", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_end",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_start",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "thinking",
        data: { text: "second thought", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "tool output 2", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(1);
      expect(turns[0].rounds).toHaveLength(2);
      expect(turns[0].rounds[0].thinkingText).toBe("first thought");
      expect(turns[0].rounds[1].thinkingText).toBe("second thought");
      expect(turns[0].rounds[0].segments.map((segment) => segment.text).join("")).toContain("tool output 1");
      expect(turns[0].rounds[1].segments.map((segment) => segment.text).join("")).toContain("tool output 2");
      expect(turns[0].rounds[0].thinkingEndedAt).toBeTypeOf("number");
    });
  });
});
