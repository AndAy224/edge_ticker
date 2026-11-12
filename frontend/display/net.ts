// fetch() with a deadline. A backend that accepts the connection and then never
// answers leaves a plain fetch pending for good — on a page that runs for
// weeks that is a latched flag (self-heal) or one of Chromium's six per-host
// connections gone.

/** Long enough for the detail proxies' own upstream timeouts (15-20 s), and
 *  no longer than a detail layer stays up (20 s): nothing later could be shown. */
export const DETAIL_TIMEOUT_MS = 20_000;

/** Rejects (AbortError) once `ms` pass without a complete response — callers
 *  treat that like any other failed fetch. The timer is deliberately not
 *  cleared when the headers arrive: the abort then also ends a body that
 *  stalls half-way, and is a no-op on a response already read. */
export function fetchTimeout(
  url: string,
  ms: number = DETAIL_TIMEOUT_MS,
  init: RequestInit = {},
): Promise<Response> {
  const controller = new AbortController();
  setTimeout(() => controller.abort(), ms);
  return fetch(url, { ...init, signal: controller.signal });
}
