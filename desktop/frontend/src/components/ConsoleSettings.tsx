import { useEffect, useState } from "react";
import { useApp } from "../state/AppContext";

const BUFFER_MIN = 100;
const BUFFER_MAX = 100000;

// Common monospace fonts offered in the terminal font dropdown. The empty
// value means "use the built-in default stack".
const FONT_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Default" },
  { value: "Consolas", label: "Consolas" },
  { value: "Cascadia Mono", label: "Cascadia Mono" },
  { value: "Cascadia Code", label: "Cascadia Code" },
  { value: "Courier New", label: "Courier New" },
  { value: "Lucida Console", label: "Lucida Console" },
  { value: "DejaVu Sans Mono", label: "DejaVu Sans Mono" },
  { value: "Menlo", label: "Menlo" },
  { value: "Monaco", label: "Monaco" },
  { value: "monospace", label: "monospace" },
];

/** Embedded-console options: font family and scrollback buffer size. Values
 *  persist to the GUI-only config and apply live to open console tabs. */
export function ConsoleSettings() {
  const { t, consoleOptions, setConsoleOptions } = useApp();
  const [fontFamily, setFontFamily] = useState(consoleOptions.fontFamily);
  const [bufferLines, setBufferLines] = useState(consoleOptions.bufferLines);

  // Resync the local draft when the server value changes (e.g. another window).
  useEffect(() => {
    setFontFamily(consoleOptions.fontFamily);
  }, [consoleOptions.fontFamily]);
  useEffect(() => {
    setBufferLines(consoleOptions.bufferLines);
  }, [consoleOptions.bufferLines]);

  const commit = (next: { fontFamily?: string; bufferLines?: number }) => {
    const clampedBuffer = Math.max(
      BUFFER_MIN,
      Math.min(BUFFER_MAX, Math.round(next.bufferLines ?? bufferLines)),
    );
    void setConsoleOptions({
      fontFamily: (next.fontFamily ?? fontFamily).trim(),
      bufferLines: clampedBuffer,
    });
  };

  return (
    <div className="settings-page">
      <h2 className="settings-page-title">{t("settings.page.console")}</h2>

      <div className="setting-row">
        <label htmlFor="console-font">{t("settings.console.font")}</label>
        <div className="setting-control">
          <select
            id="console-font"
            className="select"
            aria-label={t("settings.console.font")}
            value={fontFamily}
            onChange={(e) => {
              setFontFamily(e.target.value);
              commit({ fontFamily: e.target.value });
            }}
          >
            {/* Preserve a custom (manually configured) value that isn't one of
                the presets so it stays selectable. */}
            {fontFamily &&
              !FONT_OPTIONS.some((o) => o.value === fontFamily) && (
                <option value={fontFamily}>{fontFamily}</option>
              )}
            {FONT_OPTIONS.map((o) => (
              <option key={o.value || "default"} value={o.value}>
                {o.value === "" ? t("settings.console.fontDefault") : o.label}
              </option>
            ))}
          </select>
          <p className="setting-hint">{t("settings.console.fontHint")}</p>
        </div>
      </div>

      <div className="setting-row">
        <label htmlFor="console-buffer">{t("settings.console.buffer")}</label>
        <div className="setting-control">
          <input
            id="console-buffer"
            type="number"
            className="text-input"
            min={BUFFER_MIN}
            max={BUFFER_MAX}
            step={100}
            value={bufferLines}
            onChange={(e) => setBufferLines(Number(e.target.value) || BUFFER_MIN)}
            onBlur={() => commit({ bufferLines })}
          />
          <p className="setting-hint">{t("settings.console.bufferHint")}</p>
        </div>
      </div>
    </div>
  );
}
