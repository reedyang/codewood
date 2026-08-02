import type {
  SubAgentMessage,
  SubAgentToolCall,
  SubAgentToolRoundRaw,
} from "../api/types";
import { translate, type Lang } from "../i18n";

const CMD_OUTPUT_BEGIN = "\uE000";
const CMD_OUTPUT_END = "\uE001";
const CMD_PROMPT_BEGIN = "\uE004";
const CMD_PROMPT_END = "\uE005";
const ANSI_RESET = "\u001b[0m";

function ansiRgb(text: string, r: number, g: number, b: number): string {
  return `\u001b[38;2;${r};${g};${b}m${text}${ANSI_RESET}`;
}

function ansiBold(text: string): string {
  return `\u001b[1m${text}${ANSI_RESET}`;
}

function ansiBrightBlue(text: string): string {
  return ansiRgb(text, 61, 168, 245);
}

function ansiYellow(text: string): string {
  return ansiRgb(text, 227, 209, 112);
}

function ansiCyan(text: string): string {
  return ansiRgb(text, 97, 175, 239);
}

function ansiGreen(text: string): string {
  return ansiRgb(text, 63, 172, 72);
}

function looksLikePathOrUrl(text: string): boolean {
  if (!text) return false;
  return /^(?:\/|[A-Za-z]:[\\/]|~\/|\.\.?[\\/])/.test(text)
    || /^https?:\/\//.test(text)
    || /\.[a-zA-Z]{1,6}$/.test(text)
    || /[\\/][\w.-]+$/.test(text);
}

const SUBCOMMANDS = new Set([
  "install", "run", "start", "stop", "check", "list", "show",
  "create", "delete", "remove", "update", "switch", "clone",
  "pull", "push", "build", "test", "verify",
]);

