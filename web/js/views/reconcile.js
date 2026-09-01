/* Reconcile — pick a source, watch the stages, read the result.

   The stage strip is driven by real pipeline milestones, not a timer. Nothing
   here invents progress: a stage only advances when the work behind it is
   genuinely done. */

import { api, clearCache } from "../api.js";
import { money, moneyShort, num, pct, ms, label } from "../format.js";
import {
  el, clear, icon, metric, badge, groupBadge, toast, drawer,
  errorState, emptyState, excTone, groupTone,
} from "../ui.js";

const STAGES = ["Ingest", "Validate", "Normalize", "Match", "Resolve", "Report"];

export async function reconcile(root, ctx) {
  clear(root);
  const view = el("div", { class: "view" });
  root.append(view);

  view.append(el("div", { class: "view-head" },
    el("p", { text: "Pick a batch. The engine ties payments, settlements, bank credits "
                  + "and ledger entries together, then reports what it could not resolve." })));

  const sourceSlot = el("div", { class: "section" });
  const runSlot = el("div", {});
  view.append(sourceSlot, runSlot);

  let datasets = [], rzp = null;
  try {
    [datasets, rzp] = await Promise.all([
      api.datasets(),
      api.razorpayStatus().catch(() => null),
    ]);
  } catch (err) {
    sourceSlot.append(errorState(err, () => reconcile(root, ctx)));
    return;
  }

  const cards = el("div", {
    style: { display: "grid", gap: "12px",
             gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))" },
  });

  for (const d of datasets) {
    cards.append(sourceCard({
      title: `${d.label} month`,
      meta: d.days ? `${d.days} days of activity` : "",
      body: d.blurb,
      cta: "Reconcile",
      onRun: () => run(() => api.reconcile(d.name), `${d.label} month`),
    }));
  }

  cards.append(sourceCard({
    title: "Financial scenarios",
    meta: "15 hand-checked cases",
    body: "Refunds, partial refunds, chargebacks, TDS, zero-MDR UPI, a cross-period "
        + "payout and a multi-currency pair — each with a known correct answer.",
    cta: "Reconcile",
    onRun: () => run(() => api.reconcileScenarios(), "Financial scenarios"),
  }));

  if (rzp) {
    const live = rzp.provenance_if_run === "live_test";
    cards.append(sourceCard({
      title: "Razorpay",
      meta: live ? "test-mode API"
                 : rzp.configured ? "fixture — API unreachable" : "local fixture",
      tone: live ? "ok" : "neutral",
      body: live
        ? "Pulled live from your Razorpay test-mode account."
        : rzp.configured
        ? "Credentials are set but the test-mode API could not be reached, so this "
        + "runs on fixtures. Check the key and secret, then reload."
        : "Fixtures in Razorpay's documented response shape. Not Razorpay data — "
        + "add test-mode keys to pull the real thing.",
      cta: "Reconcile",
      onRun: () => run(() => api.reconcileRazorpay(true), "Razorpay batch"),
    }));
  }

  sourceSlot.append(
    el("div", { class: "section-head" },
      el("h3", { text: "Choose a batch" }),
      el("span", { class: "spacer" }),
      el("a", { class: "card-link", href: "#/import", text: "Or import your own files →" })),
    cards);

  /* No simulated progress. The pipeline runs server-side in a single call, so
     the only two honest states are "in flight" and "done" — and the elapsed
     clock below is measured, not animated. The stage list is then shown on the
     result as a record of what ran, where it can actually be read. */
  async function run(fn, title) {
    clear(runSlot);
    const elapsed = el("span", { class: "topbar-sub", text: "0 ms" });
    runSlot.append(el("div", { class: "section" },
      el("div", { class: "card" },
        el("div", { class: "card-head" },
          el("span", { class: "spin" }),
          el("h3", { text: `Reconciling ${title}` }),
          el("span", { class: "spacer" }), elapsed))));

    const started = performance.now();
    const tick = setInterval(
      () => { elapsed.textContent = ms(Math.round(performance.now() - started)); }, 80);

    let result;
    try {
      result = await fn();
    } catch (err) {
      clearInterval(tick);
      clear(runSlot).append(el("div", { class: "section" },
        errorState(err, () => run(fn, title))));
      return;
    }
    clearInterval(tick);
    clearCache("evaluation");
    ctx.refreshCounts();
    renderResult(runSlot, result, title, Math.round(performance.now() - started));
    runSlot.scrollIntoView({ behavior: "smooth", block: "start" });
    toast(`${title} reconciled — ${num(result.metrics.total_entries)} records`, "ok");
  }
}

