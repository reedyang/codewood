import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  const newChat = vi.fn(async () => "chat-2");
  const pasteImage = vi.fn(async () => ({ path: "D:/workspace-b/.codewood/chats/data/record-chat-2/img.png", name: "img.png" }));
  const selectChat = vi.fn(async () => true);
  const sendInput = vi.fn(async () => undefined);
  const syncModelPresets = vi.fn(async () => undefined);
  return {
    getState,
    connectEvents,
    getChatHistory,
    listWorkspaceChats,
    newChat,
    pasteImage,
    selectChat,
    sendInput,
    syncModelPresets,
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
      newChat.mockClear();
      pasteImage.mockClear();
      selectChat.mockClear();
      sendInput.mockClear();
      syncModelPresets.mockClear();
    },
  };
});

vi.mock("../api/client", () => ({
  ApiClient: class {
    getState = apiMock.getState;
    connectEvents = apiMock.connectEvents;
    getChatHistory = apiMock.getChatHistory;
    listWorkspaceChats = apiMock.listWorkspaceChats;
    newChat = apiMock.newChat;
    pasteImage = apiMock.pasteImage;
    selectChat = apiMock.selectChat;
    sendInput = apiMock.sendInput;
    syncModelPresets = apiMock.syncModelPresets;
  },
}));

import { AppProvider, useApp } from "./AppContext";

function buildState(overrides: Partial<AppState> = {}): AppState {
  const base: AppState = {
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
    }, {
      id: "ws-2",
      name: "Workspace B",
      root: "D:/workspace-b",
      active: false,
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
      model: "provider/model",
    }],
    activeChatId: "chat-1",
    model: {
      current: "provider/model",
      available: ["provider/model"],
      ready: true,
      reasoningEffort: "",
      reasoningEfforts: [],
    },
    language: "en",
    executionPolicy: "default",
  };
  return {
    ...base,
    ...overrides,
  };
}

function TurnsProbe() {
  const { turns } = useApp();
  return <pre data-testid="turns">{JSON.stringify(turns)}</pre>;
}

function DraftCreateProbe() {
  const {
    state,
    activeWorkspaceId,
    activeChatId,
    activeChats,
    turns,
    newChat,
    pasteImage,
    sendInput,
    setModel,
  } = useApp();
  return (
    <>
      <button
        onClick={() => {
          void newChat("ws-2");
        }}
      >
        enter draft
      </button>
      <button
        onClick={() => {
          void pasteImage("data:image/png;base64,AAAA");
        }}
      >
        paste draft image
      </button>
      <button
        onClick={() => {
          void sendInput("hello from draft");
        }}
      >
        send draft
      </button>
      <button
        onClick={() => {
          void setModel("openai/family/model/v2");
        }}
      >
        select draft model
      </button>
      <pre data-testid="app-state">{JSON.stringify(state)}</pre>
      <pre
        data-testid="active-view"
      >{JSON.stringify({ activeWorkspaceId, activeChatId, activeChats, turns })}</pre>
    </>
  );
}

function HistoryReloadProbe() {
  const { state, selectWorkspace } = useApp();
  return (
    <>
      <button onClick={() => { void selectWorkspace("ws-2"); }}>
        switch workspace
      </button>
      <pre data-testid="history-state">{JSON.stringify(state)}</pre>
    </>
  );
}

