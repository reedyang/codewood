import { useState, useCallback, useEffect } from "react";
import { AnsiText } from "./Ansi";
import { hostApi } from "../utils/hostApi";
import { DiffPreview, langFromPath } from "./DiffPreview";
import { HoverTooltip } from "./HoverTooltip";
import { SyntaxOutput, resolveToolOutputLang } from "./SyntaxOutput";
import { Icon } from "./Icon";
import { useApp } from "../state/AppContext";
import type { DiffRow } from "../api/types";

/** Process \\b within a single line (no \\r, no \\n).
 *  Each \\b moves the cursor back one column; the following character
 *  overwrites. Trailing non-overwritten characters are left as-is
 *  (matching terminal behaviour where \\b doesn't erase). */
function handleBackspace(text: string): string {
  if (!text.includes("\b")) return text;
  // If the text starts with \b, those backspaces arrived without
  // their preceding context (cross-chat / cross-context artifact).
  if (text[0] === "\b") {
    return text.replace(/\x08/g, "");
  }
  let cur = "";
  let col = 0;
  for (const ch of text) {
    if (ch === "\b") {
      if (col > 0) col--;
    } else {
      if (col < cur.length) {
        cur = cur.slice(0, col) + ch + cur.slice(col + 1);
      } else {
        cur += ch;
      }
      col++;
    }
  }
  return cur;
}

/** Normalize the backend's saved-cursor repaint sequence into the final
 *  visible line before we process simpler \\r / \\b overwrites. This is what
 *  lets a live tool prompt flip from green to red as soon as the tool fails,
 *  instead of waiting for end-of-turn history reconstruction. */
