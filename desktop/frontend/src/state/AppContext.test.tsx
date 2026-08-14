import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppState, ServerEvent, Turn } from "../api/types";
import { subsumedHistoryStartIndex } from "./AppContext";

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
  const saveDraftAttachment = vi.fn(async () => ({ path: "D:/workspace-b/.codewood/cache/draft-attachments/img.png", name: "img.png" }));
  const materializeDraftAttachments = vi.fn(async () => ({}));
  const selectChat = vi.fn(async () => true);
  const sendInput = vi.fn(async () => undefined);
  const setChatModel = vi.fn(async () => true);
  const setChatReasoning = vi.fn(async () => true);
  const savePendingInputs = vi.fn(async () => true);
  const compactContext = vi.fn(async () => ({ ok: true }));
  const pause = vi.fn(async () => undefined);
  const syncModelPresets = vi.fn(async () => undefined);
  const deleteChat = vi.fn(async () => true);
  const deleteWorkspace = vi.fn(async () => ({ ok: true, id: "", wasActive: false, fallbackId: "" }));
  const forkChat = vi.fn(async () => "chat-3");
  const editChat = vi.fn(async () => true);
  const setExecutionPolicy = vi.fn(async () => true);
  const createWorkspace = vi.fn(async () => ({ ok: true, id: "ws-3" }));
  return {
    getState,
    connectEvents,
    getChatHistory,
    listWorkspaceChats,
    newChat,
    pasteImage,
    saveDraftAttachment,
    materializeDraftAttachments,
    selectChat,
    sendInput,
    setChatModel,
    setChatReasoning,
    savePendingInputs,
    compactContext,
    pause,
    syncModelPresets,
    deleteChat,
    deleteWorkspace,
    forkChat,
    editChat,
    setExecutionPolicy,
    createWorkspace,
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
      saveDraftAttachment.mockClear();
      materializeDraftAttachments.mockClear();
      selectChat.mockClear();
      sendInput.mockClear();
      setChatModel.mockClear();
      setChatReasoning.mockClear();
      savePendingInputs.mockClear();
      compactContext.mockClear();
      pause.mockClear();
      syncModelPresets.mockClear();
      deleteChat.mockClear();
      deleteWorkspace.mockClear();
      forkChat.mockClear();
      editChat.mockClear();
      setExecutionPolicy.mockClear();
      createWorkspace.mockClear();
    },
  };
});

