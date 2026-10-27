import { useSyncExternalStore } from "react";

/**
 * Demo "fake login" users for the ToggleBank UI.
 *
 * Each preset drives LaunchDarkly user-key targeting so the same AI Configs
 * can resolve to different variations depending on who is "logged in":
 *   - commercial  -> user_key "<name>-commercial-plan" (a regular banking customer)
 *   - internal    -> user_key "<name>-internal-dev"    (an internal/employee user)
 * where <name> is the demo user's name as a slug, e.g. "jeremy-mcgee".
 *
 * The fields below are forwarded to POST /api/chat and become the LaunchDarkly
 * context attributes (see backend create_user_profile). `domain: "togglebank"`
 * keeps the context coherent with the banking brand.
 */
// Demo user's display name, sourced from the environment (VITE_DEMO_USER_NAME)
// so no personal name is hardcoded. Falls back to a neutral placeholder.
const DEMO_USER_NAME = import.meta.env.VITE_DEMO_USER_NAME || "Demo User";
// User keys derive from the same name, so each presenter targets their own keys
const KEY_PREFIX = DEMO_USER_NAME.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

export const DEMO_USERS = {
  commercial: {
    id: "commercial",
    name: DEMO_USER_NAME,
    badge: "Customer",
    userName: DEMO_USER_NAME,
    userKey: `${KEY_PREFIX}-commercial-plan`,
    userType: "customer",
    role: "customer",
    plan: "commercial",
    domain: "togglebank",
    policyId: "ACC-90021",
    coverageType: "Everyday Current Account",
  },
  internal: {
    id: "internal",
    name: "Dev Mode",
    badge: "Internal",
    userName: DEMO_USER_NAME,
    userKey: `${KEY_PREFIX}-internal-dev`,
    userType: "internal",
    role: "employee",
    plan: "internal",
    domain: "togglebank",
    policyId: "ACC-INT-001",
    coverageType: "Premier Current Account",
  },
} as const;

export type DemoUserId = keyof typeof DEMO_USERS;
export type DemoUser = (typeof DEMO_USERS)[DemoUserId];

// Module-level store so every component that calls useDemoUser() shares the
// same selection and re-renders when it changes — no React context/provider
// required (none is mounted in App.tsx).
let currentId: DemoUserId = "commercial";
const listeners = new Set<() => void>();

const subscribe = (cb: () => void) => {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
};

const getSnapshot = () => currentId;

export function setDemoUser(id: DemoUserId) {
  if (id in DEMO_USERS && id !== currentId) {
    currentId = id;
    listeners.forEach((l) => l());
  }
}

export function useDemoUser() {
  const id = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return { user: DEMO_USERS[id], setUser: setDemoUser };
}