function applyCursorRepaints(text: string): string {
  const repaintRe = /\x1b7\x1b\[(\d*)A\r\x1b\[2K([\s\S]*?)\x1b8/;
  let next = text;
  let guard = 0;
  while (guard < 1000) {
    const match = repaintRe.exec(next);
    if (!match) {
      break;
    }
    const before = next.slice(0, match.index);
    const after = next.slice(match.index + match[0].length);
    const up = Math.max(1, parseInt(match[1] || "1", 10) || 1);
    const replacement = match[2] || "";
    const lines = before.split("\n");
    const currentRow = Math.max(0, lines.length - 1);
    const targetRow = Math.max(0, currentRow - up);
    lines[targetRow] = replacement;
    next = `${lines.join("\n")}${after}`;
    guard += 1;
  }
  return next;
}

/** Process \\r (carriage return) with terminal-like overwrite:
 *  each line is split on \\r; the last non-empty segment wins.
 *  For lines that end with \\n (completed lines), a trailing empty
 *  \\r segment means the cursor was moved to column 0 with nothing
 *  written after — the line should be cleared (spinner stopped).
 *  \\b is also handled inside each segment.
 *  ANSI CSI sequences are preserved for AnsiText. */
function handleControlChars(text: string): string {
  text = applyCursorRepaints(text);
  const hasBS = text.includes("\b");
  const hasCR = text.includes("\r");
  if (!hasBS && !hasCR) return text;

  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") {
    lines.pop();
  }
  const lastIdx = lines.length - 1;

  return lines
    .map((line, idx) => {
      if (!line.includes("\r")) {
        return hasBS && line.includes("\b") ? handleBackspace(line) : line;
      }
      const parts = line.split("\r");
      const isCompletedLine = idx < lastIdx;
      // Completed line with multiple \r-separated non-empty segments
      // → spinner / progress bar that was never finalized. Clear it.
      if (isCompletedLine) {
        const nonEmptyCount = parts.filter((p) => p !== "").length;
        if (nonEmptyCount >= 2) return "";
        // Single non-empty frame starting with \r: spinner/progress
        // that wasn't finalized (cursor moved to next line with \n).
        if (nonEmptyCount === 1 && parts[0] === "") return "";
      }
      // Completed line with 2+ trailing empty \r segments means the
      // line was explicitly cleared by EL (erase-line) CSI → \r.
      if (isCompletedLine && parts.length > 1) {
        let trailingEmpties = 0;
        for (let i = parts.length - 1; i >= 0 && parts[i] === ""; i--) {
          trailingEmpties++;
        }
        if (trailingEmpties >= 2) return "";
      }
      // Take the last non-empty \r segment; run backspace on it.
      for (let i = parts.length - 1; i >= 0; i--) {
        if (parts[i]) return handleBackspace(parts[i]);
      }
      return "";
    })
    .join("\n");
}

/** Backward-compat alias. */
function handleCarriageReturn(text: string): string {
  return handleControlChars(text);
}

// Private-use sentinels wrapping raw command output, emitted by the backend in
// GUI mode (kept in sync with src/core/console_utils.py). They let us render
// command output in its own padded node so the indent survives soft-wrapping.
const CMD_OUTPUT_BEGIN = "\uE000";
const CMD_OUTPUT_END = "\uE001";
// Sentinels wrapping the "• Ran <command>" feedback prompt line (kept in sync
// with src/core/console_utils.py). The bullet is rendered in its own column and
// the command text gets a hanging indent so soft-wrapped lines stay aligned.
const CMD_PROMPT_BEGIN = "\uE004";
const CMD_PROMPT_END = "\uE005";
// Sentinels wrapping a structured apply_patch change-preview JSON payload
// (kept in sync with cli/core/console_utils.py). Rendered as a collapsible,
// syntax-highlighted diff block instead of raw JSON.
const DIFF_BEGIN = "\uE006";
const DIFF_END = "\uE007";
// Sentinels wrapping a sub-agent session ID (kept in sync with
// cli/core/console_utils.py). Used to associate a tool call with its
// sub-agent session for the GUI's session viewer.
const SUBAGENT_SESSION_BEGIN = "\uE008";
const SUBAGENT_SESSION_END = "\uE009";
const ANSI_SGR_RE = /\x1b\[[0-9;]*m/g;
const ANSI_CSI_RE = /\x1b\[[0-?]*[ -/]*[@-~]/g;
const BOX_DRAWING_RE = /[\u2500-\u257F]/g;

/** Lines that after trimming consist mostly of box-drawing characters are
 *  table borders.  When present, the container should shrink-wrap to the
 *  longest such line so tables don't wrap, while long plain-text lines
 *  still wrap normally. */
function getLongestTableBorderWidth(text: string): number {
  let maxW = 0;
  for (const rawLine of text.split("\n")) {
    const cleaned = rawLine.replace(ANSI_SGR_RE, "").replace(ANSI_CSI_RE, "");
    const trimmed = cleaned.trim();
    if (!trimmed) continue;
    const boxCount = (trimmed.match(BOX_DRAWING_RE) || []).length;
    if (boxCount / trimmed.length > 0.6) {
      maxW = Math.max(maxW, cleaned.length);
    }
  }
  return maxW;
}

type SegKind = "text" | "cmd" | "prompt" | "diff" | "subagent_session";
type Segment = { kind: SegKind; text: string };

function splitSteps(text: string): Segment[] {
  text = applyCursorRepaints(text);
  const segments: Segment[] = [];
  let buf = "";
  let mode: SegKind = "text";
  const flush = () => {
    if (buf) {
      segments.push({ kind: mode, text: buf });
    }
    buf = "";
  };
  for (const ch of text) {
    if (mode === "text" && ch === CMD_OUTPUT_BEGIN) {
      flush();
      mode = "cmd";
      continue;
    }
    if (mode === "cmd" && ch === CMD_OUTPUT_END) {
      flush();
      mode = "text";
      continue;
    }
    if (mode === "text" && ch === CMD_PROMPT_BEGIN) {
      flush();
      mode = "prompt";
      continue;
    }
    if (mode === "prompt" && ch === CMD_PROMPT_END) {
      flush();
      mode = "text";
      continue;
    }
    if (mode === "text" && ch === DIFF_BEGIN) {
      flush();
      mode = "diff";
      continue;
    }
    if (mode === "diff" && ch === DIFF_END) {
      flush();
      mode = "text";
      continue;
    }
    if (mode === "text" && ch === SUBAGENT_SESSION_BEGIN) {
      flush();
      mode = "subagent_session";
      continue;
    }
    if (mode === "subagent_session" && ch === SUBAGENT_SESSION_END) {
      flush();
      mode = "text";
      continue;
    }
    buf += ch;
  }
  flush();
  return segments;
}

/** Split a "• Ran ..." prompt into its bullet column and the command text. */
function splitPromptBullet(text: string): { bullet: string; body: string } {
  // The bullet is the first glyph; an optional leading ANSI color sequence may
  // precede it. Keep the bullet (with its color) and treat the rest as body.
  const m = text.match(/^((?:\x1b\[[0-9;]*m)*.)(\s*)([\s\S]*)$/);
  if (!m) {
    return { bullet: "", body: text };
  }
  return { bullet: m[1], body: m[3] };
}

function trimBlankEdges(text: string): string {
  return text.replace(/^\n+/, "").replace(/\n+$/, "");
}

function stripAnsi(text: string): string {
  return text.replace(ANSI_SGR_RE, "");
}

type ExplorePromptState = "running" | "completed";
type SegmentUnit =
  | { kind: "prompt"; segments: Segment[]; exploreState: ExplorePromptState | null }
  | { kind: "other"; segments: Segment[] };

function normalizeEllipsis(text: string): string {
  return text.replace(/\u2026/g, "...");
}

function stripInvisibleText(text: string): string {
  return handleCarriageReturn(text).replace(ANSI_CSI_RE, "").trim();
}

function getExplorePromptState(text: string): ExplorePromptState | null {
  const plain = normalizeEllipsis(
    stripAnsi(splitPromptBullet(trimBlankEdges(text)).body).trim(),
  ).toLowerCase();
  if (!plain) {
    return null;
  }
  if (
    plain === "探索中..." ||
    /^exploring(?: .+)?\.\.\.$/.test(plain) ||
    /^正在探索：.+\.\.\.$/.test(plain)
  ) {
    return "running";
  }
  if (
    plain.startsWith("探索完成（") ||
    plain.startsWith("探索完成：") ||
    /^explored(?: .+)? for .*s$/.test(plain)
  ) {
    return "completed";
  }
  return null;
}

function isBlankTextSegment(seg: Segment): boolean {
  return seg.kind === "text" && !trimBlankEdges(stripInvisibleText(seg.text));
}

function getUnitSessionId(unit: Extract<SegmentUnit, { kind: "prompt" }>): string {
  for (const seg of unit.segments) {
    if (seg.kind === "subagent_session") {
      return trimBlankEdges(seg.text);
    }
  }
  return "";
}

function withSessionMarker(
  unit: Extract<SegmentUnit, { kind: "prompt" }>,
  sessionId: string,
): Segment[] {
  if (!sessionId || getUnitSessionId(unit)) {
    return unit.segments;
  }
  const out = [...unit.segments];
  let insertAt = out.length;
  while (insertAt > 0 && isBlankTextSegment(out[insertAt - 1])) {
    insertAt -= 1;
  }
  out.splice(insertAt, 0, { kind: "subagent_session", text: sessionId });
  return out;
}

function isInvisibleUnit(unit: SegmentUnit): boolean {
  return (
    unit.kind === "other" &&
    unit.segments.every((seg) => isBlankTextSegment(seg))
  );
}

function buildSegmentUnits(segments: Segment[]): SegmentUnit[] {
  const units: SegmentUnit[] = [];
  for (let i = 0; i < segments.length; i += 1) {
    const seg = segments[i];
    if (seg.kind !== "prompt") {
      units.push({ kind: "other", segments: [seg] });
      continue;
    }
    const promptSegments: Segment[] = [seg];
    let j = i + 1;
    while (j < segments.length && isBlankTextSegment(segments[j])) {
      promptSegments.push(segments[j]);
      j += 1;
    }
    if (j < segments.length && segments[j].kind === "subagent_session") {
      promptSegments.push(segments[j]);
      j += 1;
      while (j < segments.length && isBlankTextSegment(segments[j])) {
        promptSegments.push(segments[j]);
        j += 1;
      }
    }
    units.push({
      kind: "prompt",
      segments: promptSegments,
      exploreState: getExplorePromptState(seg.text),
    });
    i = j - 1;
  }
  return units;
}

function normalizeToolSegments(text: string): Segment[] {
  const units = buildSegmentUnits(splitSteps(text));
  const normalized: Segment[] = [];
  for (let i = 0; i < units.length; i += 1) {
    const unit = units[i];
    if (unit.kind !== "prompt" || !unit.exploreState) {
      normalized.push(...unit.segments);
      continue;
    }
    let j = i;
    const run: Extract<SegmentUnit, { kind: "prompt" }>[] = [];
    while (j < units.length) {
      const candidate = units[j];
      if (candidate.kind === "prompt" && candidate.exploreState) {
        run.push(candidate);
        j += 1;
        continue;
      }
      if (isInvisibleUnit(candidate)) {
        j += 1;
        continue;
      }
      if (candidate.kind !== "prompt" || !candidate.exploreState) {
        break;
      }
    }
    const hasRunning = run.some((item) => item.exploreState === "running");
    const hasCompleted = run.some((item) => item.exploreState === "completed");
    if (hasRunning && hasCompleted) {
      const settled =
        [...run].reverse().find((item) => item.exploreState === "completed") ??
        run[run.length - 1];
      const sessionId =
        run.map((item) => getUnitSessionId(item)).find(Boolean) ?? "";
      normalized.push(...withSessionMarker(settled, sessionId));
      i = j - 1;
      continue;
    }
    normalized.push(...unit.segments);
  }
  return normalized;
}

/** Count how many tool-call prompt rows are present in a rendered tool block. */
export function countToolCalls(text: string): number {
  return normalizeToolSegments(text).reduce((count, seg) => {
    if (seg.kind !== "prompt") {
      return count;
    }
    return trimBlankEdges(seg.text) ? count + 1 : count;
  }, 0);
}

/** Whether a rendered tool block contains a sub-agent session with the given ID. */
export function textContainsSubAgentSession(text: string, id: string): boolean {
  if (!id) return false;
  for (const seg of normalizeToolSegments(text)) {
    if (seg.kind === "subagent_session" && trimBlankEdges(seg.text) === id) {
      return true;
    }
  }
  return false;
}

/** Return the last rendered tool-call description from a tool block, stripped
 *  of ANSI color codes so it can be reused as a plain-text activity title. */
export function getLastToolPromptBody(text: string): string | null {
  let lastBody: string | null = null;
  for (const seg of normalizeToolSegments(text)) {
    if (seg.kind !== "prompt") {
      continue;
    }
    const value = trimBlankEdges(seg.text);
    if (!value) {
      continue;
    }
    const { body } = splitPromptBullet(value);
    const plain = stripAnsi(body).trim();
    if (plain) {
      lastBody = plain;
    }
  }
  return lastBody;
}

const SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";

function SpinnerChar() {
  const [i, setI] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setI((n) => (n + 1) % SPINNER_CHARS.length), 100);
    return () => clearInterval(id);
  }, []);
  return <span style={{ color: "var(--accent)", marginLeft: "0.5ch", verticalAlign: "-2px" }}>{SPINNER_CHARS[i]}</span>;
}

/** Render collapsible execution steps, isolating command output blocks. */
export function StepsView({
  text,
  running,
  trailingStatusText,
}: {
  text: string;
  running?: boolean;
  trailingStatusText?: string;
}) {
  const segments = normalizeToolSegments(text);

  const onPathPreview = useCallback(async (path: string) => {
    const api = hostApi();
    if (!api?.browser_overlay_preview_path) return;
    void api.browser_overlay_preview_path(path);
  }, []);

  // Index of the last segment that carries visible content, so a diff block
  // auto-collapses once another tool/output block follows it (the "collapse the
  // previously-expanded diff when the next tool runs" behavior) while the most
  // recent diff stays expanded.
  let lastPromptIdx = -1;
  segments.forEach((seg, i) => {
    if (seg.kind === "prompt") {
      lastPromptIdx = i;
    }
  });
  // A "cmd"/"diff" segment that directly follows a "prompt" segment (the
  // "• Ran ..." line) is rendered as a toggle appended to that line, so the
  // expand/collapse control sits at the end of the tool-call description
  // instead of in its own header.
  const consumed = new Set<number>();
  return (
    <div className="activity-steps">
      {segments.map((seg, index) => {
        if (consumed.has(index)) {
          return null;
        }
        const value = trimBlankEdges(seg.text);
        if (!value) {
          return null;
        }
        if (seg.kind === "prompt") {
          const { bullet, body } = splitPromptBullet(value);
          // Find the next command-output, diff, or subagent_session block
          // that belongs to this prompt, skipping blank/text segments and
          // stopping at the next prompt if nothing is found.
          let cmdPayload = "";
          let diffIndices: number[] = [];
          let subagentSessionId = "";
          for (let j = index + 1; j < segments.length; j += 1) {
            if (segments[j].kind === "prompt") {
              break;
            }
            if (!trimBlankEdges(segments[j].text)) {
              continue;
            }
            if (segments[j].kind === "text") {
              continue;
            }
            if (segments[j].kind === "cmd") {
              // Consume every cmd segment attached to this prompt so
              // orphaned cmd blocks never render outside the prompt's
              // expand-collapse boundary.
              consumed.add(j);
              cmdPayload = trimBlankEdges(segments[j].text);
              continue;
            }
            if (segments[j].kind === "diff") {
              diffIndices.push(j);
              continue;
            }
            if (segments[j].kind === "subagent_session") {
              subagentSessionId = trimBlankEdges(segments[j].text);
              consumed.add(j);
              continue;
            }
            break;
          }
          const diffPayloads = diffIndices
            .map((i) => trimBlankEdges(segments[i].text))
            .filter(Boolean);
          diffIndices.forEach((i) => consumed.add(i));
          const isBrowserPreview = /browser_preview/i.test(body);
          return (
              <PromptWithAttachment
                key={index}
                bullet={bullet}
                body={body}
                cmdPayload={cmdPayload}
                diffPayloads={diffPayloads}
                subagentSessionId={subagentSessionId}
              defaultExpanded={false}
              onPathPreview={isBrowserPreview ? onPathPreview : undefined}
              running={running && index === lastPromptIdx}
              trailingStatusText={index === lastPromptIdx ? trailingStatusText : undefined}
            />
          );
        }
        if (seg.kind === "diff") {
          return (
            <DiffStep key={index} payload={value} defaultExpanded={false} />
          );
        }
        if (seg.kind === "cmd") {
          return null;
        }
        if (seg.kind === "subagent_session") {
          // This segment contains the session ID for a sub-agent call.
          // It's consumed by the prompt segment that precedes it.
          return null;
        }
        return (
          <div className="step-text" key={index}>
            <AnsiText text={handleCarriageReturn(value)} />
          </div>
        );
      })}
    </div>
  );
}

/** A "• Ran ..." prompt line. When it carries command output or an apply_patch
 *  diff, the expand/collapse chevron is appended to the end of the line and
 *  the payload renders below when open. Command output defaults to collapsed.
 *  For sub-agent calls, a ">" button is shown to enter the session viewer. */
function PromptWithAttachment({
  bullet,
  body,
  cmdPayload,
  diffPayloads,
  subagentSessionId,
  defaultExpanded,
  onPathPreview,
  running,
  trailingStatusText,
}: {
  bullet: string;
  body: string;
  cmdPayload: string;
  diffPayloads: string[];
  subagentSessionId: string;
  defaultExpanded: boolean;
  onPathPreview?: (path: string) => void;
  running?: boolean;
  trailingStatusText?: string;
}) {
  const { enterSubAgentSession, pendingExpandSubAgentId } = useApp();
  const hasCmd = !!cmdPayload;
  const diffs: DiffPayload[] = diffPayloads
    .map((p) => {
      try { return JSON.parse(p) as DiffPayload; } catch { return null; }
    })
    .filter((d): d is DiffPayload => d !== null && (d.diffRows?.length ?? 0) > 0);
  const hasDiff = diffs.length > 0;
  const hasAttachment = hasCmd || hasDiff;
  const isSubAgent = !!subagentSessionId;
  const shouldAutoExpand = isSubAgent && subagentSessionId === pendingExpandSubAgentId;
  const [expanded, setExpanded] = useState(hasDiff ? defaultExpanded : shouldAutoExpand);

  // Syntax-highlight file content (read tool output, or a shell command that
  // dumped a file) when we can resolve a language from the tool-call prompt.
  const { lang, lineNumbers } = resolveToolOutputLang(body, cmdPayload);
  const syntaxNode = lang
    ? SyntaxOutput({ text: cmdPayload, lang, lineNumbers })
    : null;

  const handleSubAgentClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    void enterSubAgentSession(subagentSessionId);
  };

  if (!hasAttachment && !isSubAgent) {
    return (
      <div className="cmd-prompt">
        <span className="cmd-prompt-bullet">
          <AnsiText text={bullet} />
        </span>
        <HoverTooltip content={<span style={{ fontFamily: "inherit", whiteSpace: "pre-wrap" }}>{stripAnsi(body)}</span>}>
          <span className="cmd-prompt-body">
            <AnsiText text={body} onPathPreview={onPathPreview} />
            {running && <SpinnerChar />}
            {trailingStatusText && (
              <span className="tool-inline-working">
                <span className="activity-text marquee">{trailingStatusText}</span>
              </span>
            )}
          </span>
        </HoverTooltip>
      </div>
    );
  }

  if (isSubAgent && !hasAttachment) {
    return (
      <div
        className="cmd-prompt has-attachment"
        role="button"
        tabIndex={0}
        title="View sub-agent session"
        onClick={handleSubAgentClick}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            handleSubAgentClick(e as unknown as React.MouseEvent);
          }
        }}
      >
        <span className="cmd-prompt-bullet">
          <AnsiText text={bullet} />
        </span>
        <HoverTooltip content={<span style={{ fontFamily: "inherit", whiteSpace: "pre-wrap" }}>{stripAnsi(body)}</span>}>
          <span className="cmd-prompt-body">
            <AnsiText text={body} onPathPreview={onPathPreview} />
            {running && <SpinnerChar />}
            {trailingStatusText && (
              <span className="tool-inline-working">
                <span className="activity-text marquee">{trailingStatusText}</span>
              </span>
            )}
          </span>
          <span className="cmd-prompt-diff-toggle subagent-view-btn">
            <Icon name="chevron" size={14} className="chevron" />
          </span>
        </HoverTooltip>
      </div>
    );
  }

  return (
    <>
      <div
        className="cmd-prompt has-attachment"
        role="button"
        tabIndex={0}
        title={isSubAgent ? "View sub-agent session" : expanded ? "Collapse output" : "Expand output"}
        onClick={isSubAgent ? handleSubAgentClick : () => setExpanded((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            if (isSubAgent) {
              handleSubAgentClick(e as unknown as React.MouseEvent);
            } else {
              setExpanded((v) => !v);
            }
          }
        }}
        {...{ "aria-expanded": expanded }}
      >
        <span className="cmd-prompt-bullet">
          <AnsiText text={bullet} />
        </span>
        <HoverTooltip content={<span style={{ fontFamily: "inherit", whiteSpace: "pre-wrap" }}>{stripAnsi(body)}</span>}>
          <span className="cmd-prompt-body">
            <AnsiText text={body} onPathPreview={onPathPreview} />
            {running && <SpinnerChar />}
          </span>
          {isSubAgent && (
            <span className="cmd-prompt-diff-toggle subagent-view-btn">
              <Icon name="chevron" size={14} className="chevron" />
            </span>
          )}
          <span className="cmd-prompt-diff-toggle">
            <Icon name="chevron" size={14} className={`chevron ${expanded ? "open" : ""}`} />
          </span>
        </HoverTooltip>
      </div>
      {!isSubAgent && expanded && hasCmd && syntaxNode}
      {!isSubAgent && expanded && hasCmd && !syntaxNode && (() => {
        const tw = getLongestTableBorderWidth(cmdPayload);
        const cleaned = handleCarriageReturn(cmdPayload);
        return (
          <div className="cmd-output" style={tw ? { overflowX: "auto" } : undefined}>
            {tw ? (
              <div style={{ width: `${tw + 2}ch`, wordBreak: "normal" }}>
                <AnsiText text={cleaned} />
              </div>
            ) : (
              <AnsiText text={cleaned} />
            )}
          </div>
        );
      })()}
      {!isSubAgent && expanded && hasDiff && (
        <div className="diff-files-container">
          {diffs.map((d, i) => {
            const rows = d.diffRows ?? [];
            const fname = (d.file || "").split(/[\\/]/).pop() || d.file || "";
            const added = countByType(rows, ["add", "change"]);
            const deleted = countByType(rows, ["del", "change"]);
            return (
              <div className="diff-step" key={i}>
                <div className="diff-step-header">
                  <span className="diff-step-title">{fname}</span>
                  <span className="diff-step-stats">
                    <span className="file-change-added">+{added}</span>
                    {" "}
                    <span className="file-change-deleted">-{deleted}</span>
                  </span>
                </div>
                <DiffPreview rows={rows} lang={langFromPath(d.file)} />
              </div>
            );
          })}
        </div>
      )}
      {trailingStatusText && (
        <span className="tool-inline-working">
          <span className="activity-text marquee">{trailingStatusText}</span>
        </span>
      )}
    </>
  );
}



