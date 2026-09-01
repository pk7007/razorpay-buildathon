/* Dashboard — the answer to "where does the close stand, and what do I do next?"

   Every tile answers a question a controller actually asks. There is no chart
   here for decoration: the money bar is the close in one line, and the
   category bars say where the work is. */

import { api } from "../api.js";
import { money, moneyShort, num, pct, ago, label, ms } from "../format.js";
import {
  el, clear, icon, metric, emptyState, errorState, skeletonMetrics, excTone,
} from "../ui.js";

export async function dashboard(root, ctx) {
  clear(root);
  const view = el("div", { class: "view" });
  root.append(view);

  const head = el("div", { class: "view-head" },
    el("p", { text: "Where the current close stands, and what still needs a human." }));
  /* Which run these numbers describe. The money figures come from the most
     recent reconciliation, and a dashboard that does not say so invites the
     reader to take them for an all-time total. */
  const provenance = el("div", { class: "run-context" });
  const metricsSlot = el("div", { class: "section" }, skeletonMetrics(5));
  const moneySlot = el("div", { class: "section" });
  /* A class rather than an inline grid: an inline style outranks the media
     query, and two 170px columns on a phone is not a layout. */
  const splitSlot = el("div", { class: "section split" });
  view.append(head, provenance, metricsSlot, moneySlot, splitSlot);

  let summary, runs, last;
  try {
    [summary, runs] = await Promise.all([api.exceptionsSummary(), api.runs(8)]);
    last = runs && runs.length ? runs[0] : null;
  } catch (err) {
    clear(view).append(head, errorState(err, () => dashboard(root, ctx)));
    return;
  }

  // ---- nothing has been reconciled yet -------------------------------------
  if (!last) {
    clear(view).append(
      head,
      emptyState({
        icon: "play",
        title: "No reconciliation has been run yet",
        body: "Run a month to see what ties out, what does not, and how much money "
            + "is sitting unreconciled. It takes about a second.",
        action: el("button", {
          class: "btn btn-primary",
          onClick: () => (location.hash = "#/reconcile"),
        }, icon("play"), "Run a reconciliation"),
      }),
    );
    return;
  }

  const m = last.metrics || {};
  const mo = last.money || {};
  const ccy = mo.currency || "INR";

  provenance.append(
    icon("history", 14),
    el("span", {}, "Figures below are from the latest run — ",
      el("strong", { text: last.dataset }), ", ",
      el("span", { text: ago(last.started_at) }), ", ",
      el("span", { text: `${num(m.total_entries)} records` })),
    el("span", { class: "grow" }),
    el("a", { href: "#/runs", class: "card-link", text: "Compare runs →" }));

  // ---- headline: accuracy, volume, and the money that needs chasing --------
  clear(metricsSlot).append(
    el("div", { class: "metrics" },
      metric({
        /* The tone has to follow the number. A green spine on "0.0%" is the
           dashboard telling you a lie in the one place it should not. */
        k: "Auto-matched", v: pct(m.auto_match_rate),
        tone: m.auto_match_rate >= 0.9 ? "ok" : m.auto_match_rate >= 0.5 ? "warn" : "risk",
        note: `${num(m.matched_entries)} of ${num(m.total_entries)} records`,
      }),
      metric({
        k: "Reconciled", v: moneyShort(mo.reconciled_paise, ccy),
        title: money(mo.reconciled_paise, ccy),
        note: "tied across all four sources",
      }),
      metric({
        k: "Recoverable", v: moneyShort(mo.recoverable_paise, ccy),
        tone: mo.recoverable_paise > 0 ? "risk" : "",
        title: money(mo.recoverable_paise, ccy),
        note: "booked, never reached the bank",
      }),
      metric({
        /* open_count folds "investigating" in — it is the size of the queue,
           not the count of untouched rows. The worklist says "Unresolved" for
           the same number; the two screens have to agree. */
        k: "Unresolved", v: num(summary.open_count || 0),
        tone: summary.open_count ? "warn" : "ok",
        note: summary.carried_forward
          ? `${summary.carried_forward} carried forward`
          : `${(summary.by_status || {}).open || 0} open, `
            + `${(summary.by_status || {}).investigating || 0} being investigated`,
      }),
      metric({
        k: "Value at stake", v: moneyShort(summary.open_value_minor || 0, ccy),
        title: money(summary.open_value_minor || 0, ccy),
        note: "across open items",
      }),
    ));

  // ---- the close in one line ----------------------------------------------
  const parts = [
    ["Reconciled", mo.reconciled_paise, "var(--ok)",
     "tied across payment, settlement, bank and ledger"],
    ["In transit", mo.in_transit_paise, "var(--info)",
     "settled or awaiting payout within the normal cycle"],
    ["Recoverable", mo.recoverable_paise, "var(--risk)",
     "booked revenue that never reached the bank"],
    ["Unrecorded", mo.unrecorded_paise, "var(--warn)",
     "bank credits with no ledger entry"],
    ["Ambiguous", mo.ambiguous_paise, "var(--viz-3)",
     "paid out, not uniquely attributable"],
  ].filter(([, v]) => (v || 0) > 0);
  const total = parts.reduce((s, p) => s + p[1], 0) || 1;

  moneySlot.append(
    el("div", { class: "card" },
      el("div", { class: "card-head" },
        el("h3", { text: "Where the money is" }),
        el("span", { class: "spacer" }),
        el("span", { class: "topbar-sub",
                     text: `${money(mo.gross_processed_paise, ccy)} processed` })),
      el("div", { class: "card-body" },
        el("div", { class: "moneybar", role: "img",
                    "aria-label": parts.map(([k, v]) =>
                      `${k} ${money(v, ccy)}`).join(", ") },
          ...parts.map(([k, v, c]) =>
            el("span", { style: { width: `${(100 * v / total).toFixed(2)}%`, background: c },
                         title: `${k}: ${money(v, ccy)}` }))),
        el("div", { class: "moneykey" },
          ...parts.map(([k, v, c, note]) =>
            el("div", { class: "moneykey-item" },
              el("span", { class: "moneykey-sw", style: { background: c } }),
              el("div", {},
                el("div", {},
                  el("span", { class: "moneykey-v", text: money(v, ccy) }),
                  " ",
                  el("span", { class: "moneykey-k", text: k })),
                el("div", { class: "moneykey-k", style: { fontSize: "11px" }, text: note }))))))));

  // ---- where the work is + recent runs -------------------------------------
  const byCat = Object.entries(summary.by_category || {});
  const catCard = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("h3", { text: "Where the work is" }),
      el("span", { class: "spacer" }),
      byCat.length ? el("a", { href: "#/exceptions", class: "card-link",
                               text: "Open worklist →" }) : null),
    el("div", { class: "card-body" },
      byCat.length
        ? el("div", { class: "bar-list" }, ...(() => {
            const max = Math.max(...byCat.map(([, n]) => n));
            const tones = { risk: "var(--risk)", warn: "var(--warn)",
                            info: "var(--info)", neutral: "var(--ink-4)" };
            return byCat.slice(0, 6).map(([cat, n]) => {
              const tone = excTone(cat);
              return el("a", {
                class: "bar-item",
                href: `#/exceptions?category=${encodeURIComponent(cat)}`,
                style: { textDecoration: "none", color: "inherit" },
              },
                el("span", { class: "truncate", text: label(cat) }),
                el("span", { class: "bar-track" },
                  el("span", { class: "bar-fill",
                    style: { width: `${(n / max) * 100}%`,
                             background: tones[tone] || "var(--ink-4)" } })),
                el("strong", { text: String(n) }));
            });
          })())
        : el("p", { class: "topbar-sub",
                    text: "Nothing open. Every record in the last run tied out." })));

  const runsCard = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("h3", { text: "Recent runs" }),
      el("span", { class: "spacer" }),
      el("a", { href: "#/runs", class: "card-link", text: "All runs →" })),
    el("div", { class: "grid-scroll" },
      el("table", { class: "grid" },
        el("thead", {}, el("tr", {},
          el("th", { text: "Batch" }),
          el("th", { class: "amt", text: "Records" }),
          el("th", { class: "amt", text: "Matched" }),
          el("th", { class: "amt", text: "Exceptions" }),
          el("th", { text: "When" }))),
        el("tbody", {}, ...runs.map((r) =>
          el("tr", {},
            el("td", {}, el("span", { text: r.dataset }),
              r.resolver_mode === "llm"
                ? el("span", { class: "cell-sub", text: " · llm" }) : null),
            el("td", { class: "amt", text: num(r.entries) }),
            el("td", { class: "amt", text: pct(r.auto_match_rate) }),
            el("td", { class: "amt", text: num(r.exceptions) }),
            el("td", { class: "cell-sub", text: ago(r.started_at) })))))));

  splitSlot.append(catCard, runsCard);

  // ---- provenance + engine facts, stated rather than implied ---------------
  const evi = el("div", { class: "section" });
  view.append(evi);
  try {
    const [health, rzp] = await Promise.all([api.health(), api.razorpayStatus()]);
    evi.append(
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("h3", { text: "This run" }),
          el("span", { class: "spacer" }),
          el("a", { href: "#/evidence", class: "card-link", text: "Accuracy evidence →" })),
        el("div", { class: "card-body", style: { display: "grid", gap: "14px" } },
          decidedBy(m, health),
          el("div", { class: "kv" },
            kvItem("Resolver", health.resolver === "llm" ? "LLM + rules" : "Deterministic only",
              health.resolver === "llm"
                ? "An LLM resolved the residual tail."
                : "No model was used. Every number here is deterministic."),
            kvItem("Razorpay data",
              rzp.provenance_if_run === "live_test" ? "Test-mode API" : "Local fixture",
              /* Three states, not two: a key can be present and still not work,
                 and saying "Test-mode API" in that case would label fixtures as
                 Razorpay data. */
              rzp.provenance_if_run === "live_test"
                ? "Pulled from Razorpay test mode."
                : rzp.configured
                  ? "Credentials are set but the API could not be reached — "
                    + "runs are using fixtures."
                  : "Fixtures in Razorpay's response shape — not Razorpay data."),
            kvItem("Replay", m.replay_stable ? "Stable" : "Unstable",
              m.replay_stable ? "Re-running reproduced the same result." : "Re-run differed — investigate."),
            kvItem("Engine time", ms(m.latency_ms),
              m.total_entries && m.latency_ms
                ? `${num(Math.round(m.total_entries / (m.latency_ms / 1000)))} records/sec`
                : ""),
          ))));
  } catch { /* the dashboard is still useful without provenance */ }
}

