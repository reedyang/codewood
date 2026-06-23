/**
 * Guarantee ``window.localStorage`` / ``window.sessionStorage`` exist.
 *
 * WebKitGTK (the Linux pywebview backend) disables Web Storage for pages
 * loaded over ``file://`` — ``window.localStorage`` is then ``undefined`` and
 * any access throws ``TypeError: undefined is not an object``, blanking the
 * app before React mounts. Windows' WebView2 allows it, so this only matters
 * on Linux, but the shim is harmless everywhere.
 *
 * When the native storage is missing or unusable (e.g. throws on access), we
 * install an in-memory fallback implementing the Storage interface. Prefs
 * persist for the session but reset on restart — acceptable degradation, and
 * far better than a white screen. This module must be imported before any code
 * that touches storage.
 */

function makeMemoryStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear() {
      map.clear();
    },
    getItem(key: string) {
      return map.has(key) ? (map.get(key) as string) : null;
    },
    key(index: number) {
      return Array.from(map.keys())[index] ?? null;
    },
    removeItem(key: string) {
      map.delete(key);
    },
    setItem(key: string, value: string) {
      map.set(key, String(value));
    },
  } as Storage;
}

function isUsable(storage: Storage | undefined | null): boolean {
  if (!storage) {
    return false;
  }
  try {
    const probe = "__cw_storage_probe__";
    storage.setItem(probe, "1");
    storage.removeItem(probe);
    return true;
  } catch {
    return false;
  }
}

function ensure(name: "localStorage" | "sessionStorage"): void {
  let usable = false;
  try {
    usable = isUsable(window[name]);
  } catch {
    usable = false;
  }
  if (usable) {
    return;
  }
  try {
    Object.defineProperty(window, name, {
      value: makeMemoryStorage(),
      configurable: true,
      writable: false,
    });
  } catch {
    // As a last resort, assign directly (older engines).
    try {
      (window as unknown as Record<string, Storage>)[name] = makeMemoryStorage();
    } catch {
      /* nothing else we can do */
    }
  }
}

ensure("localStorage");
ensure("sessionStorage");