function BusySwitchProbe() {
  const {
    activeWorkspaceId,
    activeChatId,
    busyByChat,
    runningChatStartedAtByChat,
    turns,
    switchToChat,
  } = useApp();
  return (
    <>
      <button onClick={() => { void switchToChat("chat-2", "ws-1"); }}>
        switch to chat 2
      </button>
      <button onClick={() => { void switchToChat("chat-1", "ws-1"); }}>
        switch back to chat 1
      </button>
      <pre data-testid="busy-switch-view">
        {JSON.stringify({
          activeWorkspaceId,
          activeChatId,
          busyByChat,
          runningChatStartedAtByChat,
          turns,
        })}
      </pre>
    </>
  );
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

  it("closes the previous visible round when a new round starts without an explicit round_end", async () => {
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
        event: "assistant",
        data: { text: "first visible reply", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_start",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "tool output", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(1);
      expect(turns[0].rounds).toHaveLength(2);
      expect(turns[0].rounds[0].waitEndedAt).toBeTypeOf("number");
      expect(turns[0].rounds[1].waitEndedAt).toBeNull();
      expect(turns[0].rounds[0].segments.map((segment) => segment.text).join("")).toContain("first visible reply");
      expect(turns[0].rounds[1].segments.map((segment) => segment.text).join("")).toContain("tool output");
    });
  });

  it("splits visible answer text from subsequent tool output without an explicit round_start", async () => {
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
        event: "assistant",
        data: { text: "先给用户一段说明", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "tool output", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(1);
      expect(turns[0].rounds).toHaveLength(2);
      expect(turns[0].rounds[0].waitEndedAt).toBeTypeOf("number");
      expect(turns[0].rounds[1].waitEndedAt).toBeNull();
      expect(turns[0].rounds[0].segments).toEqual([
        expect.objectContaining({ kind: "answer", text: "先给用户一段说明" }),
      ]);
      expect(turns[0].rounds[1].segments).toEqual([
        expect.objectContaining({ kind: "step", text: "tool output" }),
      ]);
    });
  });

  it("merges a deferred tool output block into the already-visible prompt step", async () => {
    render(
      <AppProvider>
        <TurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "Run command", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_start",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\uE004• Ran npx ccusage codex\uE005", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\n\uE000command output\uE001", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(1);
      expect(turns[0].rounds).toHaveLength(1);
      expect(turns[0].rounds[0].segments).toHaveLength(1);
      expect(turns[0].rounds[0].segments[0]).toEqual(
        expect.objectContaining({
          kind: "step",
          text: "\uE004• Ran npx ccusage codex\uE005\n\uE000command output\uE001",
        }),
      );
    });
  });

  it("exposes an optimistic active workspace/chat without mutating the raw backend state", async () => {
    render(
      <AppProvider>
        <DraftCreateProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send draft" }));
    });

    await waitFor(() => {
      const state = JSON.parse(screen.getByTestId("app-state").textContent || "{}") as AppState;
      const view = JSON.parse(screen.getByTestId("active-view").textContent || "{}") as {
        activeWorkspaceId: string;
        activeChatId: string;
        activeChats: AppState["chats"];
        turns: Turn[];
      };
      expect(apiMock.newChat).toHaveBeenCalledWith("ws-2");
      expect(apiMock.sendInput).toHaveBeenCalledWith("hello from draft", true, "chat-2");
      expect(state.workspace.id).toBe("ws-1");
      expect(state.activeChatId).toBe("chat-1");
      expect(view.activeWorkspaceId).toBe("ws-2");
      expect(view.activeChatId).toBe("chat-2");
      expect(view.activeChats[0]?.id).toBe("chat-2");
      expect(view.activeChats[0]?.active).toBe(true);
      expect(view.turns).toHaveLength(1);
    });
  });

  it("keeps the optimistic transcript focused when the new workspace chat id matches the current one", async () => {
    apiMock.newChat.mockResolvedValueOnce("chat-1");

    render(
      <AppProvider>
        <DraftCreateProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send draft" }));
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("active-view").textContent || "{}") as {
        activeWorkspaceId: string;
        activeChatId: string;
        turns: Turn[];
      };
      expect(view.activeWorkspaceId).toBe("ws-2");
      expect(view.activeChatId).toBe("chat-1");
      expect(view.turns).toHaveLength(1);
      expect(view.turns[0]?.userText).toContain("hello from draft");
    });
  });

  it("materializes the target workspace chat before uploading a pasted image in draft mode", async () => {
    render(
      <AppProvider>
        <DraftCreateProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "paste draft image" }));
    });

    await waitFor(() => {
      expect(apiMock.newChat).toHaveBeenCalledWith("ws-2");
      expect(apiMock.pasteImage).toHaveBeenCalledWith(
        "chat-2",
        "data:image/png;base64,AAAA",
        "ws-2",
      );
    });
  });

  it("defers model change to materialization when switching model in draft mode", async () => {
    render(
      <AppProvider>
        <DraftCreateProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    // Select model in draft mode — should NOT materialize the chat yet.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "select draft model" }));
    });

    await waitFor(() => {
      const state = JSON.parse(screen.getByTestId("app-state").textContent || "{}") as AppState;
      expect(apiMock.newChat).not.toHaveBeenCalled();
      expect(apiMock.sendInput).not.toHaveBeenCalled();
      // Local state is updated optimistically.
      expect(state.model.current).toBe("openai/family/model/v2");
    });
  });

  it("accepts the target workspace idle snapshot after draft creation in another workspace", async () => {
    render(
      <AppProvider>
        <DraftCreateProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send draft" }));
    });

    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-2",
          workspaceId: "ws-2",
          state: buildState({
            workspace: {
              id: "ws-2",
              name: "Workspace B",
              root: "D:/workspace-b",
              workDirectory: "D:/workspace-b",
            },
            workspaces: [{
              id: "ws-1",
              name: "Workspace",
              root: "D:/workspace",
              active: false,
              isDefault: false,
            }, {
              id: "ws-2",
              name: "Workspace B",
              root: "D:/workspace-b",
              active: true,
              isDefault: false,
            }],
            chats: [{
              index: 0,
              id: "chat-2",
              name: "Updated Title",
              messageCount: 2,
              active: true,
              running: false,
              archived: false,
              planMode: false,
              model: "provider/model",
            }],
            activeChatId: "chat-2",
          }),
        },
      });
    });

    await waitFor(() => {
      const state = JSON.parse(screen.getByTestId("app-state").textContent || "{}") as AppState;
      const view = JSON.parse(screen.getByTestId("active-view").textContent || "{}") as {
        activeWorkspaceId: string;
        activeChatId: string;
        activeChats: AppState["chats"];
      };
      expect(state.workspace.id).toBe("ws-2");
      expect(state.activeChatId).toBe("chat-2");
      expect(view.activeWorkspaceId).toBe("ws-2");
      expect(view.activeChatId).toBe("chat-2");
      expect(view.activeChats[0]?.name).toBe("Updated Title");
    });
  });

  it("reloads history when switching to a same-id chat in another workspace", async () => {
    render(
      <AppProvider>
        <HistoryReloadProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());
    await waitFor(() => expect(apiMock.getChatHistory).toHaveBeenCalledTimes(1));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "switch workspace" }));
    });

    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-2",
          state: buildState({
            workspace: {
              id: "ws-2",
              name: "Workspace B",
              root: "D:/workspace-b",
              workDirectory: "D:/workspace-b",
            },
            workspaces: [{
              id: "ws-1",
              name: "Workspace",
              root: "D:/workspace",
              active: false,
              isDefault: false,
            }, {
              id: "ws-2",
              name: "Workspace B",
              root: "D:/workspace-b",
              active: true,
              isDefault: false,
            }],
            chats: [{
              index: 0,
              id: "chat-1",
              name: "Workspace B Chat 1",
              messageCount: 3,
              active: true,
              running: false,
              archived: false,
              planMode: false,
              model: "provider/model",
            }],
            activeChatId: "chat-1",
          }),
        },
      });
    });

    await waitFor(() => {
      expect(apiMock.getChatHistory).toHaveBeenCalledTimes(2);
    });
  });

  it("preserves the running chat timer source and optimistic selection while switching away and back", async () => {
    apiMock.getState.mockResolvedValue(
      buildState({
        chats: [{
          index: 0,
          id: "chat-1",
          name: "Chat 1",
          messageCount: 1,
          active: true,
          running: true,
          archived: false,
          planMode: false,
          model: "provider/model",
        }, {
          index: 1,
          id: "chat-2",
          name: "Chat 2",
          messageCount: 0,
          active: false,
          running: false,
          archived: false,
          planMode: false,
          model: "provider/model",
        }],
      }),
    );

    render(
      <AppProvider>
        <BusySwitchProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "Working", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "switch to chat 2" }));
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("busy-switch-view").textContent || "{}") as {
        activeWorkspaceId: string;
        activeChatId: string;
        busyByChat: Record<string, boolean>;
        runningChatStartedAtByChat: Record<string, number>;
      };
      expect(view.activeWorkspaceId).toBe("ws-1");
      expect(view.activeChatId).toBe("chat-2");
      expect(view.busyByChat["ws-1\u0000chat-1"]).toBe(true);
      expect(view.runningChatStartedAtByChat["ws-1\u0000chat-1"]).toBeTypeOf("number");
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "switch back to chat 1" }));
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("busy-switch-view").textContent || "{}") as {
        activeWorkspaceId: string;
        activeChatId: string;
        turns: Turn[];
      };
      expect(view.activeWorkspaceId).toBe("ws-1");
      expect(view.activeChatId).toBe("chat-1");
      expect(view.turns).toHaveLength(1);
      expect(view.turns[0]?.endedAt).toBeNull();
    });
  });
});
