import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import type { IndexStatus } from "../api/types";

const POLL_MS = 500;

/**
 * Shared poller for the backend's /index-status endpoint.
 *
 * The status bar and the sidebar's workspace hover tips both need the same
 * (aggregated + per-workspace) index data, so the polling is kept in a
 * module-level singleton: the first subscriber starts a single interval and
 * every subscriber receives every update; the interval stops when the last
 * subscriber unmounts.
 */

let activeClient: ApiClient | null = null;
let subscribers = 0;
let timer: ReturnType<typeof setInterval> | null = null;
let latest: IndexStatus | null = null;

const listeners = new Set<(status: IndexStatus) => void>();

function pollOnce(): void {
  const client = activeClient;
  if (!client) return;
  try {
    client
      .fetchIndexStatus()
      .then((status) => {
        latest = status;
        listeners.forEach((cb) => cb(status));
      })
      .catch(() => {
        // Transient network hiccups: keep the last known status.
      });
  } catch {
    // Poller misconfigured (e.g. mocked client without fetchIndexStatus).
  }
}

function ensureStarted(client: ApiClient): void {
  activeClient = client;
  if (timer == null) {
    pollOnce();
    timer = setInterval(pollOnce, POLL_MS);
  } else if (latest) {
    const snapshot = latest;
    listeners.forEach((cb) => cb(snapshot));
  }
}

function ensureStopped(): void {
  if (subscribers <= 0 && timer != null) {
    clearInterval(timer);
    timer = null;
    activeClient = null;
  }
}

/** Subscribe to the shared /index-status poller. Returns the latest payload
 *  (null before the first successful fetch). */
export function useIndexStatus(client: ApiClient): IndexStatus | null {
  const [status, setStatus] = useState<IndexStatus | null>(latest);

  useEffect(() => {
    subscribers += 1;
    const onUpdate = (next: IndexStatus) => setStatus(next);
    listeners.add(onUpdate);
    ensureStarted(client);
    if (latest) {
      setStatus(latest);
    }
    return () => {
      listeners.delete(onUpdate);
      subscribers -= 1;
      ensureStopped();
    };
  }, [client]);

  return status;
}