vi.mock("../api/client", () => ({
  ApiClient: class {
    port = "";
    token = "";
    constructor(port?: string, token?: string) {
      if (port != null && token != null) {
        this.port = String(port);
        this.token = token;
      }
    }
    getState = apiMock.getState;
    connectEvents = apiMock.connectEvents;
    getChatHistory = apiMock.getChatHistory;
    listWorkspaceChats = apiMock.listWorkspaceChats;
    newChat = apiMock.newChat;
    pasteImage = apiMock.pasteImage;
    saveDraftAttachment = apiMock.saveDraftAttachment;
    materializeDraftAttachments = apiMock.materializeDraftAttachments;
    selectChat = apiMock.selectChat;
    sendInput = apiMock.sendInput;
    setChatModel = apiMock.setChatModel;
    setChatReasoning = apiMock.setChatReasoning;
    savePendingInputs = apiMock.savePendingInputs;
    compactContext = apiMock.compactContext;
    pause = apiMock.pause;
    syncModelPresets = apiMock.syncModelPresets;
    deleteChat = apiMock.deleteChat;
    deleteWorkspace = apiMock.deleteWorkspace;
    forkChat = apiMock.forkChat;
    editChat = apiMock.editChat;
    setExecutionPolicy = apiMock.setExecutionPolicy;
    createWorkspace = apiMock.createWorkspace;
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

function HistoryTurnsProbe() {
  const { historyTurns, turns } = useApp();
  return (
    <pre data-testid="history-and-turns">
      {JSON.stringify({
        history: historyTurns.map((h) => h.userText),
        live: turns.map((t) => ({ userText: t.userText, endedAt: t.endedAt })),
      })}
    </pre>
  );
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

function DraftModelPreserveProbe() {
  const { state, newChat, setModel, setReasoning, setDraftHasContent } =
    useApp();
  return (
    <>
      <button
        onClick={() => {
          void newChat("ws-1");
        }}
      >
        enter draft
      </button>
      <button
        onClick={() => {
          void setModel("openai/family/model/v2");
        }}
      >
        select draft model
      </button>
      <button
        onClick={() => {
          void setReasoning("high");
        }}
      >
        select draft reasoning
      </button>
      <button onClick={() => setDraftHasContent(true)}>mark draft content</button>
      <pre data-testid="draft-model-state">
        {JSON.stringify(state?.model)}
      </pre>
    </>
  );
}

function DraftModelSwitchResetProbe() {
  const { state, newChat, sendInput, setModel, setReasoning } = useApp();
  return (
    <>
      <button
        onClick={() => {
          void newChat("ws-1");
        }}
      >
        enter draft
      </button>
      <button
        onClick={() => {
          void setReasoning("high");
        }}
      >
        select draft reasoning
      </button>
      <button
        onClick={() => {
          void setModel("openai/family/model/v2");
        }}
      >
        select draft model
      </button>
      <button
        onClick={() => {
          void sendInput("hello");
        }}
      >
        send draft
      </button>
      <pre data-testid="draft-reset-state">
        {JSON.stringify(state?.model)}
      </pre>
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

function CreateWorkspaceProbe() {
  const { state, createWorkspace, draftMode, draftWorkspaceId, activeWorkspaceId } = useApp();
  return (
    <>
      <button onClick={() => { void createWorkspace("D:/workspace-c"); }}>
        create workspace
      </button>
      <pre data-testid="create-ws-list">{JSON.stringify(state?.workspaces ?? [])}</pre>
      <pre data-testid="create-ws-draft">{JSON.stringify({ draftMode, draftWorkspaceId, activeWorkspaceId })}</pre>
    </>
  );
}

function UnreadProbe() {
  const { state, unreadChatIds, workspaceChats, switchToChat, refreshWorkspaceChats } = useApp();
  return (
    <>
      <button onClick={() => { void switchToChat("chat-1", "ws-1"); }}>
        open chat 1
      </button>
      <button onClick={() => { void switchToChat("chat-2", "ws-1"); }}>
        open chat 2
      </button>
      <button onClick={() => { void switchToChat("chat-1", "ws-2"); }}>
        open ws2 chat
      </button>
      <button onClick={() => { void refreshWorkspaceChats("ws-1"); }}>
        refresh ws-1
      </button>
      <pre data-testid="unread-view">{JSON.stringify(unreadChatIds)}</pre>
      <pre data-testid="unread-state">{JSON.stringify(state?.chats)}</pre>
      <pre data-testid="unread-wschats">{JSON.stringify(workspaceChats)}</pre>
      <pre data-testid="unread-active">{JSON.stringify(state?.workspace?.id)}</pre>
    </>
  );
}

function DeleteChatProbe() {
  const { state, activeChats, deleteChat } = useApp();
  return (
    <>
      <button onClick={() => { void deleteChat("chat-2", "ws-1"); }}>
        delete chat 2
      </button>
      <button onClick={() => { void deleteChat("chat-1", "ws-1"); }}>
        delete active chat
      </button>
      <pre data-testid="delete-state">{JSON.stringify(state)}</pre>
      <pre data-testid="delete-chats">{JSON.stringify(activeChats)}</pre>
    </>
  );
}

function DeleteWorkspaceProbe() {
  const { state, deleteWorkspace } = useApp();
  return (
    <>
      <button onClick={() => { void deleteWorkspace("ws-2"); }}>
        delete ws-2
      </button>
      <button onClick={() => { void deleteWorkspace("ws-1"); }}>
        delete active workspace
      </button>
      <pre data-testid="delete-ws-state">{JSON.stringify(state)}</pre>
    </>
  );
}

function BusySwitchProbe() {
  const {
    state,
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
          modelCurrent: state?.model.current,
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

function ImmediateSwitchProbe() {
  const {
    activeWorkspaceId,
    activeChatId,
    activeChats,
    historyTurns,
    historyLoading,
    switchToChat,
  } = useApp();
  return (
    <>
      <button onClick={() => { void switchToChat("chat-2", "ws-1"); }}>
        switch immediately
      </button>
      <pre data-testid="immediate-switch-view">
        {JSON.stringify({
          activeWorkspaceId,
          activeChatId,
          activeChats,
          historyTurns,
          historyLoading,
        })}
      </pre>
    </>
  );
}

function EditThenModelProbe() {
  const { state, editChat, setModel } = useApp();
  return (
    <>
      <button onClick={() => { void editChat(-1); }}>
        edit last
      </button>
      <button onClick={() => { void setModel("openai/family/model/v2"); }}>
        set model v2
      </button>
      <pre data-testid="edit-model-state">{JSON.stringify(state)}</pre>
    </>
  );
}

function EditHistoryProbe() {
  const { editChat, historyTurns, historyTotal } = useApp();
  return (
    <>
      <button onClick={() => { void editChat(-1); }}>
        edit last
      </button>
      <pre data-testid="edit-history-view">{JSON.stringify({ historyTurns, historyTotal })}</pre>
    </>
  );
}

function ModelThenSendProbe() {
  const { setModel, sendInput } = useApp();
  return (
    <>
      <button onClick={() => { void setModel("openai/family/model/v2"); }}>
        set model v2
      </button>
      <button onClick={() => { void sendInput("hello after switch"); }}>
        send message
      </button>
    </>
  );
}

function HealthSendProbe() {
  const { sendInput } = useApp();
  return (
    <>
      <button onClick={() => { void sendInput("/server-health"); }}>
        send health
      </button>
      <button onClick={() => { void sendInput("normal message"); }}>
        send normal
      </button>
    </>
  );
}

function PendingJumpProbe() {
  const { pendingInputs, pendingAutoSend, sendInput, sendInputSteer, sendPendingInputNow, startPendingInputs, reorderPendingInput } = useApp();
  return (
    <>
      <button onClick={() => { void sendInput("msg-A"); }}>queue A</button>
      <button onClick={() => { void sendInput("msg-B"); }}>queue B</button>
      <button onClick={() => { void sendInput("msg-C"); }}>queue C</button>
      <button onClick={() => { void sendInputSteer("steer-msg"); }}>steer now</button>
      <button onClick={() => { reorderPendingInput(0, 2); }}>reorder 0 to 2</button>
      <button onClick={() => { reorderPendingInput(1, 3); }}>reorder 1 to end</button>
      <button onClick={() => { void sendPendingInputNow(0); }}>jump index 0</button>
      <button onClick={() => { void sendPendingInputNow(1); }}>jump index 1</button>
      <button onClick={() => { void startPendingInputs(); }}>start queue</button>
      <pre data-testid="pending-state">
        {JSON.stringify({ pendingInputs, pendingAutoSend })}
      </pre>
    </>
  );
}

function StreamingStateMergeProbe() {
  const { state } = useApp();
  return (
    <pre data-testid="stream-state">
      {JSON.stringify({
        model: state?.model?.current,
        reasoningEffort: state?.model?.reasoningEffort,
        cacheStats: state?.cacheStats,
        tokenStats: state?.tokenStats,
      })}
    </pre>
  );
}

function StateChatsProbe() {
  const { state } = useApp();
  return <pre data-testid="state-chats">{JSON.stringify(state?.chats)}</pre>;
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

  it("settles the tool round as soon as its cmd-output block closes", async () => {
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
        data: { text: "\uE004• Ran git status\uE005", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\n\uE000modified: file.py", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    // While the output block is still open the round must stay running so
    // the tool spinner keeps spinning during command execution.
    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns[0].rounds).toHaveLength(1);
      expect(turns[0].rounds[0].waitEndedAt).toBeNull();
    });

    // The closing chunk settles the round immediately — no round_end needed,
    // so the GUI can flip to the "Working..." wait indicator right away.
    act(() => {
      apiMock.emit({
        event: "output",
        data: { text: "\n\uE001", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns[0].rounds).toHaveLength(1);
      expect(turns[0].rounds[0].waitEndedAt).toBeTypeOf("number");
    });
  });

  it("keeps command-output continuation chunks in the round with the open cmd block", async () => {
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
        data: { text: "\uE004• Ran run_all_tests.py\uE005", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\n\uE000Framework: pytest", chatId: "chat-1", workspaceId: "ws-1" },
      });
      // The model round closes while the command is still streaming.
      apiMock.emit({
        event: "round_end",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\n[gw15] [ 66%] PASSED test_one", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\n[gw15] [100%] PASSED test_two\uE001", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(1);
      expect(turns[0].rounds).toHaveLength(1);
      const steps = turns[0].rounds[0].segments.filter((s) => s.kind === "step");
      expect(steps).toHaveLength(1);
      expect(steps[0]?.text).toBe(
        "\uE004• Ran run_all_tests.py\uE005\n\uE000Framework: pytest\n[gw15] [ 66%] PASSED test_one\n[gw15] [100%] PASSED test_two\uE001",
      );
    });
  });

  it("folds late cmd-output chunks into the previous open block across a new round_start", async () => {
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
        data: { text: "\uE004• Ran run_all_tests.py\uE005", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\n\uE000Framework: pytest", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_end",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      // A new model round opens while the command output is still draining.
      apiMock.emit({
        event: "round_start",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\n[gw15] PASSED\uE001", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(1);
      expect(turns[0].rounds).toHaveLength(1);
      const steps = turns[0].rounds[0].segments.filter((s) => s.kind === "step");
      expect(steps).toHaveLength(1);
      expect(steps[0]?.text).toBe(
        "\uE004• Ran run_all_tests.py\uE005\n\uE000Framework: pytest\n[gw15] PASSED\uE001",
      );
    });
  });

  it("applies a failed tool prompt repaint while the turn is still running", async () => {
    render(
      <AppProvider>
        <TurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "Run read", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_start",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: {
          text: "\uE004\x1b[38;2;19;161;14m•\x1b[0m Read helloworld2.py\uE005",
          chatId: "chat-1",
          workspaceId: "ws-1",
        },
      });
      apiMock.emit({
        event: "tool_feedback_repaint",
        data: {
          text: "\uE004\x1b[38;2;197;15;31m•\x1b[0m Read helloworld2.py\uE005",
          chatId: "chat-1",
          workspaceId: "ws-1",
        },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(1);
      expect(turns[0].rounds).toHaveLength(1);
      expect(turns[0].rounds[0].segments).toHaveLength(1);
      expect(turns[0].rounds[0].segments[0]?.text).toContain("\x1b[38;2;197;15;31m");
      expect(turns[0].rounds[0].segments[0]?.text).not.toContain("\x1b[38;2;19;161;14m");
    });
  });

  it("repaints the running explore step into the completed row in place", async () => {
    render(
      <AppProvider>
        <TurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "Analyze sub-agent architecture", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_start",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: {
          text: "\uE004• Exploring sub-agent architecture...\uE005",
          chatId: "chat-1",
          workspaceId: "ws-1",
        },
      });
      apiMock.emit({
        event: "tool_feedback_repaint",
        data: {
          text: "\uE004• Explored sub-agent architecture for 41.3s\uE005",
          chatId: "chat-1",
          workspaceId: "ws-1",
        },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(1);
      expect(turns[0].rounds).toHaveLength(1);
      expect(turns[0].rounds[0].segments).toHaveLength(1);
      const text = turns[0].rounds[0].segments[0]?.text || "";
      expect(text).toContain("Explored sub-agent architecture for 41.3s");
      expect(text).not.toContain("Exploring");
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
      expect(apiMock.newChat).toHaveBeenCalledWith("ws-2", "", "");
      expect(apiMock.sendInput).toHaveBeenCalledWith("hello from draft", true, "chat-2", "ws-2");
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

  it("stages a pasted image in the workspace cache without materializing the chat in draft mode", async () => {
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
      expect(apiMock.newChat).not.toHaveBeenCalled();
      expect(apiMock.pasteImage).not.toHaveBeenCalled();
      expect(apiMock.saveDraftAttachment).toHaveBeenCalledWith(
        "data:image/png;base64,AAAA",
        "",
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

  it("resets the reasoning effort when switching models in draft mode", async () => {
    apiMock.getState.mockResolvedValue(
      buildState({
        model: {
          current: "provider/model",
          available: ["provider/model", "openai/family/model/v2"],
          ready: true,
          reasoningEffort: "high",
          reasoningEfforts: ["low", "high"],
          reasoningEffortsBySelector: {
            "provider/model": ["low", "high"],
            "openai/family/model/v2": ["low", "high"],
          },
        },
      }),
    );

    render(
      <AppProvider>
        <DraftModelSwitchResetProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    // Pick a reasoning effort for the draft, then switch the model. The
    // effort belonged to the previous model, so it must be discarded and
    // never leak into the chat created on send.
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "select draft reasoning" }),
      );
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "select draft model" }));
    });

    await waitFor(() => {
      const model = JSON.parse(
        screen.getByTestId("draft-reset-state").textContent || "{}",
      ) as AppState["model"];
      expect(model.current).toBe("openai/family/model/v2");
      // Switching models discards the previous effort choice.
      expect(model.reasoningEffort).toBe("");
      // The new model's supported levels are applied.
      expect(model.reasoningEfforts).toEqual(["low", "high"]);
    });

    // Send the draft: the new chat is created with the model but no reasoning
    // effort, matching what the composer showed.
    apiMock.newChat.mockResolvedValueOnce("chat-2");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send draft" }));
    });

    expect(apiMock.newChat).toHaveBeenCalledWith("", "openai/family/model/v2", "");
  });

  it("keeps the draft model/reasoning when re-entering a draft that still has content", async () => {
    render(
      <AppProvider>
        <DraftModelPreserveProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    // Choose a model + reasoning effort for the draft (not sent yet).
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "select draft model" }));
    });
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "select draft reasoning" }),
      );
    });
    // The user typed content into the draft composer (reported by ChatView).
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "mark draft content" }));
    });

    // Simulate the backend applying the model of the chat the user switched to
    // (its idle/state snapshot overwrites the optimistic draft model display).
    act(() => {
      apiMock.emit({
        event: "state",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            model: {
              current: "provider/model",
              available: ["provider/model", "openai/family/model/v2"],
              ready: true,
              reasoningEffort: "",
              reasoningEfforts: [],
            },
          }),
        },
      });
    });

    // Click New Chat again to return to the existing draft.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    await waitFor(() => {
      const model = JSON.parse(
        screen.getByTestId("draft-model-state").textContent || "{}",
      ) as { current: string; reasoningEffort: string };
      expect(model.current).toBe("openai/family/model/v2");
      expect(model.reasoningEffort).toBe("high");
    });
  });

  it("resets the draft model/reasoning for a fresh draft with no content", async () => {
    render(
      <AppProvider>
        <DraftModelPreserveProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    // Pick a model but never type anything.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "select draft model" }));
    });

    act(() => {
      apiMock.emit({
        event: "state",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            model: {
              current: "provider/model",
              available: ["provider/model", "openai/family/model/v2"],
              ready: true,
              reasoningEffort: "",
              reasoningEfforts: [],
            },
          }),
        },
      });
    });

    // A fresh New Chat with no draft content resets to inherit the last chat.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    await waitFor(() => {
      const model = JSON.parse(
        screen.getByTestId("draft-model-state").textContent || "{}",
      ) as { current: string; reasoningEffort: string };
      expect(model.current).toBe("provider/model");
    });
  });

  it("shows the target workspace's inherited model/reasoning for a fresh draft in another workspace", async () => {
    // Workspace B's latest chat uses a different model + reasoning than the
    // focused workspace A's chat-1 ("provider/model"). The backend's new_chat
    // inherits from the target workspace AFTER switching, so the composer must
    // display B's model — not A's — while composing (regression: it showed
    // chat A's model but the sent chat used workspace B's).
    apiMock.listWorkspaceChats.mockResolvedValue([
      {
        id: "chat-9",
        name: "B chat",
        updatedAt: "2026-01-02T00:00:00Z",
        model: "openai/family/model/v2",
        reasoning: "high",
      },
    ]);

    render(
      <AppProvider>
        <DraftCreateProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "enter draft" }));
    });

    await waitFor(() => {
      const state = JSON.parse(
        screen.getByTestId("app-state").textContent || "{}",
      ) as AppState;
      expect(state.model.current).toBe("openai/family/model/v2");
      expect(state.model.reasoningEffort).toBe("high");
    });

    // Materialization still passes empty selections so the backend computes
    // the same inheritance atomically (no behavior change on send).
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send draft" }));
    });
    expect(apiMock.newChat).toHaveBeenCalledWith("ws-2", "", "");
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

  it("does not merge a background workspace's mismatched idle snapshot by bare chat id", async () => {
    apiMock.getState.mockResolvedValue(buildState({
      workspace: {
        id: "ws-2",
        name: "Workspace B",
        root: "D:/workspace-b",
        workDirectory: "D:/workspace-b",
      },
      chats: [{
        index: 0,
        id: "chat-1",
        name: "Chat B",
        messageCount: 1,
        active: true,
        running: false,
        archived: false,
        planMode: false,
        model: "provider/model",
      }],
    }));
    render(
      <AppProvider>
        <StateChatsProbe />
      </AppProvider>,
    );
    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByTestId("state-chats").textContent).toContain("Chat B");
    });

    // The envelope correctly says this is workspace A, but a racy backend
    // snapshot has B's workspace metadata and A's chat record.  Matching on
    // only ``chat-1`` would rename the B entry to "Chat A".
    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            workspace: {
              id: "ws-2",
              name: "Workspace B",
              root: "D:/workspace-b",
              workDirectory: "D:/workspace-b",
            },
            chats: [{
              index: 0,
              id: "chat-1",
              name: "Chat A",
              messageCount: 2,
              active: true,
              running: true,
              archived: false,
              planMode: false,
              model: "provider/model",
            }],
          }),
        },
      });
    });

    await waitFor(() => {
      expect(screen.getByTestId("state-chats").textContent).toContain("Chat B");
      expect(screen.getByTestId("state-chats").textContent).not.toContain("Chat A");
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
          model: "provider/model-a",
        }, {
          index: 1,
          id: "chat-2",
          name: "Chat 2",
          messageCount: 0,
          active: false,
          running: false,
          archived: false,
          planMode: false,
          model: "provider/model-b",
        }],
        model: {
          current: "provider/model-a",
          available: ["provider/model-a", "provider/model-b"],
          ready: true,
          reasoningEffort: "",
          reasoningEfforts: [],
        },
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

    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-2",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{
              index: 0,
              id: "chat-1",
              name: "Chat 1",
              messageCount: 1,
              active: false,
              running: true,
              archived: false,
              planMode: false,
              model: "provider/model-a",
            }, {
              index: 1,
              id: "chat-2",
              name: "Chat 2",
              messageCount: 0,
              active: true,
              running: false,
              archived: false,
              planMode: false,
              model: "provider/model-b",
            }],
            activeChatId: "chat-2",
            model: {
              current: "provider/model-b",
              available: ["provider/model-a", "provider/model-b"],
              ready: true,
              reasoningEffort: "",
              reasoningEfforts: [],
            },
          }),
        },
      });
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("busy-switch-view").textContent || "{}") as {
        activeWorkspaceId: string;
        activeChatId: string;
        busyByChat: Record<string, boolean>;
        runningChatStartedAtByChat: Record<string, number>;
        modelCurrent: string;
      };
      expect(view.activeWorkspaceId).toBe("ws-1");
      expect(view.activeChatId).toBe("chat-2");
      expect(view.modelCurrent).toBe("provider/model-b");
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
        modelCurrent: string;
        turns: Turn[];
      };
      expect(view.activeWorkspaceId).toBe("ws-1");
      expect(view.activeChatId).toBe("chat-1");
      expect(view.modelCurrent).toBe("provider/model-a");
      expect(view.turns).toHaveLength(1);
      expect(view.turns[0]?.endedAt).toBeNull();
    });
  });

  it("switches to the target chat immediately and clears transcript before history resolves", async () => {
    let resolveSelectChat: ((value: boolean) => void) | null = null;
    apiMock.selectChat.mockImplementationOnce(
      () =>
        new Promise<boolean>((resolve) => {
          resolveSelectChat = resolve;
        }),
    );
    apiMock.getState.mockResolvedValue(
      buildState({
        chats: [
          {
            index: 0,
            id: "chat-1",
            name: "Chat 1",
            messageCount: 1,
            active: true,
            running: false,
            archived: false,
            planMode: false,
            model: "provider/model",
          },
          {
            index: 1,
            id: "chat-2",
            name: "Chat 2",
            messageCount: 2,
            active: false,
            running: false,
            archived: false,
            planMode: false,
            model: "provider/model",
          },
        ],
      }),
    );
    apiMock.getChatHistory.mockResolvedValue({
      turns: [
        {
          userText: "hello",
          rounds: [],
          startedAt: 1,
          endedAt: 2,
        },
      ],
      start: 0,
      total: 1,
    });

    render(
      <AppProvider>
        <ImmediateSwitchProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "switch immediately" }));
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("immediate-switch-view").textContent || "{}") as {
        activeWorkspaceId: string;
        activeChatId: string;
        activeChats: AppState["chats"];
        historyTurns: unknown[];
        historyLoading: boolean;
      };
      const activeChat = view.activeChats.find((chat) => chat.id === "chat-2");
      expect(view.activeWorkspaceId).toBe("ws-1");
      expect(view.activeChatId).toBe("chat-2");
      expect(activeChat?.active).toBe(true);
      expect(activeChat?.name).toBe("Chat 2");
      expect(view.historyTurns).toEqual([]);
      expect(view.historyLoading).toBe(true);
    });

    await act(async () => {
      resolveSelectChat?.(true);
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("immediate-switch-view").textContent || "{}") as {
        historyTurns: Array<{ userText?: string }>;
        historyLoading: boolean;
      };
      expect(view.historyLoading).toBe(false);
      expect(view.historyTurns[0]?.userText).toBe("hello");
    });
  });

  it("keeps the newly selected model when edit clears the chat and a stale state snapshot arrives", async () => {
    render(
      <AppProvider>
        <EditThenModelProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "edit last" }));
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "set model v2" }));
    });

    act(() => {
      apiMock.emit({
        event: "state",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            model: {
              current: "provider/model",
              available: ["provider/model", "openai/family/model/v2"],
              ready: true,
              reasoningEffort: "",
              reasoningEfforts: [],
            },
          }),
        },
      });
    });

    await waitFor(() => {
      const state = JSON.parse(screen.getByTestId("edit-model-state").textContent || "{}") as AppState;
      expect(apiMock.editChat).toHaveBeenCalledWith("chat-1", "ws-1", -1);
      expect(apiMock.setChatModel).toHaveBeenCalledWith("chat-1", "openai/family/model/v2", "ws-1");
      expect(state.model.current).toBe("openai/family/model/v2");
    });
  });

  it("optimistically trims history turns as soon as edit starts", async () => {
    let resolveEdit: (() => void) | null = null;
    apiMock.getChatHistory.mockResolvedValue({
      turns: [
        { userText: "first", rounds: [] },
        { userText: "second", rounds: [] },
      ],
      start: 0,
      total: 2,
    });
    apiMock.editChat.mockImplementation(() => {
      return new Promise<boolean>((resolve) => {
        resolveEdit = () => resolve(true);
      });
    });

    render(
      <AppProvider>
        <EditHistoryProbe />
      </AppProvider>,
    );

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("edit-history-view").textContent || "{}") as {
        historyTurns: Array<{ userText?: string }>;
        historyTotal: number;
      };
      expect(view.historyTurns).toHaveLength(2);
      expect(view.historyTotal).toBe(2);
    });

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "edit last" }));
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("edit-history-view").textContent || "{}") as {
        historyTurns: Array<{ userText?: string }>;
        historyTotal: number;
      };
      expect(view.historyTurns).toHaveLength(1);
      expect(view.historyTurns[0]?.userText).toBe("first");
      expect(view.historyTotal).toBe(1);
    });

    await act(async () => {
      resolveEdit?.();
      await Promise.resolve();
    });
  });

  it("waits for a pending model switch before sending the next message", async () => {
    let resolveModelSwitch: (() => void) | null = null;
    apiMock.setChatModel.mockImplementation((chatId: string, model: string) => {
      if (chatId === "chat-1" && model === "openai/family/model/v2") {
        return new Promise<boolean>((resolve) => {
          resolveModelSwitch = () => resolve(true);
        });
      }
      return Promise.resolve(true);
    });

    render(
      <AppProvider>
        <ModelThenSendProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "set model v2" }));
      fireEvent.click(screen.getByRole("button", { name: "send message" }));
    });

    await waitFor(() => {
      expect(apiMock.setChatModel).toHaveBeenCalledWith("chat-1", "openai/family/model/v2", "ws-1");
    });
    expect(apiMock.sendInput).toHaveBeenCalledTimes(0);

    await act(async () => {
      resolveModelSwitch?.();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(apiMock.sendInput).toHaveBeenNthCalledWith(1, "hello after switch", true, "chat-1", "ws-1");
    });
  });

  it("jump-sends one message and defers draining until the jumped task finishes", async () => {
    render(
      <AppProvider>
        <PendingJumpProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // The active chat is busy (a turn is streaming): messages queue up.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "long running task", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "queue A" }));
      fireEvent.click(screen.getByRole("button", { name: "queue B" }));
      fireEvent.click(screen.getByRole("button", { name: "queue C" }));
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-A", "msg-B", "msg-C"]);
    });

    // Jump-send the middle message.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "jump index 1" }));
    });

    expect(apiMock.pause).toHaveBeenCalledWith("chat-1", "ws-1");
    expect(apiMock.sendInput).toHaveBeenCalledWith("msg-B", true, "chat-1", "ws-1");
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-A", "msg-C"]);
      expect(st.pendingAutoSend).toBe(true);
    });

    // The paused turn's idle must NOT drain the queue: the remaining messages
    // wait for the jumped task to finish.
    await act(async () => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-A", "msg-C"]);
    });
    expect(apiMock.sendInput).not.toHaveBeenCalledWith("msg-A", true, "chat-1", "ws-1");

    // The jumped message B runs...
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "msg-B", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    // ...and when B finishes, auto-send resumes and dequeues exactly one.
    await act(async () => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });
    await waitFor(() => {
      expect(apiMock.sendInput).toHaveBeenCalledWith("msg-A", true, "chat-1", "ws-1");
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-C"]);
    });
  });

  it("does not double-send a jumped message when the previous idle already scheduled auto-send", async () => {
    render(
      <AppProvider>
        <PendingJumpProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // A turn is streaming: messages queue up.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "long running task", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "queue B" }));
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-B"]);
    });

    // The turn finishes BEFORE the user clicks Steer: the idle handler clears
    // busy and schedules the 200ms auto-send of the queue head.
    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });

    // The user clicks Steer while the auto-send timer is still pending, and
    // the pause round-trip is slow enough that the timer fires first. The jump
    // must win: msg-B is sent exactly once and the queue is not drained ahead
    // of the jumped message.
    apiMock.pause.mockImplementationOnce(
      () => new Promise((resolve) => setTimeout(resolve, 300)),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "jump index 0" }));
    });

    // Let both the 200ms auto-send timer and the slow pause complete.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 400));
    });

    expect(apiMock.pause).toHaveBeenCalledWith("chat-1", "ws-1");
    expect(apiMock.sendInput).toHaveBeenCalledTimes(1);
    expect(apiMock.sendInput).toHaveBeenCalledWith("msg-B", true, "chat-1", "ws-1");
    const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
    expect(st.pendingInputs).toEqual([]);
    expect(st.pendingAutoSend).toBe(false);
  });

  it("restores a jumped message when the jump send fails", async () => {
    render(
      <AppProvider>
        <PendingJumpProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "long running task", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "queue B" }));
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-B"]);
    });

    // The send fails after the message was removed from the queue: it must be
    // restored at the head instead of silently disappearing.
    apiMock.pause.mockImplementationOnce(async () => undefined);
    apiMock.sendInput.mockImplementationOnce(async () => {
      throw new Error("network down");
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "jump index 0" }));
    });

    const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
    expect(st.pendingInputs).toEqual(["msg-B"]);
    expect(st.pendingAutoSend).toBe(true);
  });

  it("steers a typed message immediately while busy, bypassing the queue", async () => {
    render(
      <AppProvider>
        <PendingJumpProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // A turn is streaming: the composer Steer must NOT queue the message.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "long running task", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "steer now" }));
    });

    expect(apiMock.pause).toHaveBeenCalledWith("chat-1", "ws-1");
    expect(apiMock.sendInput).toHaveBeenCalledWith("steer-msg", true, "chat-1", "ws-1");
    const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
    expect(st.pendingInputs).toEqual([]);
    expect(st.pendingAutoSend).toBe(false);
  });

  it("falls back to the pending queue when a steer send fails", async () => {
    render(
      <AppProvider>
        <PendingJumpProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "long running task", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    apiMock.pause.mockImplementationOnce(async () => undefined);
    apiMock.sendInput.mockImplementationOnce(async () => {
      throw new Error("network down");
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "steer now" }));
    });

    // The failed jump must not eat the message: it lands in the pending queue
    // so the next idle still picks it up.
    const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
    expect(st.pendingInputs).toEqual(["steer-msg"]);
    expect(st.pendingAutoSend).toBe(true);
  });

  it("sends normally without pausing when the chat is idle", async () => {
    render(
      <AppProvider>
        <PendingJumpProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "steer now" }));
    });

    expect(apiMock.pause).not.toHaveBeenCalled();
    expect(apiMock.sendInput).toHaveBeenCalledWith("steer-msg", true, "chat-1", "ws-1");
  });

  it("reorders queued messages via drag-to-reorder slots", async () => {
    render(
      <AppProvider>
        <PendingJumpProbe />
      </AppProvider>,
    );
    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());
    act(() => {
      apiMock.emit({ event: "turn_start", data: { text: "t", chatId: "chat-1", workspaceId: "ws-1" } });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "queue A" }));
      fireEvent.click(screen.getByRole("button", { name: "queue B" }));
      fireEvent.click(screen.getByRole("button", { name: "queue C" }));
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-A", "msg-B", "msg-C"]);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "reorder 0 to 2" }));
    });
    let st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
    expect(st.pendingInputs).toEqual(["msg-B", "msg-A", "msg-C"]);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "reorder 1 to end" }));
    });
    st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
    expect(st.pendingInputs).toEqual(["msg-B", "msg-C", "msg-A"]);
    expect(apiMock.savePendingInputs).toHaveBeenCalled();
  });

  it("drains the remaining queue one per turn after the jumped task completes", async () => {
    render(
      <AppProvider>
        <PendingJumpProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "long running task", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "queue A" }));
      fireEvent.click(screen.getByRole("button", { name: "queue B" }));
      fireEvent.click(screen.getByRole("button", { name: "queue C" }));
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-A", "msg-B", "msg-C"]);
    });

    // Jump B: only B leaves the list immediately.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "jump index 1" }));
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-A", "msg-C"]);
      expect(st.pendingAutoSend).toBe(true);
    });

    // Paused turn unwinds: the marker suppresses this one drain.
    await act(async () => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-A", "msg-C"]);
    });

    // Jumped message B runs...
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "msg-B", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    // ...and finishes -> auto-send resumes and dequeues A.
    await act(async () => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });
    await waitFor(() => {
      expect(apiMock.sendInput).toHaveBeenCalledWith("msg-A", true, "chat-1", "ws-1");
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual(["msg-C"]);
    });

    // A runs and finishes -> auto-send sends C; the queue empties and
    // auto-send is cleared.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "msg-A", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await act(async () => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });
    await waitFor(() => {
      expect(apiMock.sendInput).toHaveBeenCalledWith("msg-C", true, "chat-1", "ws-1");
    });
    await waitFor(() => {
      const st = JSON.parse(screen.getByTestId("pending-state").textContent || "{}");
      expect(st.pendingInputs).toEqual([]);
      expect(st.pendingAutoSend).toBe(false);
    });
  });

  it("sends /server-health immediately even while the chat is busy", async () => {
    render(
      <AppProvider>
        <HealthSendProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // Mark the active chat as busy (a turn is streaming).
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "long running task", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    // /server-health bypasses the pending queue and reaches the server at once.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send health" }));
    });
    expect(apiMock.sendInput).toHaveBeenCalledTimes(1);
    expect(apiMock.sendInput).toHaveBeenCalledWith("/server-health", true, "chat-1", "ws-1");

    // A normal message while busy is buffered into the pending queue instead.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "send normal" }));
    });
    expect(apiMock.sendInput).toHaveBeenCalledTimes(1);
  });

  it("removes a deleted chat from the active workspace chat list immediately", async () => {
    apiMock.getState.mockResolvedValue(
      buildState({
        chats: [
          { id: "chat-1", name: "Chat 1", active: true, running: false },
          { id: "chat-2", name: "Chat 2", active: false, running: false },
        ],
      }),
    );
    render(
      <AppProvider>
        <DeleteChatProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "delete chat 2" }));
    });

    expect(apiMock.deleteChat).toHaveBeenCalledWith("chat-2", "ws-1");
    await waitFor(() => {
      const chats = JSON.parse(screen.getByTestId("delete-chats").textContent || "[]") as Array<{ id: string }>;
      expect(chats.map((c) => c.id)).toEqual(["chat-1"]);
    });
  });

  it("removes a deleted workspace immediately while the focused chat is still streaming", async () => {
    render(
      <AppProvider>
        <DeleteWorkspaceProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // The focused chat starts a streaming turn (sets the streaming key).
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "working", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    // A /delete-workspace idle broadcast arrives while that turn is still
    // running: the snapshot's workspace list no longer contains ws-2 and the
    // chat is still marked running, so the still-running merge path runs. It
    // must merge the authoritative workspace list, otherwise the deleted
    // workspace lingers in the sidebar until the turn's terminal idle event.
    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
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
              running: true,
              archived: false,
              planMode: false,
              model: "provider/model",
            }],
          }),
        },
      });
    });

    await waitFor(() => {
      const state = JSON.parse(screen.getByTestId("delete-ws-state").textContent || "{}") as AppState;
      expect(state.workspaces.map((w) => w.id)).toEqual(["ws-1"]);
    });
  });

  it("applies the fallback workspace immediately after deleting the active workspace", async () => {
    apiMock.deleteWorkspace.mockResolvedValueOnce({
      ok: true,
      id: "ws-1",
      wasActive: true,
      fallbackId: "ws-2",
    });
    // First getState feeds the initial render; the second one returns the
    // post-delete fallback state that the delete handler re-fetches because
    // the SSE idle broadcast would be discarded (workspace mismatch).
    apiMock.getState.mockResolvedValueOnce(buildState());
    apiMock.getState.mockResolvedValueOnce(
      buildState({
        workspace: {
          id: "ws-2",
          name: "Workspace B",
          root: "D:/workspace-b",
          workDirectory: "D:/workspace-b",
        },
        workspaces: [{
          id: "ws-2",
          name: "Workspace B",
          root: "D:/workspace-b",
          active: true,
          isDefault: false,
        }],
        chats: [],
        activeChatId: "",
      }),
    );
    render(
      <AppProvider>
        <DeleteWorkspaceProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "delete active workspace" }));
    });

    await waitFor(() => {
      const state = JSON.parse(screen.getByTestId("delete-ws-state").textContent || "{}") as AppState;
      expect(state.workspace.id).toBe("ws-2");
      expect(state.workspaces.map((w) => w.id)).toEqual(["ws-2"]);
    });
    expect(apiMock.getState).toHaveBeenCalledTimes(2);
  });

  it("drops a chat from the sidebar when an idle SSE event omits it (merge path)", async () => {
    apiMock.getState.mockResolvedValue(
      buildState({
        chats: [
          { id: "chat-1", name: "Chat 1", active: true, running: false },
          { id: "chat-2", name: "Chat 2", active: false, running: false },
        ],
      }),
    );
    render(
      <AppProvider>
        <DeleteChatProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // The idle event is for a DIFFERENT chat than the focused one (so the merge
    // path runs) and its authoritative chat list no longer contains chat-2.
    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-3",
          workspaceId: "ws-1",
          state: buildState({
            chats: [
              { id: "chat-1", name: "Chat 1", active: true, running: false },
            ],
          }),
        },
      });
    });

    await waitFor(() => {
      const chats = JSON.parse(screen.getByTestId("delete-chats").textContent || "[]") as Array<{ id: string }>;
      expect(chats.map((c) => c.id)).toEqual(["chat-1"]);
    });
  });

  it("enters draft mode and clears the deleted active chat from the list", async () => {
    apiMock.getState.mockResolvedValue(
      buildState({
        chats: [
          { id: "chat-1", name: "Chat 1", active: true, running: false },
          { id: "chat-2", name: "Chat 2", active: false, running: false },
        ],
      }),
    );
    render(
      <AppProvider>
        <DeleteChatProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "delete active chat" }));
    });

    await waitFor(() => {
      const state = JSON.parse(screen.getByTestId("delete-state").textContent || "{}") as AppState;
      const chats = JSON.parse(screen.getByTestId("delete-chats").textContent || "[]") as Array<{ id: string }>;
      expect(chats.map((c) => c.id)).toEqual(["chat-2"]);
      expect(state.chats.map((c) => c.id)).toEqual(["chat-2"]);
    });
  });

  it("settles and clears the live turn on idle so the finished task collapses into history", async () => {
    // The persisted history now contains the finished turn; the live in-memory
    // turn must be settled (endedAt set) and then cleared so the history view
    // (collapsed "Worked for") takes over instead of the expanded live view.
    apiMock.getChatHistory.mockResolvedValue({
      turns: [
        {
          userText: "查看我的codex用量",
          timestamp: new Date().toISOString(),
          rounds: [
            {
              waitSeconds: 20,
              text: "您的 Codex 总用量约为 8.18 亿 tokens",
              tools: "\uE004• Ran shell npx ccusage codex\uE005",
              thinking: "",
              selection: "",
              compactNoticeTitle: "",
              compactNoticeBody: "",
              interrupted: "",
              modelError: "",
            },
          ],
        },
      ],
      start: 0,
      total: 1,
    });
    render(
      <AppProvider>
        <TurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // Stream a live turn with a tool round.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "查看我的codex用量", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_start",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\uE004• Ran shell npx ccusage codex\uE005", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(1);
    });

    // Task finishes: idle with running=false. The live turn must be settled and
    // cleared (so the collapsed history view renders), not left expanded.
    await act(async () => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
      expect(turns).toHaveLength(0);
    });
  });

  it("keeps the completed task visible when a pending message starts before the history reload lands", async () => {
    // The mount-time history load resolves immediately; the idle-triggered
    // reload is deferred so we can land a NEW turn's ``turn_start`` before its
    // response is applied (the pending-queue auto-send race).
    const deferred: Array<(page: unknown) => void> = [];
    apiMock.getChatHistory
      .mockImplementationOnce(async () => ({ turns: [], start: 0, total: 0 }))
      .mockImplementation(
        () =>
          new Promise((resolve) => {
            deferred.push(resolve);
          }),
      );
    render(
      <AppProvider>
        <HistoryTurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // Task A streams and finishes (idle with running=false), which triggers the
    // deferred history reload.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "task A", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "step A", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("history-and-turns").textContent || "{}") as {
        live: Array<{ userText: string }>;
      };
      expect(view.live).toHaveLength(1);
    });
    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });

    // Pending task B starts streaming BEFORE the reload response arrives. The
    // reload response only carries task A (B's user message is not persisted
    // yet) — task A must stay visible via history, and B keeps streaming live.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "task B", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("history-and-turns").textContent || "{}") as {
        live: Array<{ userText: string }>;
      };
      expect(view.live).toHaveLength(2);
    });

    await act(async () => {
      deferred[0]?.({
        turns: [
          {
            userText: "task A",
            rounds: [{ waitSeconds: 1, text: "answer A", tools: "" }],
          },
        ],
        start: 0,
        total: 1,
      });
      await Promise.resolve();
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("history-and-turns").textContent || "{}") as {
        history: Array<string | undefined>;
        live: Array<{ userText: string }>;
      };
      expect(view.history).toEqual(["task A"]);
      expect(view.live.map((l) => l.userText)).toEqual(["task B"]);
    });
  });

  it("keeps settled live turns when the reload response is empty (persistence race)", async () => {
    const deferred: Array<(page: unknown) => void> = [];
    apiMock.getChatHistory
      .mockImplementationOnce(async () => ({ turns: [], start: 0, total: 0 }))
      .mockImplementation(
        () =>
          new Promise((resolve) => {
            deferred.push(resolve);
          }),
      );
    render(
      <AppProvider>
        <HistoryTurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "task A", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "step A", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("history-and-turns").textContent || "{}") as {
        live: Array<{ userText: string }>;
      };
      expect(view.live).toHaveLength(1);
    });
    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
      apiMock.emit({
        event: "turn_start",
        data: { text: "task B", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    // The reload response is an empty page (task A's disk flush hasn't landed
    // yet). The settled live turn A must NOT be dropped — it stays visible
    // alongside the streaming task B until the next reload reconciles it.
    await act(async () => {
      deferred[0]?.({ turns: [], start: 0, total: 0 });
      await Promise.resolve();
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("history-and-turns").textContent || "{}") as {
        history: Array<string | undefined>;
        live: Array<{ userText: string }>;
      };
      expect(view.history).toEqual([]);
      expect(view.live.map((l) => l.userText)).toEqual(["task A", "task B"]);
    });
  });

  it("drops a settled live turn whose archived copy normalized whitespace (interrupt ordering)", async () => {
    // After an interrupted task, a settled live turn can linger at the tail when
    // the persisted copy normalized its userText (e.g. trailing whitespace), so
    // the exact-match drop missed it and the transcript re-ordered the newest
    // history turns BEFORE the stale live turn. Trimmed matching must drop it.
    apiMock.getChatHistory.mockResolvedValue({
      turns: [
        {
          userText: "上上次消息A",
          timestamp: "2026-08-03 12:00:00",
          rounds: [{ waitSeconds: 1, text: "回复A", tools: "", selection: "", thinking: "", compactNoticeTitle: "", compactNoticeBody: "", interrupted: "", modelError: "" }],
        },
        {
          userText: "上次消息B ",
          timestamp: new Date().toISOString(),
          rounds: [{ waitSeconds: 1, text: "", tools: "", selection: "", thinking: "", compactNoticeTitle: "", compactNoticeBody: "", interrupted: "", modelError: "" }],
        },
        {
          userText: "最新消息C",
          timestamp: "2026-08-03 12:05:00",
          rounds: [{ waitSeconds: 1, text: "回复C", tools: "", selection: "", thinking: "", compactNoticeTitle: "", compactNoticeBody: "", interrupted: "", modelError: "" }],
        },
      ],
      start: 0,
      total: 3,
    });
    render(
      <AppProvider>
        <HistoryTurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // B streams as a live turn, then finishes with an idle event.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "上次消息B", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "step B", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });
    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("history-and-turns").textContent || "{}") as {
        live: Array<{ userText: string }>;
      };
      expect(view.live.map((l) => l.userText)).toEqual(["上次消息B"]);
    });

    await act(async () => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("history-and-turns").textContent || "{}") as {
        history: Array<string | undefined>;
        live: Array<{ userText: string }>;
      };
      expect(view.history).toEqual(["上上次消息A", "上次消息B ", "最新消息C"]);
      expect(view.live.map((l) => l.userText)).toEqual([]);
    });
  });

  it("ignores a duplicate turn_start while the original turn is streaming", async () => {
    render(
      <AppProvider>
        <TurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "run the task", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "first command", chatId: "chat-1", workspaceId: "ws-1" },
      });
      // This can occur when the SSE connection is re-established while the
      // original event subscription is still being torn down.
      apiMock.emit({
        event: "turn_start",
        data: { text: "run the task", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "second command", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    await waitFor(() => {
      const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Array<{
        userText: string;
        rounds: Array<{ segments: Array<{ text: string }> }>;
      }>;
      expect(turns).toHaveLength(1);
      expect(turns[0].userText).toBe("run the task");
      expect(turns[0].rounds[0].segments.map((segment) => segment.text).join("")).toBe(
        "first commandsecond command",
      );
    });
  });

  it("keeps a just-finished repeated prompt when history only has an older copy", async () => {
    apiMock.getChatHistory.mockResolvedValue({
      turns: [{
        userText: "repeat this task",
        timestamp: "2020-01-01 00:00:00",
        rounds: [{ waitSeconds: 1, text: "old answer", tools: "" }],
      }],
      start: 0,
      total: 1,
    });
    render(
      <AppProvider>
        <HistoryTurnsProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "repeat this task", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "new command", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{ id: "chat-1", name: "Chat 1", active: true, running: false }],
          }),
        },
      });
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("history-and-turns").textContent || "{}") as {
        history: Array<string | undefined>;
        live: Array<{ userText: string }>;
      };
      expect(view.history).toEqual(["repeat this task"]);
      expect(view.live.map((turn) => turn.userText)).toEqual(["repeat this task"]);
    });
  });

  it("keeps a background chat's tool description and streaming output in its own bucket when switching back mid-stream", async () => {
    apiMock.getState.mockResolvedValue(
      buildState({
        chats: [
          { id: "chat-1", name: "Chat 1", active: true, running: false },
          { id: "chat-2", name: "Chat 2", active: false, running: false },
        ],
      }),
    );
    render(
      <AppProvider>
        <BusySwitchProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // Chat-1 starts a turn and a shell tool call; the tool description arrives.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "运行 timeout 10", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "round_start",
        data: { chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: {
          text: "\uE004• Ran shell timeout 10\uE005",
          chatId: "chat-1",
          workspaceId: "ws-1",
        },
      });
    });

    // Switch to chat-2 BEFORE the countdown starts streaming.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "switch to chat 2" }));
    });

    // The countdown streams while chat-1 is in the background. It must still
    // accumulate in chat-1's bucket so the tool block stays intact.
    act(() => {
      apiMock.emit({
        event: "output",
        data: { text: "\n\uE0009\uE001", chatId: "chat-1", workspaceId: "ws-1" },
      });
      apiMock.emit({
        event: "output",
        data: { text: "\n\uE0008\uE001", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    // Switch back to chat-1: the full tool block (description + output) is intact.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "switch back to chat 1" }));
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("busy-switch-view").textContent || "{}") as {
        activeChatId: string;
        turns: Turn[];
      };
      expect(view.activeChatId).toBe("chat-1");
      expect(view.turns).toHaveLength(1);
      const stepText = view.turns[0].rounds
        .flatMap((r) => r.segments)
        .filter((s) => s.kind === "step")
        .map((s) => s.text)
        .join("");
      expect(stepText).toContain("Ran shell timeout 10");
      expect(stepText).toContain("9");
      expect(stepText).toContain("8");
    });
  });

  it("shows an unread dot for a chat whose background turn finished, then clears it on open", async () => {
    apiMock.getState.mockResolvedValue(
      buildState({
        chats: [
          {
            index: 0,
            id: "chat-1",
            name: "Chat 1",
            messageCount: 1,
            active: true,
            running: false,
            archived: false,
            planMode: false,
          },
          {
            index: 1,
            id: "chat-2",
            name: "Chat 2",
            messageCount: 2,
            active: false,
            running: false,
            archived: false,
            planMode: false,
          },
        ],
        activeChatId: "chat-1",
      }),
    );
    render(
      <AppProvider>
        <UnreadProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // chat-1 is focused; a background turn in chat-2 finishes and the backend
    // marks chat-2 unread in the idle snapshot.
    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-2",
          workspaceId: "ws-1",
          state: buildState({
            chats: [
              {
                index: 0,
                id: "chat-1",
                name: "Chat 1",
                messageCount: 1,
                active: true,
                running: false,
                archived: false,
                planMode: false,
              },
              {
                index: 1,
                id: "chat-2",
                name: "Chat 2",
                messageCount: 2,
                active: false,
                running: false,
                archived: false,
                planMode: false,
                hasUnread: true,
              },
            ],
            activeChatId: "chat-1",
          }),
        },
      });
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("unread-view").textContent || "{}") as Record<
        string,
        boolean
      >;
      expect(view["ws-1\u0000chat-2"]).toBe(true);
      expect(view["ws-1\u0000chat-1"]).toBeUndefined();
    });

    // Opening the chat clears the persisted flag via select_chat's state echo.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "open chat 2" }));
    });

    act(() => {
      apiMock.emit({
        event: "state",
        data: {
          workspaceId: "ws-1",
          state: buildState({
            chats: [
              {
                index: 0,
                id: "chat-1",
                name: "Chat 1",
                messageCount: 1,
                active: false,
                running: false,
                archived: false,
                planMode: false,
              },
              {
                index: 1,
                id: "chat-2",
                name: "Chat 2",
                messageCount: 2,
                active: true,
                running: false,
                archived: false,
                planMode: false,
                hasUnread: false,
              },
            ],
            activeChatId: "chat-2",
          }),
        },
      });
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("unread-view").textContent || "{}") as Record<
        string,
        boolean
      >;
      expect(view["ws-1\u0000chat-2"]).toBeUndefined();
    });
  });

  it("never shows an unread dot on the chat currently being viewed, even if a snapshot claims it", async () => {
    apiMock.getState.mockResolvedValue(
      buildState({
        chats: [
          {
            index: 0,
            id: "chat-1",
            name: "Chat 1",
            messageCount: 1,
            active: true,
            running: false,
            archived: false,
            planMode: false,
          },
          {
            index: 1,
            id: "chat-2",
            name: "Chat 2",
            messageCount: 2,
            active: false,
            running: false,
            archived: false,
            planMode: false,
          },
        ],
        activeChatId: "chat-1",
      }),
    );
    render(
      <AppProvider>
        <UnreadProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // A stale/spurious snapshot claims the VIEWED chat (chat-1) is unread. The
    // frontend must override it — the user is looking at that chat.
    act(() => {
      apiMock.emit({
        event: "state",
        data: {
          workspaceId: "ws-1",
          state: buildState({
            chats: [
              {
                index: 0,
                id: "chat-1",
                name: "Chat 1",
                messageCount: 1,
                active: true,
                running: false,
                archived: false,
                planMode: false,
                hasUnread: true,
              },
              {
                index: 1,
                id: "chat-2",
                name: "Chat 2",
                messageCount: 2,
                active: false,
                running: false,
                archived: false,
                planMode: false,
              },
            ],
            activeChatId: "chat-1",
          }),
        },
      });
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("unread-view").textContent || "{}") as Record<
        string,
        boolean
      >;
      expect(view["ws-1\u0000chat-1"]).toBeUndefined();
    });
  });

  it("does not let a stale refresh resurrect a cleared unread dot", async () => {
    apiMock.getState.mockResolvedValue(
      buildState({
        chats: [
          {
            index: 0,
            id: "chat-1",
            name: "Chat 1",
            messageCount: 1,
            active: true,
            running: false,
            archived: false,
            planMode: false,
          },
          {
            index: 1,
            id: "chat-2",
            name: "Chat 2",
            messageCount: 2,
            active: false,
            running: false,
            archived: false,
            planMode: false,
          },
        ],
        activeChatId: "chat-1",
      }),
    );
    // A refresh response built BEFORE the user opened the chat still claims it
    // unread — this is the stale value the race delivers.
    apiMock.listWorkspaceChats.mockResolvedValue([
      { id: "chat-1", name: "Chat 1", hasUnread: true },
      { id: "chat-2", name: "Chat 2", hasUnread: false },
    ]);
    render(
      <AppProvider>
        <UnreadProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // The user opened chat-1 (cleared) and is now viewing it. A late refresh of
    // the ACTIVE workspace arrives; it must NOT overwrite the cleared flag.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "refresh ws-1" }));
    });

    await waitFor(() => {
      const ws = JSON.parse(screen.getByTestId("unread-wschats").textContent || "{}") as Record<
        string,
        { id: string; hasUnread?: boolean }[]
      >;
      const chat1 = (ws["ws-1"] ?? []).find((c) => c.id === "chat-1");
      expect(chat1?.hasUnread).not.toBe(true);
    });
    const view = JSON.parse(screen.getByTestId("unread-view").textContent || "{}") as Record<
      string,
      boolean
    >;
    expect(view["ws-1\u0000chat-1"]).toBeUndefined();
  });

  it("cleared unread stays cleared across a workspace round-trip", async () => {
    // ws-2 is active; ws-1's chat-1 completed in the background (hasUnread=true).
    apiMock.getState.mockResolvedValue(
      buildState({
        workspace: {
          id: "ws-2",
          name: "Workspace B",
          root: "D:/workspace-b",
          workDirectory: "D:/workspace-b",
        },
        workspaces: [
          { id: "ws-1", name: "Workspace", root: "D:/workspace", active: false, isDefault: false },
          { id: "ws-2", name: "Workspace B", root: "D:/workspace-b", active: true, isDefault: false },
        ],
        chats: [
          {
            index: 0,
            id: "chat-1",
            name: "Ws2 Chat",
            messageCount: 1,
            active: true,
            running: false,
            archived: false,
            planMode: false,
          },
        ],
        activeChatId: "chat-1",
      }),
    );
    render(
      <AppProvider>
        <UnreadProbe />
      </AppProvider>,
    );
    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // Background completion in ws-1 refreshes its (non-active) cache with unread.
    apiMock.listWorkspaceChats.mockResolvedValue([
      { id: "chat-1", name: "Chat 1", hasUnread: true },
    ]);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "refresh ws-1" }));
    });
    await waitFor(() => {
      const ws = JSON.parse(screen.getByTestId("unread-wschats").textContent || "{}") as Record<
        string,
        { id: string; hasUnread?: boolean }[]
      >;
      expect((ws["ws-1"] ?? []).find((c) => c.id === "chat-1")?.hasUnread).toBe(true);
    });

    // Switch to ws-1 chat-1 (clears it), then a stale refresh arrives while ws-1
    // is ACTIVE — it must not resurrect the dot.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "open chat 1" }));
    });
    // Backend select_chat echoes the cleared snapshot for ws-1.
    act(() => {
      apiMock.emit({
        event: "state",
        data: {
          workspaceId: "ws-1",
          state: buildState({
            workspace: {
              id: "ws-1",
              name: "Workspace",
              root: "D:/workspace",
              workDirectory: "D:/workspace",
            },
            chats: [
              {
                index: 0,
                id: "chat-1",
                name: "Chat 1",
                messageCount: 1,
                active: true,
                running: false,
                archived: false,
                planMode: false,
                hasUnread: false,
              },
            ],
            activeChatId: "chat-1",
          }),
        },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "refresh ws-1" }));
    });

    // Switch back to ws-2: ws-1 becomes non-active; the dot must stay cleared.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "open ws2 chat" }));
    });
    act(() => {
      apiMock.emit({
        event: "state",
        data: {
          workspaceId: "ws-2",
          state: buildState({
            workspace: {
              id: "ws-2",
              name: "Workspace B",
              root: "D:/workspace-b",
              workDirectory: "D:/workspace-b",
            },
            chats: [
              {
                index: 0,
                id: "chat-1",
                name: "Ws2 Chat",
                messageCount: 1,
                active: true,
                running: false,
                archived: false,
                planMode: false,
              },
            ],
            activeChatId: "chat-1",
          }),
        },
      });
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("unread-view").textContent || "{}") as Record<
        string,
        boolean
      >;
      expect(view["ws-1\u0000chat-1"]).toBeUndefined();
    });
  });

  it("applies model + dashboard stats from a state event while the chat is streaming", async () => {
    render(
      <AppProvider>
        <StreamingStateMergeProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // Chat-1 starts streaming a turn.
    act(() => {
      apiMock.emit({
        event: "turn_start",
        data: { text: "Working", chatId: "chat-1", workspaceId: "ws-1" },
      });
    });

    // A state event for the streaming chat (e.g. a model switch or a select_chat
    // focus echo) must still update the composer selector + dashboard stats even
    // though only chats/plan are fully merged to protect segment accumulation.
    act(() => {
      apiMock.emit({
        event: "state",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState({
            chats: [{
              index: 0,
              id: "chat-1",
              name: "Chat 1",
              messageCount: 0,
              active: true,
              running: true,
              archived: false,
              planMode: false,
              model: "provider/model-b",
            }],
            activeChatId: "chat-1",
            model: {
              current: "provider/model-b",
              available: ["provider/model", "provider/model-b"],
              ready: true,
              reasoningEffort: "high",
              reasoningEfforts: ["low", "high"],
            },
            cacheStats: {
              totalTokens: 120,
              hitTokens: 80,
              missTokens: 40,
              hitRate: 66.7,
              model: "model-b",
              supported: true,
              hasBreakdown: true,
            },
            tokenStats: {
              outputTokens: 500,
              reasoningTokens: 100,
              hasOutputTokens: true,
              hasReasoningTokens: true,
              includesReasoning: false,
            },
          }),
        },
      });
    });

    await waitFor(() => {
      const view = JSON.parse(screen.getByTestId("stream-state").textContent || "{}") as {
        model: string;
        reasoningEffort: string;
        cacheStats: AppState["cacheStats"];
        tokenStats: AppState["tokenStats"];
      };
      expect(view.model).toBe("provider/model-b");
      expect(view.reasoningEffort).toBe("high");
      expect(view.cacheStats?.totalTokens).toBe(120);
      expect(view.cacheStats?.supported).toBe(true);
      expect(view.tokenStats?.outputTokens).toBe(500);
    });
  });

  it("createWorkspace lands on the new workspace draft and refreshes the list", async () => {
    render(
      <AppProvider>
        <CreateWorkspaceProbe />
      </AppProvider>,
    );

    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    const createdState = buildState({
      workspace: {
        id: "ws-3",
        name: "Workspace C",
        root: "D:/workspace-c",
        workDirectory: "D:/workspace-c",
      },
      workspaces: [
        ...buildState().workspaces,
        {
          id: "ws-3",
          name: "Workspace C",
          root: "D:/workspace-c",
          active: true,
          isDefault: false,
        },
      ],
      chats: [],
      activeChatId: "",
    });
    apiMock.createWorkspace.mockResolvedValueOnce({ ok: true, id: "ws-3" });
    apiMock.getState.mockResolvedValueOnce(createdState);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "create workspace" }));
    });

    await waitFor(() => {
      const list = JSON.parse(screen.getByTestId("create-ws-list").textContent || "[]") as Array<{ id: string }>;
      expect(list.map((w) => w.id)).toContain("ws-3");
      const draft = JSON.parse(screen.getByTestId("create-ws-draft").textContent || "{}") as {
        draftMode: boolean;
        draftWorkspaceId: string;
        activeWorkspaceId: string;
      };
      expect(draft.draftMode).toBe(true);
      expect(draft.draftWorkspaceId).toBe("ws-3");
      expect(draft.activeWorkspaceId).toBe("ws-3");
    });
  });
});

