/**
 * shared/auth.ts — Bearer-token storage for the panel/admin APIs.
 *
 * The MOBIUS.HOME backend is Bearer-only (CSRF-immune by construction — no
 * ambient cookie a browser auto-attaches). Both RN apps hold an enrolled token
 * and send it as `Authorization: Bearer <token>`.
 *
 * WEB build: uses localStorage (present in react-native-web). NATIVE (iOS/
 * Android) will swap the two functions below for expo-secure-store — every other
 * consumer only calls getToken()/setToken(), so that port is two functions.
 * Guarded so it degrades to an in-memory token if no storage exists.
 */

const TOKEN_KEY = 'mobius_panel_token';

let memoryToken: string | null = null;

function storage(): Storage | null {
  try {
    // globalThis.localStorage exists on web; undefined on native.
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch {
    return null;
  }
}

/** The current Bearer token, or null if not enrolled/bootstrapped yet. */
export function getToken(): string | null {
  const s = storage();
  if (s) {
    return s.getItem(TOKEN_KEY);
  }
  return memoryToken;
}

/** Persist the Bearer token (returned once at enrollment/bootstrap). */
export function setToken(token: string): void {
  const s = storage();
  if (s) {
    s.setItem(TOKEN_KEY, token);
  } else {
    memoryToken = token;
  }
}

/** Clear the token (revoked / signed out). */
export function clearToken(): void {
  const s = storage();
  if (s) {
    s.removeItem(TOKEN_KEY);
  }
  memoryToken = null;
}

/** Authorization header for an authenticated request, or {} when unauthenticated. */
export function authHeader(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}