function highlightShellCommand(cmd: string): string {
  if (!cmd) return cmd;
  const parts = cmd.split(/(\s+)/);
  const out: string[] = [];
  let firstTokenSeen = false;
  let prevPlain = "";
  for (const part of parts) {
    if (!part || /^\s+$/.test(part)) {
      out.push(part);
      continue;
    }
    const plain = part.replace(/^['"]+|['"]+$/g, "");
    const lower = plain.toLowerCase();
    if (part === "|" || part === "||" || part === "&&" || part === ";") {
      out.push(ansiYellow(part));
      prevPlain = lower;
      continue;
    }
    if (!firstTokenSeen) {
      out.push(ansiBrightBlue(part));
      firstTokenSeen = true;
      prevPlain = lower;
      continue;
    }
    if (/^--?/.test(plain) && !/^https?:\/\//.test(part)) {
      out.push(ansiYellow(part));
      prevPlain = lower;
      continue;
    }
    if (prevPlain === "-m") {
      out.push(ansiBrightBlue(part));
      prevPlain = lower;
      continue;
    }
    if (SUBCOMMANDS.has(lower)) {
      out.push(ansiBrightBlue(part));
      prevPlain = lower;
      continue;
    }
    if (part.length >= 2 && /^['"]/.test(part) && /['"]$/.test(part)) {
      const inner = part.slice(1, -1);
      if (looksLikePathOrUrl(inner)) {
        out.push(part[0] + ansiCyan(inner) + part[part.length - 1]);
      } else {
        out.push(ansiGreen(part));
      }
      prevPlain = lower;
      continue;
    }
    if (looksLikePathOrUrl(plain) || /^%[^%]+%$/.test(plain) || /^\$[\w{}]+/.test(plain)) {
      out.push(ansiCyan(part));
      prevPlain = lower;
      continue;
    }
    out.push(part);
    prevPlain = lower;
  }
  return out.join("");
}

function toolLabel(toolName: string, lang: Lang): string {
  const name = String(toolName || "").trim().toLowerCase();
  if (name) {
    const translated = translate(lang, `tool.label.${name}`);
    if (translated !== `tool.label.${name}`) {
      return translated;
    }
  }
  const words = name.replace(/_/g, " ").split(/\s+/).filter(Boolean);
  if (words.length === 0) {
    return translate(lang, "tool.label.tool");
  }
  words[0] = words[0][0].toUpperCase() + words[0].slice(1);
  return words.join(" ");
}

function highlightPath(path: string): string {
  return ansiRgb(path, 97, 175, 239);
}

function toRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function parseToolCallArgs(call: SubAgentToolCall | undefined): Record<string, unknown> {
  if (!call) {
    return {};
  }
  if (call.args && typeof call.args === "object" && !Array.isArray(call.args)) {
    return call.args;
  }
  const rawArgs = call.function?.arguments;
  if (!rawArgs) {
    return {};
  }
  if (typeof rawArgs === "object" && !Array.isArray(rawArgs)) {
    return rawArgs;
  }
  if (typeof rawArgs !== "string") {
    return {};
  }
  try {
    return toRecord(JSON.parse(rawArgs));
  } catch {
    return {};
  }
}

function formatReadDetail(args: Record<string, unknown>): string {
  const path = String(args.path || "").trim();
  if (!path) {
    return "";
  }
  const suffix: string[] = [];
  if (args.offset != null) {
    suffix.push(`offset=${String(args.offset)}`);
  }
  if (args.limit != null) {
    suffix.push(`limit=${String(args.limit)}`);
  }
  return `${highlightPath(path)}${suffix.length ? ` [${suffix.join(", ")}]` : ""}`;
}

function formatToolDetail(toolName: string, args: Record<string, unknown>): string {
  const name = String(toolName || "").trim().toLowerCase();
  if (name === "read") {
    return formatReadDetail(args);
  }
  if (name === "grep") {
    const path = String(args.path || "").trim();
    const include = String(args.include || "").trim();
    if (path && include) {
      const sep = /^[\\/]/.test(include) ? "" : "/";
      return highlightPath(`${path}${sep}${include}`);
    }
    if (path) return highlightPath(path);
    if (include) return highlightPath(include);
    return "";
  }
  if (name === "project_context_search") {
    const query = String(args.query || "").trim();
    return query ? `: "${query}"` : "";
  }
  if (name === "run_subagent") {
    const subagent = String(args.subagent || "").trim();
    const topic = String(args.topic || "").trim();
    if (subagent && topic) {
      return `${subagent}: ${topic}`;
    }
    return subagent || topic;
  }
  if (name === "shell" || name === "bash") {
    const cmd = String(args.command || "").trim();
    return cmd || "";
  }
  if (name === "update_plan") {
    return "";
  }
  if (args.path != null) {
    const path = String(args.path || "").trim();
    if (path) {
      return highlightPath(path);
    }
  }
  return "";
}

export function buildFallbackToolRound(
  toolName: string,
  args: Record<string, unknown> = {},
  output = "",
  marker = "",
  options?: { failed?: boolean; lang?: Lang; errText?: string },
): string {
  const name = String(toolName || "").trim() || "tool";
  const lang = options?.lang ?? "en";
  const failed = Boolean(options?.failed);
  const isShell = name === "shell" || name === "bash";
  const bullet = failed ? ansiRgb("•", 197, 15, 31) : ansiRgb("•", 19, 161, 14);
  const label = isShell
    ? ansiBold(translate(lang, "status.ran"))
    : toolLabel(name, lang);
  const detail = isShell ? highlightShellCommand(String(args.command || "").trim()) : formatToolDetail(name, args);
  let round = `${CMD_PROMPT_BEGIN}${bullet} ${label}${detail ? ` ${detail}` : ""}${CMD_PROMPT_END}`;
  if (output) {
    round += `\n${CMD_OUTPUT_BEGIN}${output}${CMD_OUTPUT_END}`;
  } else if (options?.errText) {
    round += `\n${CMD_OUTPUT_BEGIN}${options.errText}${CMD_OUTPUT_END}`;
  }
  if (marker) {
    round += `\n${marker}`;
  }
  return round;
}

export function buildFallbackToolRoundFromCall(
  call: SubAgentToolCall | undefined,
  output = "",
  options?: { failed?: boolean; lang?: Lang },
): string {
  const name = String(call?.function?.name || call?.name || "").trim();
  return buildFallbackToolRound(name, parseToolCallArgs(call), output, "", options);
}

export function buildFallbackToolRoundsFromRaw(
  rawList: SubAgentToolRoundRaw[] | undefined,
  options?: { lang?: Lang },
): string[] {
  if (!Array.isArray(rawList) || rawList.length === 0) {
    return [];
  }
  return rawList
    .map((item) =>
      buildFallbackToolRound(
        String(item?.tool || ""),
        toRecord(item?.args),
        String(item?.output || ""),
        String(item?.marker || ""),
        { failed: Boolean(item?.failed), lang: options?.lang, errText: String(item?.error || "") },
      ),
    )
    .filter(Boolean);
}

export function getSubAgentMessageToolRounds(
  msg: SubAgentMessage,
  options?: { lang?: Lang },
): string[] {
  if (Array.isArray(msg.tool_rounds) && msg.tool_rounds.length > 0) {
    return msg.tool_rounds;
  }
  const fromRaw = buildFallbackToolRoundsFromRaw(msg._tool_rounds_raw, options);
  if (fromRaw.length > 0) {
    return fromRaw;
  }
  if (!Array.isArray(msg.tool_calls) || msg.tool_calls.length === 0) {
    return [];
  }
  return msg.tool_calls.map((call) => buildFallbackToolRoundFromCall(call, "", options));
}
