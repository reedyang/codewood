import "@testing-library/jest-dom/vitest";

// jsdom does not always provide localStorage (especially in older versions or
// certain environments), and the app's own code depends on it.  Use the same
// in-memory fallback that main.tsx uses for WebKitGTK.
import "../utils/ensureStorage";

// jsdom does not implement HTMLCanvasElement, but @xterm/xterm tries to
// create a 2-D canvas context during module initialisation.  Provide a
// minimal stub so the import does not spew "Not implemented" warnings.
HTMLCanvasElement.prototype.getContext = function () {
  return null;
};
