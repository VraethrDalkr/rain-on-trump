// Talks to the backend: base URL, one request at a time, last good answer.

const PROD_API = 'https://rain-api.fly.dev';
const TIMEOUT_MS = 15000;
const LAST_KEY = 'lastAnswer';

/** API base for a page location (localhost / LAN dev → :8000 on that host). */
export function apiBaseFor(loc) {
  if (loc.hostname === 'rain-api.fly.dev') return loc.origin;
  if (/^(localhost|127\.|10\.|192\.168\.)/.test(loc.hostname)) {
    return `${loc.protocol}//${loc.hostname}:8000`;
  }
  return PROD_API;
}

/** True when a response body is an answer we can show (a 503 may still be). */
export function usablePayload(ok, data) {
  const shaped = Boolean(data) && typeof data === 'object' && 'precipitating' in data;
  return ok ? shaped : shaped && Boolean(data.error);
}

/** Last good payload and when it arrived, or null. */
export function lastAnswer() {
  try {
    const raw = JSON.parse(localStorage.getItem(LAST_KEY) || 'null');
    return raw && raw.data ? { data: raw.data, at: new Date(raw.at) } : null;
  } catch {
    return null;
  }
}

let current = null;

/**
 * Fetch /is_it_raining.json. The newest call wins: a previous request still
 * in flight is aborted, and its caller gets ``{superseded: true}``.
 *
 * @returns {Promise<{data: object}|{data: null, error: Error}|{superseded: true}>}
 */
export async function fetchAnswer() {
  if (current) current.abort();
  const ctl = new AbortController();
  current = ctl;
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${apiBaseFor(location)}/is_it_raining.json`, {
      cache: 'no-store',
      signal: ctl.signal,
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (current !== ctl) return { superseded: true };
    if (!usablePayload(res.ok, data)) throw new Error(`API error ${res.status}`);
    try {
      localStorage.setItem(LAST_KEY, JSON.stringify({ data, at: new Date().toISOString() }));
    } catch {
      // storage full or blocked: offline fallback just won't have this answer
    }
    return { data };
  } catch (error) {
    if (current !== ctl) return { superseded: true };
    return { data: null, error };
  } finally {
    clearTimeout(timer);
    if (current === ctl) current = null;
  }
}
