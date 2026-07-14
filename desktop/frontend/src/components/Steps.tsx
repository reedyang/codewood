import { useState, useCallback } from "react";
import { AnsiText } from "./Ansi";
import { hostApi } from "../utils/hostApi";
import { DiffPreview, langFromPath } from "./DiffPreview";
import { SyntaxOutput, resolveToolOutputLang } from "./SyntaxOutput";
import { Icon } from "./Icon";
import { useApp } from "../state/AppContext";
import type { DiffRow } from "../api/types";

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

type SegKind = "text" | "cmd" | "prompt" | "diff" | "subagent_session";
type Segment = { kind: SegKind; text: string };

function splitSteps(text: string): Segment[] {
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

/** Count how many tool-call prompt rows are present in a rendered tool block. */
export function countToolCalls(text: string): number {
  return splitSteps(text).reduce((count, seg) => {
    if (seg.kind !== "prompt") {
      return count;
    }
    return trimBlankEdges(seg.text) ? count + 1 : count;
  }, 0);
}

/** Whether a rendered tool block contains a sub-agent session with the given ID. */
export function textContainsSubAgentSession(text: string, id: string): boolean {
  if (!id) return false;
  for (const seg of splitSteps(text)) {
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
  for (const seg of splitSteps(text)) {
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

/** Render collapsible execution steps, isolating command output blocks. */
export function StepsView({ text }: { text: string }) {
  const segments = splitSteps(text);

  const onPathPreview = useCallback(async (path: string) => {
    const api = hostApi();
    if (!api?.browser_overlay_preview_path) return;
    void api.browser_overlay_preview_path(path);
  }, []);

  // Index of the last segment that carries visible content, so a diff block
  // auto-collapses once another tool/output block follows it (the "collapse the
  // previously-expanded diff when the next tool runs" behavior) while the most
  // recent diff stays expanded.
  let lastContentIdx = -1;
  segments.forEach((seg, i) => {
    if (trimBlankEdges(seg.text)) {
      lastContentIdx = i;
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
          // Find the next non-blank segment; if it is a command-output, diff,
          // or subagent_session block, fuse it into the command row.
          let cmdIdx = -1;
          let cmdPayload = "";
          let diffIdx = -1;
          let subagentSessionId = "";
          for (let j = index + 1; j < segments.length; j += 1) {
            if (!trimBlankEdges(segments[j].text)) {
              continue;
            }
            if (segments[j].kind === "cmd") {
              cmdIdx = j;
              cmdPayload = trimBlankEdges(segments[j].text);
            } else if (segments[j].kind === "diff") {
              diffIdx = j;
            } else if (segments[j].kind === "subagent_session") {
              subagentSessionId = trimBlankEdges(segments[j].text);
              consumed.add(j);
            }
            break;
          }
          if (cmdIdx >= 0 && cmdPayload) {
            consumed.add(cmdIdx);
          }
          const diffPayload = diffIdx >= 0 ? trimBlankEdges(segments[diffIdx].text) : "";
          if (diffPayload) {
            consumed.add(diffIdx);
          }
          const isBrowserPreview = /browser_preview/i.test(body);
          return (
            <PromptWithAttachment
              key={index}
              bullet={bullet}
              body={body}
              cmdPayload={cmdPayload}
              diffPayload={diffPayload}
              subagentSessionId={subagentSessionId}
              defaultExpanded={diffIdx === lastContentIdx}
              onPathPreview={isBrowserPreview ? onPathPreview : undefined}
            />
          );
        }
        if (seg.kind === "diff") {
          return (
            <DiffStep key={index} payload={value} defaultExpanded={index === lastContentIdx} />
          );
        }
        if (seg.kind === "cmd") {
          return <CmdOutputBlock key={index} text={value} />;
        }
        if (seg.kind === "subagent_session") {
          // This segment contains the session ID for a sub-agent call.
          // It's consumed by the prompt segment that precedes it.
          return null;
        }
        return (
          <div className="step-text" key={index}>
            <AnsiText text={value} />
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
  diffPayload,
  subagentSessionId,
  defaultExpanded,
  onPathPreview,
}: {
  bullet: string;
  body: string;
  cmdPayload: string;
  diffPayload: string;
  subagentSessionId: string;
  defaultExpanded: boolean;
  onPathPreview?: (path: string) => void;
}) {
  const { enterSubAgentSession, pendingExpandSubAgentId } = useApp();
  const hasCmd = !!cmdPayload;
  let parsed: DiffPayload | null = null;
  if (diffPayload) {
    try {
      parsed = JSON.parse(diffPayload) as DiffPayload;
    } catch {
      parsed = null;
    }
  }
  const rows = parsed?.diffRows ?? [];
  const hasDiff = rows.length > 0;
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
        <span className="cmd-prompt-body">
          <AnsiText text={body} onPathPreview={onPathPreview} />
        </span>
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
        <span className="cmd-prompt-body">
          <AnsiText text={body} onPathPreview={onPathPreview} />
          <span className="cmd-prompt-diff-toggle subagent-view-btn">
            <Icon name="chevron" size={14} className="chevron" />
          </span>
        </span>
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
        <span className="cmd-prompt-body">
          <AnsiText text={body} onPathPreview={onPathPreview} />
          {isSubAgent && (
            <span className="cmd-prompt-diff-toggle subagent-view-btn">
              <Icon name="chevron" size={14} className="chevron" />
            </span>
          )}
          <span className="cmd-prompt-diff-toggle">
            <Icon name="chevron" size={14} className={`chevron ${expanded ? "open" : ""}`} />
          </span>
        </span>
      </div>
      {!isSubAgent && expanded && hasCmd && (
        <div className="cmd-output">
          {syntaxNode ?? <AnsiText text={cmdPayload} />}
        </div>
      )}
      {!isSubAgent && expanded && hasDiff && <DiffPreview rows={rows} lang={langFromPath(parsed?.file)} />}
    </>
  );
}

function CmdOutputBlock({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="cmd-output-block">
      <button
        type="button"
        className="cmd-output-header"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        title={expanded ? "Collapse output" : "Expand output"}
      >
        <Icon name="chevron" size={14} className={`chevron ${expanded ? "open" : ""}`} />
      </button>
      {expanded && (
        <div className="cmd-output">
          <AnsiText text={text} />
        </div>
      )}
    </div>
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
          {countByType(rows, ["add", "change"])} + / {countByType(rows, ["del", "change"])} -
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