interface DiffPayload {
  file?: string;
  diffRows?: DiffRow[];
}

/** A collapsible apply_patch change-preview block in the transcript. Defaults
 *  to expanded for the most recent diff; collapses automatically once a later
 *  tool/output block follows it. The user can always toggle it manually. */
function DiffStep({
  payload,
  defaultExpanded,
}: {
  payload: string;
  defaultExpanded: boolean;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  let parsed: DiffPayload | null = null;
  try {
    parsed = JSON.parse(payload) as DiffPayload;
  } catch {
    parsed = null;
  }
  const rows = parsed?.diffRows ?? [];
  if (rows.length === 0) {
    return null;
  }
  const fileName = (parsed?.file || "").split(/[\\/]/).pop() || parsed?.file || "";
  const added = countByType(rows, ["add", "change"]);
  const deleted = countByType(rows, ["del", "change"]);
  return (
    <div className="diff-step">
      <button
        type="button"
        className="diff-step-header"
        onClick={() => setExpanded((v) => !v)}
      >
        <span className={`diff-step-chevron ${expanded ? "open" : ""}`}>▸</span>
        <span className="diff-step-title">{fileName}</span>
        <span className="diff-step-stats">
          <span className="file-change-added">+{added}</span>
          {" "}
          <span className="file-change-deleted">-{deleted}</span>
        </span>
      </button>
      {expanded ? (
        <DiffPreview rows={rows} lang={langFromPath(parsed?.file)} />
      ) : null}
    </div>
  );
}

function countByType(rows: DiffRow[], types: string[]): number {
  return rows.filter((r) => types.includes(r.type)).length;
}
