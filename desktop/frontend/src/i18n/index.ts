export type Lang = "en" | "zh-CN";

type Dict = Record<string, string>;

const en: Dict = {
  "nav.chat": "Chat",
  "nav.workspace": "Workspace",
  "nav.settings": "Settings",
  "app.subtitle": "AI coding assistant",

  "sidebar.newChat": "New chat",
  "sidebar.pinned": "Pinned",
  "sidebar.workspaces": "Workspaces",
  "sidebar.chats": "Chats",
  "sidebar.archived": "Archived",
  "sidebar.noChats": "No chats yet",
  "sidebar.noWorkspaces": "No workspaces",

  "activity.working": "Working ...",
  "activity.workedFor": "Worked for",

  "menu.pinProject": "Pin project",
  "menu.unpinProject": "Unpin project",
  "menu.openInExplorer": "Open in Explorer",
  "menu.renameProject": "Rename project",
  "menu.archiveChats": "Archive chats",
  "menu.remove": "Remove",
  "menu.pinChat": "Pin Chat",
  "menu.unpinChat": "Unpin Chat",
  "menu.archiveChat": "Archive chat",
  "menu.unarchiveChat": "Unarchive chat",
  "menu.rename": "Rename",
  "menu.more": "More",

  "empty.prompt": "What should we work on in {workspace}?",

  "status.online": "Connected",
  "status.offline": "Disconnected",

  "workspace.none": "Don't work in a project",
  "workspace.search": "Search projects…",
  "workspace.addNew": "Add new project",
  "workspace.useExisting": "Use an existing folder",

  "model.label": "Model",
  "panel.toggle": "Toggle sidebar",

  "menu.file": "File",
  "menu.help": "Help",
  "menu.file.newChat": "New Chat",
  "menu.file.openFolder": "Open Folder...",
  "menu.file.close": "Close",
  "menu.file.settings": "Settings...",
  "menu.file.exit": "Exit",
  "menu.help.about": "About Code Wood",
  "win.minimize": "Minimize",
  "win.maximize": "Maximize",
  "win.close": "Close",

  "about.title": "About Code Wood",
  "about.version": "Version",
  "about.description": "Code Wood is an AI coding assistant.",

  "chat.sessions": "Sessions",
  "chat.new": "New chat",
  "chat.newPlaceholder": "Chat name (optional)",
  "chat.reload": "Reload",
  "chat.rename": "Rename",
  "chat.fork": "Fork",
  "chat.delete": "Delete",
  "chat.deleteAll": "Delete all",
  "chat.renamePrompt": "New chat name:",
  "chat.messages": "msgs",
  "chat.empty": "No conversation yet. Type a prompt or a /command below.",
  "chat.inputPlaceholder": "Send a message, or a /command (e.g. /chat list, /workspace list)…",
  "chat.send": "Send",
  "chat.interrupt": "Stop",
  "chat.busy": "Working…",
  "chat.you": "You",

  "workspace.title": "Workspaces",
  "workspace.current": "Current workspace",
  "workspace.create": "Create",
  "workspace.createPathPlaceholder": "New workspace path",
  "workspace.createNamePlaceholder": "Name (optional)",
  "workspace.switch": "Switch",
  "workspace.rename": "Rename",
  "workspace.delete": "Delete",
  "workspace.renamePrompt": "New workspace name:",
  "workspace.root": "Root",
  "workspace.active": "active",

  "settings.title": "Settings",
  "settings.theme": "Theme",
  "settings.theme.light": "Light",
  "settings.theme.dark": "Dark",
  "settings.theme.system": "System",
  "settings.language": "Language",
  "settings.model": "Model",
  "settings.executionPolicy": "Execution policy",
  "settings.policy.unlimited": "Unlimited",
  "settings.policy.moderate": "Moderate",
  "settings.policy.confirmation": "Confirmation",
  "settings.close": "Close",

  "confirm.title": "Confirmation required",
  "confirm.yes": "Yes",
  "confirm.no": "No",
  "confirm.always": "Always",
  "confirm.answerPlaceholder": "Type a response…",
  "confirm.submit": "Submit",

  "common.cancel": "Cancel",
  "common.ok": "OK",
  "common.connecting": "Connecting to backend…",
};

