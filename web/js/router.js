/* Hash routing.

   Hash, not the history API, because this app is served as static files by
   FastAPI and also opens from a plain checkout — a deep link has to work
   without a server-side rewrite rule.

   `isCurrent()` exists because every view is async: if someone clicks away
   mid-fetch, the stale render must not paint over the new screen. */

export function parseHash(hash = location.hash) {
  const raw = (hash || "").replace(/^#\/?/, "");
  const [path, qs] = raw.split("?");
  const params = {};
  if (qs) for (const [k, v] of new URLSearchParams(qs)) params[k] = v;
  return { path: path || "dashboard", params };
}

export function createRouter(routes, onNavigate) {
  let token = 0;
  let current = null;

  function run() {
    const { path, params } = parseHash();
    const route = routes.includes(path) ? path : "dashboard";
    const mine = ++token;
    current = route;
    onNavigate(route, params, () => mine === token);
  }

  window.addEventListener("hashchange", run);
  return { start: run, current: () => current };
}