describe("subsumedHistoryStartIndex", () => {
  const iso = (ms: number) => new Date(ms).toISOString();
  const now = () => Date.now();

  function liveTurn(userText: string, startedAt: number, rounds: Turn["rounds"] = []): Turn {
    return { id: 1, userText, rounds, startedAt, endedAt: null };
  }

  it("returns the tail index when the active turn's archived copy is the page tail", () => {
    const t = now();
    const index = subsumedHistoryStartIndex(
      [liveTurn("hello", t)],
      [
        { userText: "older", timestamp: iso(t - 60_000), rounds: [] },
        { userText: "hello", timestamp: iso(t), rounds: [] },
      ],
    );
    expect(index).toBe(1);
  });

  it("subsumes the whole logical turn when compaction split it after the user entry", () => {
    // A mid-turn context compaction persists the running turn as [user turn,
    // compaction-summary turn, assistant continuation turn]. The tail is the
    // assistant-only continuation (no user text) and must not defeat the
    // match: the live copy replaces all three persisted turns.
    const t = now();
    const index = subsumedHistoryStartIndex(
      [liveTurn("能自动发现类似的目录", t)],
      [
        { userText: "旧消息", timestamp: iso(t - 120_000), rounds: [] },
        { userText: "能自动发现类似的目录", timestamp: iso(t - 1_000), rounds: [{ waitSeconds: 4, text: "计划", tools: "" }] },
        {
          userText: "",
          timestamp: iso(t + 1_000),
          rounds: [{ waitSeconds: 0, text: "", tools: "", compactNoticeTitle: "上下文已自动压缩", compactNoticeBody: "交接摘要" }],
        },
        { userText: "", timestamp: iso(t + 5_000), rounds: [{ waitSeconds: 7, text: "编译通过。跑 sandbox 测试：", tools: "" }] },
      ],
    );
    expect(index).toBe(1);
  });

  it("returns -1 when the page holds no copy of the active turn", () => {
    const t = now();
    const index = subsumedHistoryStartIndex(
      [liveTurn("new question", t)],
      [{ userText: "old question", timestamp: iso(t - 60_000), rounds: [] }],
    );
    expect(index).toBe(-1);
  });

  it("does not subsume an older identical prompt far outside the time window", () => {
    const t = now();
    const index = subsumedHistoryStartIndex(
      [liveTurn("repeat this task", t)],
      [{ userText: "repeat this task", timestamp: "2020-01-01 00:00:00", rounds: [{ waitSeconds: 1, text: "old answer", tools: "" }] }],
    );
    expect(index).toBe(-1);
  });

  it("falls back to output inclusion for legacy entries without a parseable timestamp", () => {
    const t = now();
    const index = subsumedHistoryStartIndex(
      [liveTurn("hello", t, [{ id: 1, waitStartedAt: t, waitEndedAt: null, segments: [{ id: 1, kind: "answer", text: "shared output" }] }])],
      [{ userText: "hello", rounds: [{ waitSeconds: 1, text: "shared output", tools: "" }] }],
    );
    expect(index).toBe(0);
  });

  it("falls back to the no-rounds match for legacy entries with no round data", () => {
    const t = now();
    const index = subsumedHistoryStartIndex(
      [liveTurn("legacy", t)],
      [{ userText: "legacy", rounds: [] }],
    );
    expect(index).toBe(0);
  });
});