/* The card *is* the control. Five identical primary buttons on one screen is
   five competing calls to action; making each card a single button leaves one
   tab stop per choice and no ambiguity about what the primary action is. */
function sourceCard({ title, meta, body, cta, onRun, tone }) {
  const foot = el("span", { class: "source-cta" }, el("span", { text: cta }), icon("arrow", 15));
  const card = el("button", { class: "source-card", type: "button" },
    el("span", { class: "source-head" },
      el("span", { class: "source-title", text: title }),
      meta ? badge(meta, tone || "neutral", { dot: false }) : null),
    el("span", { class: "source-body", text: body }),
    foot);

  card.addEventListener("click", async () => {
    if (card.dataset.busy) return;
    card.dataset.busy = "true";
    clear(foot).append(el("span", { class: "spin" }), el("span", { text: "Reconciling…" }));
    try { await onRun(); }
    finally {
      delete card.dataset.busy;
      clear(foot).append(el("span", { text: cta }), icon("arrow", 15));
    }
  });
  return card;
}

/* The pipeline, shown on a finished run rather than animated during one. */
function stageStrip() {
  const node = el("div", { class: "steps", "aria-label": "Pipeline stages" });
  STAGES.forEach((s, i) => {
    if (i) node.append(el("span", { class: "step-line" }));
    node.append(el("span", { class: "step", dataset: { state: "done" } },
      el("span", { class: "step-dot", text: "✓" }), el("span", { text: s })));
  });
  return node;
}

/* ---------------------------------------------------------------- result --- */

