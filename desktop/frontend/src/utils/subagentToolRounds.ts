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
  const bullet = failed ? ansiRgb("•", 197, 15, 31) : ansiRgb("•", 19, 161, 14);
  const label = toolLabel(name, lang);
  const detail = formatToolDetail(name, args);
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