describe("AppContext backend crash recovery", () => {
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
    // Simulate the desktop host bridge (pywebview js_api).
    (window as unknown as { pywebview?: unknown }).pywebview = {
      api: { backend_info: vi.fn() },
    };
  });

  afterEach(() => {
    delete (window as unknown as { pywebview?: unknown }).pywebview;
  });

  it("rebuilds the ApiClient and reconnects when the host publishes a new endpoint", async () => {
    const bridge = (
      window as unknown as {
        pywebview: { api: { backend_info: ReturnType<typeof vi.fn> } };
      }
    ).pywebview.api;
    // Server is down: getState rejects and the stale endpoint stays
    // published until the host finishes its restart.
    apiMock.getState.mockRejectedValue(new Error("connection refused"));
    bridge.backend_info.mockResolvedValue({ port: 1, token: "old-token" });

    render(
      <AppProvider>
        <TurnsProbe />
      </AppProvider>,
    );

    // Flush mount effects (initial getState + immediate endpoint poll).
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const callsBefore = apiMock.connectEvents.mock.calls.length;
    expect(callsBefore).toBeGreaterThanOrEqual(1);

    // The backend was restarted: the host pushes the new endpoint to the
    // frontend, which rebuilds its ApiClient and reconnects immediately.
    act(() => {
      window.dispatchEvent(
        new CustomEvent("codewood:backend-restarted", {
          detail: { port: 2, token: "new-token" },
        }),
      );
    });

    // Flush the re-run event-stream effect (its getState rejection lands here).
    await act(async () => {
      await Promise.resolve();
    });

    // The event stream was re-established against the rebuilt client.
    expect(apiMock.connectEvents.mock.calls.length).toBeGreaterThan(callsBefore);
    expect(apiMock.getState.mock.calls.length).toBeGreaterThan(1);
  });

  describe("background tasks", () => {
    it("routes background_task_output into the bound round and settles it on end", async () => {
      render(
        <AppProvider>
          <TurnsProbe />
        </AppProvider>,
      );
      await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

      act(() => {
        apiMock.emit({
          event: "turn_start",
          data: { text: "run", chatId: "chat-1", workspaceId: "ws-1" },
        });
        apiMock.emit({
          event: "output",
          data: {
            // The started block keeps its CMD_OUTPUT open (no \uE001 END).
            text: "\uE004• Ran in background echo hi\uE005\n\uE000Background task started (id=call_1)...",
            bgTaskId: "call_1",
            chatId: "chat-1",
            workspaceId: "ws-1",
          },
        });
      });

      await waitFor(() => {
        const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
        expect(turns[0].rounds[0].bgTaskId).toBe("call_1");
        expect(turns[0].rounds[0].bgTaskEnded).toBe(false);
        expect(turns[0].rounds[0].waitEndedAt).toBeNull();
      });

      act(() => {
        apiMock.emit({
          event: "background_task_output",
          data: { taskId: "call_1", text: "progress-1\n", chatId: "chat-1", workspaceId: "ws-1" },
        });
      });

      await waitFor(() => {
        const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
        const text = turns[0].rounds[0].segments.map((s) => s.text).join("");
        expect(text).toContain("progress-1");
      });

      act(() => {
        apiMock.emit({
          event: "background_task_output",
          data: {
            taskId: "call_1",
            text: "final-line\n",
            end: true,
            status: "completed",
            chatId: "chat-1",
            workspaceId: "ws-1",
          },
        });
      });

      await waitFor(() => {
        const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
        const round = turns[0].rounds[0];
        expect(round.bgTaskEnded).toBe(true);
        expect(round.waitEndedAt).toBeTypeOf("number");
        const text = round.segments.map((s) => s.text).join("");
        expect(text).toContain("final-line");
        expect(text).toContain("[bg task call_1 completed]");
        expect(text).toContain("\uE001");
      });
    });

    it("keeps the sub-agent session marker pushed while running and on end", async () => {
      render(
        <AppProvider>
          <TurnsProbe />
        </AppProvider>,
      );
      await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

      act(() => {
        apiMock.emit({
          event: "turn_start",
          data: { text: "run", chatId: "chat-1", workspaceId: "ws-1" },
        });
        apiMock.emit({
          event: "output",
          data: {
            text: "\uE004• Ran in background image-analyzer\uE005\n\uE000Background sub-agent started (id=call_1)...",
            bgTaskId: "call_1",
            chatId: "chat-1",
            workspaceId: "ws-1",
          },
        });
      });

      await waitFor(() => {
        const turns = JSON.parse(
          screen.getByTestId("turns").textContent || "[]",
        ) as Turn[];
        expect(turns[0].rounds[0].bgTaskId).toBe("call_1");
      });

      // The worker pushes the session marker while the sub-agent is running.
      act(() => {
        apiMock.emit({
          event: "background_task_output",
          data: {
            taskId: "call_1",
            text: "\uE008sa_live\uE009",
            chatId: "chat-1",
            workspaceId: "ws-1",
          },
        });
      });

      await waitFor(() => {
        const turns = JSON.parse(
          screen.getByTestId("turns").textContent || "[]",
        ) as Turn[];
        const text = turns[0].rounds[0].segments.map((s) => s.text).join("");
        expect(text).toContain("\uE008sa_live\uE009");
      });

      // The final end event replaces the output block but must keep the marker.
      act(() => {
        apiMock.emit({
          event: "background_task_output",
          data: {
            taskId: "call_1",
            text: "final answer\n",
            end: true,
            status: "completed",
            chatId: "chat-1",
            workspaceId: "ws-1",
          },
        });
      });

      await waitFor(() => {
        const turns = JSON.parse(
          screen.getByTestId("turns").textContent || "[]",
        ) as Turn[];
        const round = turns[0].rounds[0];
        expect(round.bgTaskEnded).toBe(true);
        const text = round.segments.map((s) => s.text).join("");
        expect(text).toContain("final answer");
        expect(text).toContain("\uE008sa_live\uE009");
      });
    });

    it("keeps the background round spinning across round_start/round_end", async () => {
      render(
        <AppProvider>
          <TurnsProbe />
        </AppProvider>,
      );
      await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

      act(() => {
        apiMock.emit({
          event: "turn_start",
          data: { text: "run", chatId: "chat-1", workspaceId: "ws-1" },
        });
        apiMock.emit({
          event: "output",
          data: {
            text: "\uE004• Ran in background echo hi\uE005\n\uE000Background task started (id=call_1)...",
            bgTaskId: "call_1",
            chatId: "chat-1",
            workspaceId: "ws-1",
          },
        });
      });

      await waitFor(() => {
        const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
        expect(turns[0].rounds[0].bgTaskId).toBe("call_1");
      });

      // A later model round (round_start/round_end) must NOT freeze the
      // background round — its spinner keeps running until the task ends.
      act(() => {
        apiMock.emit({ event: "round_start", data: { chatId: "chat-1", workspaceId: "ws-1" } });
        apiMock.emit({ event: "round_end", data: { chatId: "chat-1", workspaceId: "ws-1" } });
      });

      await waitFor(() => {
        const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
        const bgRound = turns[0].rounds.find((r) => r.bgTaskId === "call_1");
        expect(bgRound).toBeDefined();
        expect(bgRound!.waitEndedAt).toBeNull();
      });

      act(() => {
        apiMock.emit({
          event: "background_task_output",
          data: {
            taskId: "call_1",
            text: "",
            end: true,
            status: "completed",
            chatId: "chat-1",
            workspaceId: "ws-1",
          },
        });
      });

      await waitFor(() => {
        const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
        const bgRound = turns[0].rounds.find((r) => r.bgTaskId === "call_1");
        expect(bgRound).toBeDefined();
        expect(bgRound!.waitEndedAt).toBeTypeOf("number");
      });
    });

    it("does not swallow a bullet feedback line into an open background block", async () => {
      render(
        <AppProvider>
          <TurnsProbe />
        </AppProvider>,
      );
      await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

      act(() => {
        apiMock.emit({
          event: "turn_start",
          data: { text: "run", chatId: "chat-1", workspaceId: "ws-1" },
        });
        apiMock.emit({
          event: "output",
          data: {
            // A background task keeps its output block open (no \uE001 END).
            text: "\uE004• Ran in background echo hi\uE005\n\uE000Background task started (id=call_1)...",
            bgTaskId: "call_1",
            chatId: "chat-1",
            workspaceId: "ws-1",
          },
        });
      });
      await waitFor(() => {
        const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
        expect(turns[0].rounds.some((r) => r.bgTaskId === "call_1")).toBe(true);
      });

      // A blocking tool's feedback line ("• Wait ...") must render as its own
      // row instead of being appended into the open background block.
      act(() => {
        apiMock.emit({
          event: "output",
          data: {
            // The backend wraps feedback lines in the CMD_PROMPT sentinels
            // (\uE004…\uE005) plus ANSI colors; the bullet check must strip
            // both or the line is swallowed into the open background block.
            text:
              "\uE004\x1b[38;2;19;161;14m•\x1b[0m \x1b[1mWait\x1b[0m \x1b[94m(seconds=30)\x1b[0m\uE005",
            chatId: "chat-1",
            workspaceId: "ws-1",
          },
        });
      });

      await waitFor(() => {
        const turns = JSON.parse(screen.getByTestId("turns").textContent || "[]") as Turn[];
        const allText = turns[0].rounds.map((r) => r.segments.map((s) => s.text).join("")).join("|");
        expect(allText.replace(/\x1b\[[0-9;]*m/g, "")).toContain("Wait (seconds=30)");
        // The wait description must NOT be appended inside the background block.
        const bgRound = turns[0].rounds.find((r) => r.bgTaskId === "call_1");
        expect(bgRound!.segments.map((s) => s.text).join("")).not.toContain("Wait (seconds=30)");
        // And it must live in its own round, ready to render immediately.
        expect(
          turns[0].rounds.some((r) => r.segments.map((s) => s.text).join("").replace(/\x1b\[[0-9;]*m/g, "").includes("Wait (seconds=30)")),
        ).toBe(true);
      });
    });
  });
});

