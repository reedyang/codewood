import { AnsiText } from "./Ansi";

// Private-use sentinels wrapping raw command output, emitted by the backend in
// GUI mode (kept in sync with src/core/console_utils.py). They let us render
// command output in its own padded node so the indent survives soft-wrapping.
const CMD_OUTPUT_BEGIN = "\uE000";
const CMD_OUTPUT_END = "\uE001";

type Segment = { kind: "text" | "cmd"; text: string };

function splitSteps(text: string): Segment[] {
  const segments: Segment[] = [];
  let buf = "";
  let inCmd = false;
  for (const ch of text) {
    if (!inCmd && ch === CMD_OUTPUT_BEGIN) {
      if (buf) {
        segments.push({ kind: "text", text: buf });
      }
      buf = "";
      inCmd = true;
      continue;
    }
    if (inCmd && ch === CMD_OUTPUT_END) {
      if (buf) {
        segments.push({ kind: "cmd", text: buf });
      }
      buf = "";
      inCmd = false;
      continue;
    }
    buf += ch;
  }
  if (buf) {
    segments.push({ kind: inCmd ? "cmd" : "text", text: buf });
  }
  return segments;
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
        return (
          <div className={seg.kind === "cmd" ? "cmd-output" : "step-text"} key={index}>
            <AnsiText text={value} />
          </div>
        );
      })}
    </div>
  );
}
