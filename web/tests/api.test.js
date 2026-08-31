/* The client's error handling and the router.
 *
 * These two modules decide what a finance user is told when something goes
 * wrong. A silent failure here shows an empty worklist to someone whose month
 * is not empty, which is the one thing this product exists not to do.
 */
import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";

import { ApiError, api, clearCache } from "../js/api.js";
import { createRouter, parseHash } from "../js/router.js";

/* a fetch stand-in, so nothing here touches a network */
function stubFetch(handler) {
  globalThis.fetch = async (path, options) => handler(path, options);
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => clearCache());

describe("error mapping", () => {
  it("turns an unreachable server into an actionable message", async () => {
    stubFetch(() => { throw new TypeError("Failed to fetch"); });
    await assert.rejects(api.health(), (e) => {
      assert.ok(e instanceof ApiError);
      assert.match(e.message, /cannot reach/i);
      assert.match(e.hint, /running/i);
      return true;
    });
  });

  it("prefers the server's own detail over a status code", async () => {
    stubFetch(() => jsonResponse({ detail: "unknown dataset 'nope'" }, 404));
    await assert.rejects(api.health(), (e) => {
      assert.equal(e.message, "unknown dataset 'nope'");
      assert.equal(e.status, 404);
      return true;
    });
  });

  it("keeps the request id so a real failure stays traceable", async () => {
    stubFetch(() => jsonResponse({ detail: "internal error", request_id: "ab12cd34" }, 500));
    await assert.rejects(api.health(), (e) => {
      assert.equal(e.requestId, "ab12cd34");
      assert.match(e.hint, /request id/i);
      return true;
    });
  });

  it("carries a hint for every status the API actually returns", async () => {
    for (const status of [400, 404, 409, 413, 422, 429, 500]) {
      stubFetch(() => jsonResponse({ detail: "x" }, status));
      await assert.rejects(api.health(), (e) => {
        assert.ok(e.hint, `status ${status} has no hint`);
        return true;
      });
    }
  });

  it("refuses a 200 that is not JSON instead of reading it as empty", async () => {
    // a proxy error page, a sign-in redirect, a tunnel banner
    stubFetch(() => new Response("<html>login</html>", {
      status: 200, headers: { "Content-Type": "text/html" },
    }));
    await assert.rejects(api.runs(), (e) => {
      assert.match(e.message, /cannot read/i);
      assert.match(e.hint, /proxy|sign-in/i);
      return true;
    });
  });

  it("refuses a 200 whose JSON is broken", async () => {
    stubFetch(() => new Response("{not valid", {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
    await assert.rejects(api.runs(), (e) => {
      assert.match(e.message, /cannot read/i);
      return true;
    });
  });

  it("refuses a well-formed response of the wrong shape", async () => {
    // `list.length` on an object is undefined, which every view reads as "empty"
    stubFetch(() => jsonResponse({ unexpected: true }));
    await assert.rejects(api.runs(), (e) => {
      assert.match(e.message, /not in the expected shape/i);
      return true;
    });
    stubFetch(() => jsonResponse({ total: 3 }));   // a page with no items[]
    await assert.rejects(api.exceptions(), (e) => {
      assert.match(e.message, /not in the expected shape/i);
      return true;
    });
  });

  it("accepts the shapes the API really returns", async () => {
    stubFetch(() => jsonResponse([{ id: "r1" }]));
    assert.deepEqual(await api.runs(), [{ id: "r1" }]);
    stubFetch(() => jsonResponse({ total: 1, items: [{ id: "e1" }] }));
    assert.equal((await api.exceptions()).items.length, 1);
  });

  it("treats 204 as an empty success, not an error", async () => {
    stubFetch(() => new Response(null, { status: 204 }));
    assert.equal(await api.health(), null);
  });
});

describe("caching", () => {
  it("computes an expensive read once", async () => {
    let calls = 0;
    stubFetch(() => { calls += 1; return jsonResponse({ holdout: {} }); });
    await api.evaluation();
    await api.evaluation();
    assert.equal(calls, 1, "the cached read was fetched twice");
  });

  it("never caches a failure", async () => {
    let calls = 0;
    stubFetch(() => { calls += 1; return jsonResponse({ detail: "boom" }, 500); });
    await assert.rejects(api.evaluation());
    await assert.rejects(api.evaluation());
    assert.equal(calls, 2, "a failed read was cached and can never recover");
  });
});

describe("router", () => {
  it("reads the route and its query out of a hash", () => {
    assert.deepEqual(parseHash("#/exceptions?status=open&category=duplicate"),
      { path: "exceptions", params: { status: "open", category: "duplicate" } });
    assert.deepEqual(parseHash("#/runs"), { path: "runs", params: {} });
  });

  it("falls back to the dashboard on an empty or unknown hash", () => {
    assert.equal(parseHash("").path, "dashboard");
    assert.equal(parseHash("#").path, "dashboard");
    assert.equal(parseHash("#/").path, "dashboard");
  });

  it("decodes an encoded parameter", () => {
    assert.equal(parseHash("#/exceptions?q=%20bank%20credit").params.q, " bank credit");
  });

  it("routes an unknown path to the dashboard rather than a blank screen", () => {
    const seen = [];
    globalThis.location = { hash: "#/does-not-exist" };
    globalThis.window = { addEventListener: () => {} };
    const r = createRouter(["dashboard", "runs"], (route) => seen.push(route));
    r.start();
    assert.deepEqual(seen, ["dashboard"]);
  });

  it("marks an earlier navigation stale so a slow render cannot paint over a new one", () => {
    const guards = [];
    globalThis.location = { hash: "#/runs" };
    globalThis.window = { addEventListener: () => {} };
    const r = createRouter(["dashboard", "runs"], (route, params, isCurrent) =>
      guards.push(isCurrent));
    r.start();
    r.start();
    assert.equal(guards.length, 2);
    assert.equal(guards[0](), false, "the first navigation still thinks it is current");
    assert.equal(guards[1](), true);
  });
});