const zhCN: Dict = {
  "nav.chat": "对话",
  "nav.workspace": "工作区",
  "nav.settings": "设置",
  "app.subtitle": "AI 编程助手",

  "sidebar.newChat": "新建对话",
  "sidebar.pinned": "已置顶",
  "sidebar.workspaces": "工作区",
  "sidebar.chats": "对话",
  "sidebar.archived": "已归档",
  "sidebar.noChats": "暂无对话",
  "sidebar.noWorkspaces": "暂无工作区",

  "activity.working": "处理中 ...",
  "activity.workedFor": "用时",

  "menu.pinProject": "置顶工作区",
  "menu.unpinProject": "取消置顶",
  "menu.openInExplorer": "在文件管理器中打开",
  "menu.renameProject": "重命名工作区",
  "menu.archiveChats": "归档对话",
  "menu.remove": "移除",
  "menu.pinChat": "置顶对话",
  "menu.unpinChat": "取消置顶",
  "menu.archiveChat": "归档对话",
  "menu.unarchiveChat": "取消归档",
  "menu.rename": "重命名",
  "menu.more": "更多",

  "empty.prompt": "在 {workspace} 中我们要做点什么？",

  "status.online": "已连接",
  "status.offline": "未连接",

  "workspace.none": "不在任何项目中工作",
  "workspace.search": "搜索项目……",
  "workspace.addNew": "新增项目",
  "workspace.useExisting": "使用已有文件夹",

  "model.label": "模型",
  "panel.toggle": "切换侧边栏",

  "menu.file": "文件",
  "menu.help": "帮助",
  "menu.file.newChat": "新建对话",
  "menu.file.openFolder": "打开文件夹…",
  "menu.file.close": "关闭",
  "menu.file.settings": "设置…",
  "menu.file.exit": "退出",
  "menu.help.about": "关于 Code Wood",
  "win.minimize": "最小化",
  "win.maximize": "最大化",
  "win.close": "关闭",

  "about.title": "关于 Code Wood",
  "about.version": "版本",
  "about.description": "Code Wood 是一款 AI 编程助手。",

  "chat.sessions": "会话",
  "chat.new": "新建对话",
  "chat.newPlaceholder": "对话名称（可选）",
  "chat.reload": "重新加载",
  "chat.rename": "重命名",
  "chat.fork": "复刻",
  "chat.delete": "删除",
  "chat.deleteAll": "全部删除",
  "chat.renamePrompt": "新的对话名称：",
  "chat.messages": "条",
  "chat.empty": "暂无对话。在下方输入提示词或 /命令。",
  "chat.inputPlaceholder": "发送消息，或输入 /命令（如 /chat list、/workspace list）……",
  "chat.send": "发送",
  "chat.interrupt": "停止",
  "chat.busy": "处理中……",
  "chat.you": "你",

  "workspace.title": "工作区",
  "workspace.current": "当前工作区",
  "workspace.create": "创建",
  "workspace.createPathPlaceholder": "新工作区路径",
  "workspace.createNamePlaceholder": "名称（可选）",
  "workspace.switch": "切换",
  "workspace.rename": "重命名",
  "workspace.delete": "删除",
  "workspace.renamePrompt": "新的工作区名称：",
  "workspace.root": "根目录",
  "workspace.active": "当前",

  "settings.title": "设置",
  "settings.theme": "主题",
  "settings.theme.light": "浅色",
  "settings.theme.dark": "深色",
  "settings.theme.system": "跟随系统",
  "settings.language": "语言",
  "settings.model": "模型",
  "settings.executionPolicy": "执行策略",
  "settings.policy.unlimited": "无限制",
  "settings.policy.moderate": "适中",
  "settings.policy.confirmation": "逐项确认",
  "settings.close": "关闭",

  "confirm.title": "需要确认",
  "confirm.yes": "是",
  "confirm.no": "否",
  "confirm.always": "总是",
  "confirm.answerPlaceholder": "输入回复……",
  "confirm.submit": "提交",

  "common.cancel": "取消",
  "common.ok": "确定",
  "common.connecting": "正在连接后端……",
};

const dictionaries: Record<Lang, Dict> = { en, "zh-CN": zhCN };

export function normalizeLang(value: string | undefined | null): Lang {
  const text = (value ?? "").trim().toLowerCase();
  if (text === "zh-cn" || text === "zh" || text === "zh_cn") {
    return "zh-CN";
  }
  return "en";
}

export function translate(lang: Lang, key: string): string {
  return dictionaries[lang]?.[key] ?? dictionaries.en[key] ?? key;
}

export const SUPPORTED_LANGS: Lang[] = ["en", "zh-CN"];