export function renderResult(host, result, title, roundtrip) {
  clear(host);
  const m = result.metrics, mo = result.money, ccy = mo.currency || "INR";
  const entries = Object.fromEntries((result.entries || []).map((e) => [e.id, e]));

  host.append(el("div", { class: "section" },
    el("div", { class: "section-head" },
      el("h3", { text: `${title} — result` }),
      el("span", { class: "spacer" }),
      el("span", { class: "topbar-sub",
                   text: `${ms(m.latency_ms)} engine · ${ms(roundtrip)} round trip` })),
    el("div", { class: "card", style: { marginBottom: "12px" } },
      el("div", { class: "card-body" }, stageStrip())),
    el("div", { class: "metrics" },
      metric({ k: "Auto-matched", v: pct(m.auto_match_rate),
               tone: m.auto_match_rate >= 0.9 ? "ok" : m.auto_match_rate >= 0.5 ? "warn" : "risk",
               note: `${num(m.matched_entries)} of ${num(m.total_entries)} records` }),
      metric({ k: "Reconciled", v: moneyShort(mo.reconciled_paise, ccy),
               title: money(mo.reconciled_paise, ccy), note: `${num(m.groups)} groups` }),
      metric({ k: "Exceptions", v: num(m.exceptions),
               tone: m.exceptions ? "warn" : "ok", note: "each with a reason and an action" }),
      metric({ k: "Recoverable", v: moneyShort(mo.recoverable_paise, ccy),
               tone: mo.recoverable_paise > 0 ? "risk" : "",
               title: money(mo.recoverable_paise, ccy), note: "chase this" }),
      m.precision != null
        ? metric({ k: "Precision", v: pct(m.precision, 2),
                   tone: m.precision >= 0.999 ? "ok" : m.precision >= 0.98 ? "warn" : "risk",
                   note: "against a ground-truth key" })
        : metric({ k: "Replay", v: m.replay_stable ? "Stable" : "Unstable",
                   tone: m.replay_stable ? "ok" : "risk", note: "re-run reproduces this" }),
    )));

  // ---- tabs: groups | exceptions | audit ----
  const tabsHost = el("div", { class: "section" });
  host.append(tabsHost);
  const panel = el("div", {});
  const tabs = [
    ["groups", `Matched groups (${num(result.groups.length)})`],
    ["exceptions", `Exceptions (${num(result.exceptions.length)})`],
    ["audit", `Audit trail (${num(result.audit.length)})`],
  ];
  /* Toggle buttons, not ARIA tabs. A real `role="tab"` needs `aria-selected`
     and an id-linked `tabpanel`; half-implemented tab roles announce worse than
     honest pressed-state buttons. */
  const chips = el("div", { class: "chips", role: "group", "aria-label": "Result view" });
  const render = {
    groups: () => groupsTable(result, entries, ccy),
    exceptions: () => exceptionsTable(result, ccy),
    audit: () => auditList(result),
  };
  let current = "groups";
  const select = (key) => {
    current = key;
    [...chips.children].forEach((c) =>
      c.setAttribute("aria-pressed", String(c.dataset.key === key)));
    clear(panel).append(render[key]());
  };
  tabs.forEach(([key, text]) => {
    chips.append(el("button", {
      class: "chip", dataset: { key },
      "aria-pressed": String(key === current), text,
      onClick: () => select(key),
    }));
  });
  tabsHost.append(chips, panel);
  select("groups");
}

function groupsTable(result, entries, ccy) {
  if (!result.groups.length) {
    /* "Nothing matched" is a useless answer on its own. The most common cause
       on a first import is a batch with only the two ends of the chain: a bank
       credit and a booked sale differ by the gateway fee, so nothing but the
       payment and settlement records in between can bridge them. */
    const present = new Set((result.entries || []).map((e) => e.source));
    const missing = ["payment", "settlement"].filter((s) => !present.has(s));
    return emptyState({
      title: "No groups formed",
      body: missing.length
        ? `This batch has ${[...present].join(" and ")} only. A bank credit and a `
          + `booked sale differ by the gateway fee, so they cannot be tied to each `
          + `other directly — add the ${missing.join(" and ")} file${missing.length > 1 ? "s" : ""} `
          + `and the same rows will reconcile.`
        : "Nothing in this batch could be tied together. Every entry is reported "
          + "as an exception with the reason it stayed unmatched.",
    });
  }
  const rows = result.groups.map((g) => {
    const tr = el("tr", { dataset: { clickable: "1" }, tabindex: "0" },
      el("td", { class: "id", text: g.group_id }),
      el("td", {}, groupBadge(g.status)),
      el("td", {}, sourcePips(g.sources)),
      el("td", { class: "amt", text: money(g.amount_paise, ccy) }),
      el("td", { class: "amt cell-sub", text: pct(g.confidence, 0) }),
      el("td", { class: "cell-sub truncate", style: { maxWidth: "460px" },
                 title: g.rationale, text: g.rationale }));
    const open = () => showGroup(g, entries, ccy);
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    return tr;
  });
  return el("div", { class: "grid-wrap" },
    el("div", { class: "grid-scroll" },
      el("table", { class: "grid" },
        el("thead", {}, el("tr", {},
          el("th", { text: "Group" }), el("th", { text: "Status" }),
          el("th", { text: "Sources" }), el("th", { class: "amt", text: "Amount" }),
          el("th", { class: "amt", text: "Conf." }), el("th", { text: "Why it matched" }))),
        el("tbody", {}, ...rows))),
    el("div", { class: "grid-foot" },
      el("span", { text: `${num(result.groups.length)} groups` }),
      el("span", { class: "spacer" }),
      el("span", { text: "Select a row for the entries behind it" })));
}

