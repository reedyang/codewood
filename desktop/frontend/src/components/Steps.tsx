import { useState, useCallback } from "react";
import { AnsiText } from "./Ansi";
import { hostApi } from "../utils/hostApi";
import { DiffPreview, langFromPath } from "./DiffPreview";
import { Icon } from "./Icon";
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

type SegKind = "text" | "cmd" | "prompt" | "diff";
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

/** Render collapsible execution steps, isolating command output blocks. */
export function StepsView({ text }: { text: string }) {
  const segments = splitSteps(text);
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
  // A "diff" segment that directly follows a "prompt" segment (the
  // "• Ran apply_patch ..." line) is rendered as a toggle appended to that
  // line, so the expand/collapse control sits at the end of the tool-call
  // description instead of in its own header.
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
          // Find the next non-blank segment; if it is a diff, fuse it.
          let diffIdx = -1;
          for (let j = index + 1; j < segments.length; j += 1) {
            if (!trimBlankEdges(segments[j].text)) {
              continue;
            }
            if (segments[j].kind === "diff") {
              diffIdx = j;
            }
            break;
          }
          const diffPayload = diffIdx >= 0 ? trimBlankEdges(segments[diffIdx].text) : "";
          if (diffPayload) {
            consumed.add(diffIdx);
          }
          return (
            <PromptWithDiff
              key={index}
              bullet={bullet}
              body={body}
              diffPayload={diffPayload}
              defaultExpanded={diffIdx === lastContentIdx}
            />
          );
        }
        if (seg.kind === "diff") {
          return (
            <DiffStep key={index} payload={value} defaultExpanded={index === lastContentIdx} />
          );
        }
        return (
          <div className={seg.kind === "cmd" ? "cmd-output" : "step-text"} key={index}>
            <AnsiText text={value} />
          </div>
        );
      })}
    </div>
  );
}

/** A "• Ran ..." prompt line. When it carries an apply_patch diff, the
 *  expand/collapse chevron is appended to the end of the line (matching the
 *  "Worked for" activity toggle icon) and the diff renders below when open. */
function PromptWithDiff({
  bullet,
  body,
  diffPayload,
  defaultExpanded,
}: {
  bullet: string;
  body: string;
  diffPayload: string;
  defaultExpanded: boolean;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const onPathPreview = useCallback(async (path: string) => {
    const api = hostApi();
    if (!api?.browser_overlay_preview_path) return;
    const result = await Promise.resolve(api.browser_overlay_preview_path(path));
    if (result && result.ok && result.url) {
      window.dispatchEvent(
        new CustomEvent("codewood:browser-open-preview", {
          detail: { url: result.url },
        }),
      );
    }
  }, []);
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
  if (!hasDiff) {
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
  return (
    <>
      <div
        className="cmd-prompt has-diff"
        role="button"
        tabIndex={0}
        title={expanded ? "Collapse diff" : "Expand diff"}
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
        {...{ "aria-expanded": expanded }}
      >
        <span className="cmd-prompt-bullet">
          <AnsiText text={bullet} />
        </span>
        <span className="cmd-prompt-body">
          <AnsiText text={body} onPathPreview={onPathPreview} />
          <span className="cmd-prompt-diff-toggle">
            <Icon name="chevron" size={14} className={`chevron ${expanded ? "open" : ""}`} />
          </span>
        </span>
      </div>
      {expanded && <DiffPreview rows={rows} lang={langFromPath(parsed?.file)} />}
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