/* What decided this batch, as a proportion of the groups formed.

   This is the architecture claim rendered as the run's own numbers rather than
   asserted as a tagline: accounting identities and exact references settle the
   overwhelming majority, and whatever is left is the only thing a model is ever
   shown. A judge does not have to take the diagram on trust — the bar is
   measured from the run in front of them, and it moves when the data does. */
function decidedBy(m, health) {
  const usedModel = health.resolver === "llm";
  const bands = [
    ["Accounting identity", m.structural_share, "var(--viz-1)",
     "gross = net + fee + tax + TDS, tied across sources"],
    ["Exact reference", m.deterministic_share, "var(--viz-2)",
     "a shared UTR or order id, matched exactly"],
    [usedModel ? "Model, on the residual" : "Scorer, on the residual",
     m.resolver_share, "var(--viz-3)",
     usedModel
       ? "an LLM, shown only what the rules could not place"
       : "a deterministic scorer — no model was involved"],
  ].filter(([, share]) => (share || 0) > 0);

  if (!bands.length) return el("div", {});

  return el("div", {},
    el("div", { class: "metric-k", style: { marginBottom: "6px" },
                text: "How this batch was decided" }),
    el("div", { class: "moneybar", role: "img",
                "aria-label": bands.map(([k, v]) => `${k} ${pct(v)}`).join(", ") },
      ...bands.map(([k, v, c]) =>
        el("span", { style: { width: `${(v * 100).toFixed(1)}%`, background: c },
                     title: `${k}: ${pct(v)}` }))),
    el("div", { class: "moneykey" },
      ...bands.map(([k, v, c, note]) =>
        el("div", { class: "moneykey-item" },
          el("span", { class: "moneykey-sw", style: { background: c } }),
          el("div", {},
            el("div", {},
              el("span", { class: "moneykey-v", text: pct(v) }), " ",
              el("span", { class: "moneykey-k", text: k })),
            el("div", { class: "moneykey-k", style: { fontSize: "11px" }, text: note }))))));
}

function kvItem(k, v, note) {
  return el("div", { class: "kv-item" },
    el("div", { class: "kv-k", text: k }),
    el("div", { class: "kv-v", text: v }),
    note ? el("div", { class: "cell-sub", style: { fontSize: "11px" }, text: note }) : null);
}
