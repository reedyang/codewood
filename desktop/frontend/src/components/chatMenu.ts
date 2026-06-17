import type { MenuItem } from "./ContextMenu";

// Chat ids repeat across workspaces, so pin/archive prefs are keyed per
// workspace. Shared so the sidebar and the chat title bar agree on the key.
export function chatKey(wsId: string, chatId: string): string {
  return `${wsId}\u0000${chatId}`;
}

export interface ChatMenuDeps {
  t: (key: string) => string;
  isPinned: boolean;
  isArchived: boolean;
  onTogglePin: () => void;
  onToggleArchive: () => void;
  onRename: () => void;
  onRemove: () => void;
}

/** The chat context menu shared by the sidebar rows and the chat title bar. */
export function buildChatMenuItems(deps: ChatMenuDeps): MenuItem[] {
  return [
    {
      id: "pin",
      label: deps.isPinned ? deps.t("menu.unpinChat") : deps.t("menu.pinChat"),
      onSelect: deps.onTogglePin,
    },
    {
      id: "archive",
      label: deps.isArchived ? deps.t("menu.unarchiveChat") : deps.t("menu.archiveChat"),
      onSelect: deps.onToggleArchive,
    },
    {
      id: "rename",
      label: deps.t("menu.rename"),
      onSelect: deps.onRename,
    },
    {
      id: "remove",
      label: deps.t("menu.remove"),
      danger: true,
      onSelect: deps.onRemove,
    },
  ];
}
