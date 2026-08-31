/* The only place that talks to the server.

   Two rules:
   - Every failure becomes an ApiError carrying a message a finance user can
     act on. A raw "500 Internal Server Error" is never shown; the server's
     request_id is preserved so a real failure is still traceable.
   - Reads that are pure and expensive (evaluation, benchmark) are cached in
     memory for the session. */

export class ApiError extends Error {
  constructor(message, { status = 0, requestId = null, hint = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = requestId;
    this.hint = hint;
  }
}

const HINTS = {
  400: "Check that you supplied at least one file.",
  404: "That item no longer exists. Refresh and try again.",
  409: "This item changed since you opened it. Refresh to see its current state.",
  413: "The file is too large. Split it, or reduce the row count.",
  422: "The file could not be read. Check it is a CSV or JSON export.",
  429: "Too many requests in a row. Wait a moment and retry.",
  500: "Something failed on the server. The request id below identifies it in the logs.",
};

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    throw new ApiError("Cannot reach the server.", {
      hint: "Check the service is running, then retry.",
    });
  }

  if (res.status === 204) return null;

  let body = null;
  let parseFailed = false;
  const isJson = (res.headers.get("content-type") || "").includes("json");
  if (isJson) {
    try { body = await res.json(); } catch { parseFailed = true; }
  }

  if (!res.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : null;
    throw new ApiError(detail || `Request failed (${res.status})`, {
      status: res.status,
      requestId: (body && body.request_id) || res.headers.get("x-request-id"),
      hint: HINTS[res.status] || "",
    });
  }

  /* A 200 carrying something other than JSON is not a success — it is almost
     always a proxy, a login page, or a tunnel returning its own HTML. Returning
     null here let the views render "No runs yet", which tells a finance user
     their month is empty when in fact the app never reached the server. A wrong
     answer stated confidently is the one failure mode this product exists to
     avoid, so it has to be true of the product's own screens too. */
  if (!isJson || parseFailed) {
    throw new ApiError("The server returned something this app cannot read.", {
      status: res.status,
      requestId: res.headers.get("x-request-id"),
      hint: parseFailed
        ? "The response claimed to be JSON but was not valid. Check the service logs."
        : "Expected JSON. Something between this page and the API — a proxy or a "
          + "sign-in redirect — replaced the response.",
    });
  }
  return body;
}

/* The list endpoints degrade badly on a wrong-shaped 200: `list.length` on an
   object is undefined, which every view reads as "empty" and renders as a
   perfectly calm "nothing here yet". A one-line shape check turns that into an
   error the user can act on. */
function expectArray(name, value) {
  if (Array.isArray(value)) return value;
  throw new ApiError(`The server's ${name} response was not in the expected shape.`, {
    hint: "This usually means a version mismatch between the page and the API. "
        + "Reload; if it persists, the deployed build and this page disagree.",
  });
}

function expectPage(name, value) {
  if (value && Array.isArray(value.items)) return value;
  throw new ApiError(`The server's ${name} response was not in the expected shape.`, {
    hint: "This usually means a version mismatch between the page and the API. "
        + "Reload; if it persists, the deployed build and this page disagree.",
  });
}

const json = (data) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data ?? {}),
});

const _cache = new Map();
async function cached(key, fn) {
  if (!_cache.has(key)) _cache.set(key, fn());
  try {
    return await _cache.get(key);
  } catch (e) {
    _cache.delete(key);   // never cache a failure
    throw e;
  }
}
export const clearCache = (key) => (key ? _cache.delete(key) : _cache.clear());

export const api = {
  health:   () => request("/api/health"),
  datasets: async () => expectArray("datasets", await request("/api/datasets")),
  scenarios: async () => expectArray("scenarios", await request("/api/scenarios")),

  reconcile:      (dataset) => request("/api/reconcile", json({ dataset })),
  reconcileScenarios: () => request("/api/reconcile/scenarios", json({})),
  reconcileRazorpay:  (live = true) => request("/api/reconcile/razorpay", json({ live })),
  reconcileUpload: (formData) =>
    request("/api/reconcile/upload", { method: "POST", body: formData }),

  ingestPreview: (source, file) => {
    const fd = new FormData();
    fd.append("file", file);
    return request(`/api/ingest/preview?source=${encodeURIComponent(source)}`,
                   { method: "POST", body: fd });
  },

  exceptions: async (params = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== "" && v != null) q.set(k, v);
    }
    const qs = q.toString();
    return expectPage("worklist", await request(`/api/exceptions${qs ? `?${qs}` : ""}`));
  },
  exceptionsSummary: () => request("/api/exceptions/summary"),
  exception:  (id) => request(`/api/exceptions/${encodeURIComponent(id)}`),
  patchException: (id, payload) =>
    request(`/api/exceptions/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  addNote: (id, body, actor = "you") =>
    request(`/api/exceptions/${encodeURIComponent(id)}/notes`, json({ body, actor })),

  runs: async (limit = 50) =>
    expectArray("run history", await request(`/api/runs?limit=${limit}`)),
  run:  (id) => request(`/api/runs/${encodeURIComponent(id)}`),

  razorpayStatus: () => request("/api/razorpay/status"),

  // pure + expensive: worth caching for the session
  evaluation: () => cached("evaluation", () => request("/api/evaluation")),
  benchmark:  () => cached("benchmark",  () => request("/api/benchmark")),
};