describe("compactContext history refresh", () => {
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

  it("reloads the transcript after a successful manual compact", async () => {
    function CompactProbe() {
      const { compactContext, historyTurns } = useApp();
      return (
        <>
          <button onClick={() => void compactContext()}>compact</button>
          <pre data-testid="history-and-turns">
            {JSON.stringify({ history: historyTurns.map((h) => h.userText) })}
          </pre>
        </>
      );
    }

    render(
      <AppProvider>
        <CompactProbe />
      </AppProvider>,
    );
    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    // Initial snapshot: bind the active chat so the history loader runs and
    // historyChatRef points at chat-1.
    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState(),
        },
      });
    });
    await waitFor(() => expect(apiMock.getChatHistory).toHaveBeenCalled());

    const historyCallsBefore = apiMock.getChatHistory.mock.calls.length;

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "compact" }));
    });
    await waitFor(() => expect(apiMock.compactContext).toHaveBeenCalled());
    // A successful manual compact must refresh the persisted transcript
    // immediately (no idle/state SSE event follows a manual compact), so the
    // summary turn renders without the user having to switch chats.
    await waitFor(() => {
      expect(apiMock.getChatHistory.mock.calls.length).toBeGreaterThan(
        historyCallsBefore,
      );
    });
  });

  it("does not refresh a different chat when the user switched during compact", async () => {
    let resolveCompact: (value: { ok: boolean }) => void = () => {};
    apiMock.compactContext.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCompact = resolve;
        }),
    );

    function CompactSwitchProbe() {
      const { compactContext, switchToChat } = useApp();
      return (
        <>
          <button onClick={() => void compactContext()}>compact</button>
          <button onClick={() => void switchToChat("chat-2", "ws-1")}>
            switch
          </button>
        </>
      );
    }

    render(
      <AppProvider>
        <CompactSwitchProbe />
      </AppProvider>,
    );
    await waitFor(() => expect(apiMock.connectEvents).toHaveBeenCalled());

    act(() => {
      apiMock.emit({
        event: "idle",
        data: {
          chatId: "chat-1",
          workspaceId: "ws-1",
          state: buildState(),
        },
      });
    });
    await waitFor(() => expect(apiMock.getChatHistory).toHaveBeenCalled());

    // A compact starts while the user is viewing chat-1.  The HTTP request
    // stays pending (the backend compacts synchronously in that handler).
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "compact" }));
    });
    await waitFor(() => expect(apiMock.compactContext).toHaveBeenCalled());

    const historyCallsBefore = apiMock.getChatHistory.mock.calls.length;
    // ...but the user switches to chat-2 while the request is still pending.
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "switch" }));
    });
    await waitFor(() => expect(apiMock.selectChat).toHaveBeenCalled());
    expect(apiMock.selectChat).toHaveBeenCalledWith("chat-2", "ws-1");
    await waitFor(() => {
      const lastCall = apiMock.getChatHistory.mock.calls[
        apiMock.getChatHistory.mock.calls.length - 1
      ] as unknown[];
      // client.getChatHistory(before, limit, chatId, workspaceId)
      expect(lastCall?.[2]).toBe("chat-2");
    });
    // When the compact response finally resolves, the post-compact refresh is
    // keyed to the ORIGINAL chat (chat-1) and must not clobber chat-2.
    await act(async () => {
      resolveCompact({ ok: true });
    });
    expect(apiMock.getChatHistory.mock.calls.length).toBe(
      historyCallsBefore + 1,
    );
    const lastCall = apiMock.getChatHistory.mock.calls[
      apiMock.getChatHistory.mock.calls.length - 1
    ] as unknown[];
    expect(lastCall[2]).toBe("chat-2");
  });
});
