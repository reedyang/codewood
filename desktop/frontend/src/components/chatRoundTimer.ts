export function shouldShowRoundTimer({
  running,
  hasTools,
  hasAnswer,
  thinkingRunning,
}: {
  running: boolean;
  hasTools: boolean;
  hasAnswer: boolean;
  thinkingRunning: boolean;
}): boolean {
  if (hasTools) {
    return true;
  }
  if (!running) {
    return false;
  }
  // While a new round is still streaming raw thinking and has not produced any
  // visible answer/tool output yet, suppress the placeholder "Working" row.
  // Once visible content arrives (or the round finishes), the timer can show.
  if (thinkingRunning && !hasAnswer) {
    return false;
  }
  return true;
}
