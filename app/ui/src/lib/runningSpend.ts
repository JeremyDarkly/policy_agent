import { useSyncExternalStore } from "react";

/**
 * The demo user's running model spend in cents, as the backend's spend ledger
 * reports it (metrics.runningSpendCents on each chat response). The backend
 * puts the same number on every AI Config evaluation as `running_spend_cents`,
 * so a targeting rule can switch the user to a cheaper model above a threshold.
 *
 * Shared between the chat widget (which shows it) and the header's user menu
 * (which resets it), like the demo-user store in demoUser.ts.
 */
const spendByUser = new Map<string, number>();
const listeners = new Set<() => void>();
let version = 0;

const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
};

const notify = () => {
  version += 1;
  listeners.forEach((l) => l());
};

export function setRunningSpend(userKey: string, cents: number) {
  spendByUser.set(userKey, cents);
  notify();
}

/** Load a user's total from the backend (e.g. after switching demo user). */
export async function refreshRunningSpend(userKey: string) {
  try {
    const res = await fetch(`/api/spend/${encodeURIComponent(userKey)}`);
    if (res.ok) {
      const data = await res.json();
      setRunningSpend(userKey, Number(data.runningSpendCents) || 0);
    }
  } catch {
    // The backend may be down; leave the last known value.
  }
}

/** Set the user's spend back to 0 on the backend, so the demo can be repeated. */
export async function resetRunningSpend(userKey: string) {
  const res = await fetch("/api/spend/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ userKey }),
  });
  if (!res.ok) throw new Error(`reset failed: HTTP ${res.status}`);
  setRunningSpend(userKey, 0);
}

export function useRunningSpend(userKey: string): number | undefined {
  useSyncExternalStore(subscribe, () => version, () => version);
  return spendByUser.get(userKey);
}
