import "@testing-library/jest-dom/vitest";

// jsdom does not implement HTMLCanvasElement, but @xterm/xterm tries to
// create a 2-D canvas context during module initialisation.  Provide a
// minimal stub so the import does not spew "Not implemented" warnings.
HTMLCanvasElement.prototype.getContext = function () {
  return null;
};
