// Deploy detection. The backend sends, in every WS snapshot, the hashed asset
// set each page's built index.html links (backend/build.py); a page whose own
// document links a different set is running an outdated bundle.

/** The built assets this document linked, in the backend's format. */
export function ownBuild(): string {
  const els = document.querySelectorAll<HTMLScriptElement | HTMLLinkElement>(
    'script[src], link[rel="stylesheet"][href], link[rel="modulepreload"][href]',
  );
  const paths = Array.from(els)
    .map((el) => new URL("src" in el ? el.src : el.href, location.href).pathname)
    .filter((path) => path.startsWith("/assets/"));
  return Array.from(new Set(paths)).sort().join("|");
}

const RELOAD_KEY = "edge-ticker:reloaded-for";

/** True when the snapshot's build differs from ours. Always false outside a
 *  production bundle (the Vite dev server serves source, never /assets/). */
export function isOutdated(current: string | null | undefined): boolean {
  if (!import.meta.env.PROD || !current) return false;
  if (current === ownBuild()) {
    try {
      sessionStorage.removeItem(RELOAD_KEY);
    } catch {
      /* storage unavailable: the loop guard just can't persist */
    }
    return false;
  }
  return true;
}

/** Reload onto `current` — at most once per target build, so a server that
 *  keeps handing out a stale index.html can't put the page in a reload loop. */
export function reloadOnto(current: string): boolean {
  try {
    if (sessionStorage.getItem(RELOAD_KEY) === current) return false;
    sessionStorage.setItem(RELOAD_KEY, current);
  } catch {
    /* no storage: reload anyway; the snapshot after it will match */
  }
  location.reload();
  return true;
}