/* Four sources, four slots, always in the same order. Spelling out
   "bank + ledger + payment + settlement" on every row costs 140px of the
   column that actually explains the match, and reads identically on 90% of
   rows anyway. The letter carries the meaning; the fill carries presence. */
const SOURCE_SLOTS = [
  ["payment", "P", "Payment"], ["settlement", "S", "Settlement"],
  ["bank", "B", "Bank credit"], ["ledger", "L", "Ledger"],
];

function sourcePips(sources) {
  const have = new Set(sources || []);
  const extra = (sources || []).filter((s) => !SOURCE_SLOTS.some(([k]) => k === s));
  return el("span", {
    class: "pips",
    "aria-label": `Sources: ${(sources || []).join(", ") || "none"}`,
  },
    ...SOURCE_SLOTS.map(([key, letter, name]) =>
      el("span", {
        class: `pip${have.has(key) ? " on" : ""}`,
        title: have.has(key) ? name : `No ${name.toLowerCase()}`,
        text: letter,
      })),
    ...extra.map((s) => el("span", { class: "pip on alt", title: s, text: s[0].toUpperCase() })));
}

function showGroup(g, entries, ccy) {
  const members = g.entry_ids.map((id) => entries[id]).filter(Boolean);
  const deductions = members.filter((e) => e.source === "refund" || e.source === "chargeback");
  const deducted = deductions.reduce((sum, e) => sum + Math.abs(e.amount_paise), 0);

  const body = el("div", { class: "stack gap-4" },
    el("div", { class: "kv" },
      kvBox("Sale", money(g.amount_paise, ccy)),
      kvBox("Status", groupTone(g.status)[1]),
      kvBox("Stage", label(g.stage)),
      kvBox("Confidence", pct(g.confidence, 0))),

    /* A refunded sale has two grosses: what was sold, and what was left to pay
       out. Showing only the first put ₹8,000 at the top of a panel whose
       arithmetic underneath starts from ₹5,000 — the two numbers are both
       right and the reader has no way to see why they differ. */
    deducted
      ? el("div", { class: "ledger" },
          el("div", { class: "ledger-row" },
            el("span", { class: "grow", text: "Sale" }),
            el("span", { class: "amt", text: money(g.amount_paise, ccy) })),
          ...deductions.map((e) =>
            el("div", { class: "ledger-row" },
              el("span", { class: "grow" },
                `less ${e.source} `,
                el("span", { class: "id", text: e.id })),
              el("span", { class: "amt", text: money(-Math.abs(e.amount_paise), ccy) }))),
          el("div", { class: "ledger-row is-total" },
            el("span", { class: "grow", text: "Gross that had to settle" }),
            el("span", { class: "amt",
                         text: money(g.amount_paise - deducted, ccy) })))
      : null,

    el("div", {},
      el("h4", { class: "metric-k", style: { marginBottom: "6px" }, text: "Why these belong together" }),
      el("div", { class: "rule-why", text: g.rationale }),
      el("div", { class: "cell-sub", style: { marginTop: "6px" }, text: `Rule: ${g.rule}` })),
    el("div", {},
      el("h4", { class: "metric-k", style: { marginBottom: "6px" }, text: "Entries" }),
      el("div", { class: "grid-wrap" }, el("div", { class: "grid-scroll" },
        el("table", { class: "grid" },
          el("thead", {}, el("tr", {},
            el("th", { text: "Id" }), el("th", { text: "Source" }),
            el("th", { text: "Date" }), el("th", { class: "amt", text: "Amount" }))),
          el("tbody", {}, ...members.map((e) =>
            el("tr", {},
              el("td", { class: "id", text: e.id }),
              el("td", {}, badge(e.source, "neutral", { dot: false })),
              el("td", { class: "cell-sub", text: e.value_date }),
              el("td", { class: "amt", text: money(e.amount_paise, e.currency || ccy) })))))))));
  drawerFor(`Group ${g.group_id}`, `${members.length} entries`, body);
}

