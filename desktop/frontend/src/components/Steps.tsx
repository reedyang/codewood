import { AnsiText } from "./Ansi";

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

type SegKind = "text" | "cmd" | "prompt";
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
  return (
    <div className="activity-steps">
      {segments.map((seg, index) => {
        const value = trimBlankEdges(seg.text);
        if (!value) {
          return null;
        }
        if (seg.kind === "prompt") {
          const { bullet, body } = splitPromptBullet(value);
          return (
            <div className="cmd-prompt" key={index}>
              <span className="cmd-prompt-bullet">
                <AnsiText text={bullet} />
              </span>
              <span className="cmd-prompt-body">
                <AnsiText text={body} />
              </span>
            </div>
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