function exceptionsTable(result, ccy) {
  if (!result.exceptions.length) {
    return emptyState({
      icon: "check", title: "Nothing unresolved",
      body: "Every record in this batch tied out. There is no work to hand to a human.",
    });
  }
  const rows = result.exceptions.map((x) => {
    const tr = el("tr", { dataset: { clickable: "1" }, tabindex: "0" },
      el("td", {}, badge(label(x.category), excTone(x.category))),
      el("td", { class: "id", text: x.entry_id }),
      el("td", { class: "cell-sub", text: x.source }),
      el("td", { class: "cell-sub", text: x.value_date }),
      el("td", { class: "amt", text: money(x.amount_paise, ccy) }),
      el("td", { class: "cell-sub truncate", style: { maxWidth: "360px" },
                 title: x.rationale, text: x.rationale }));
    const open = () => drawerFor(label(x.category), x.entry_id,
      el("div", { class: "stack gap-4" },
        el("div", { class: "kv" },
          kvBox("Amount", money(x.amount_paise, ccy)),
          kvBox("Source", x.source),
          kvBox("Date", x.value_date),
          kvBox("Confidence", pct(x.confidence, 0))),
        el("div", { class: "rule-why", text: x.rationale }),
        el("div", { class: "evidence" },
          el("div", { class: "evidence-head" }, icon("arrow", 14), "Suggested action"),
          el("div", { text: x.suggested_action }))));
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    return tr;
  });
  return el("div", { class: "grid-wrap" },
    el("div", { class: "grid-scroll" },
      el("table", { class: "grid" },
        el("thead", {}, el("tr", {},
          el("th", { text: "Category" }), el("th", { text: "Entry" }),
          el("th", { text: "Source" }), el("th", { text: "Date" }),
          el("th", { class: "amt", text: "Amount" }), el("th", { text: "Reason" }))),
        el("tbody", {}, ...rows))),
    el("div", { class: "grid-foot" },
      el("span", { text: "These persist to the worklist and survive the next run" }),
      el("span", { class: "spacer" }),
      el("a", { href: "#/exceptions", class: "card-link", text: "Open worklist →" })));
}

function auditList(result) {
  const items = result.audit.slice(0, 400);
  return el("div", { class: "grid-wrap" },
    el("div", { class: "grid-scroll" },
      el("table", { class: "grid" },
        el("thead", {}, el("tr", {},
          el("th", { text: "#" }), el("th", { text: "Stage" }),
          el("th", { text: "Rule" }), el("th", { text: "Outcome" }),
          el("th", { text: "Reasoning" }))),
        el("tbody", {}, ...items.map((a) =>
          el("tr", {},
            el("td", { class: "id", text: String(a.seq) }),
            el("td", {}, badge(a.stage, a.stage === "agent" ? "accent" : "neutral", { dot: false })),
            el("td", { class: "id", text: a.rule }),
            el("td", {}, badge(a.outcome,
              a.outcome === "matched" ? "ok" : a.outcome === "rejected" ? "risk" : "warn")),
            el("td", { class: "cell-sub", style: { minWidth: "320px" }, text: a.rationale })))))),
    el("div", { class: "grid-foot" },
      el("span", { text: `${num(result.audit.length)} decisions, in order` }),
      el("span", { class: "spacer" }),
      result.audit.length > 400 ? el("span", { text: "showing first 400" }) : null));
}

/* small local helpers ------------------------------------------------------ */

function kvBox(k, v) {
  return el("div", { class: "kv-item" },
    el("div", { class: "kv-k", text: k }),
    el("div", { class: "kv-v", text: v }));
}

function drawerFor(title, subtitle, body) {
  drawer({ title, subtitle, body });
}
