/* Runs history, accuracy evidence, and the import flow. */

import { api } from "../api.js";
import { money, num, pct, ago, label } from "../format.js";
import {
  el, clear, icon, badge, metric, emptyState, errorState,
  skeletonGrid, skeletonMetrics, toast,
} from "../ui.js";
import { renderResult } from "./reconcile.js";

/* ------------------------------------------------------------------ runs --- */

export async function runs(root, ctx) {
  clear(root);
  const view = el("div", { class: "view" });
  root.append(view);
  view.append(el("div", { class: "view-head" },
    el("p", { text: "Every run is kept. Re-running the same batch is recorded as a "
                  + "separate run — it did happen twice — but never duplicates the worklist." })));

  const slot = el("div", {}, skeletonGrid(8, 6));
  view.append(slot);

  let list;
  try { list = await api.runs(100); }
  catch (err) { clear(slot).append(errorState(err, () => runs(root, ctx))); return; }

  if (!list.length) {
    clear(slot).append(emptyState({
      icon: "history", title: "No runs yet",
      body: "Run a reconciliation and it will be recorded here.",
      action: el("button", { class: "btn btn-primary",
        onClick: () => (location.hash = "#/reconcile") }, icon("play"), "Run one"),
    }));
    return;
  }

  clear(slot).append(el("div", { class: "grid-wrap" },
    el("div", { class: "grid-scroll" },
      el("table", { class: "grid" },
        el("thead", {}, el("tr", {},
          el("th", { text: "Batch" }), el("th", { text: "Resolver" }),
          el("th", { class: "amt", text: "Records" }), el("th", { class: "amt", text: "Groups" }),
          el("th", { class: "amt", text: "Auto-match" }), el("th", { class: "amt", text: "Exceptions" }),
          el("th", { text: "When" }))),
        el("tbody", {}, ...list.map((r) =>
          el("tr", {},
            el("td", {}, el("div", { text: r.dataset }),
              el("div", { class: "id cell-sub", text: r.id })),
            /* Deterministic is the norm, so it stays quiet; an LLM-assisted run
               is the exception and is the one a reader needs to spot. */
            el("td", {}, badge(r.resolver_mode === "llm" ? "LLM-assisted" : "Deterministic",
                               r.resolver_mode === "llm" ? "accent" : "neutral",
                               { dot: false, quiet: r.resolver_mode !== "llm" })),
            el("td", { class: "amt", text: num(r.entries) }),
            el("td", { class: "amt", text: num(r.groups) }),
            el("td", { class: "amt", text: pct(r.auto_match_rate) }),
            el("td", { class: "amt", text: num(r.exceptions) }),
            el("td", { class: "cell-sub", text: ago(r.started_at) })))))),
    el("div", { class: "grid-foot" },
      el("span", { text: `${num(list.length)} runs` }))));
}

/* -------------------------------------------------------------- evidence --- */

export async function evidence(root, ctx) {
  clear(root);
  const view = el("div", { class: "view" });
  root.append(view);
  view.append(el("div", { class: "view-head" },
    el("p", { text: "The matching rules were written against one seed. Everything below "
                  + "is scored on five seeds they have never seen — that gap is the only "
                  + "honest measure of whether they generalise or were merely fitted." })));

  const slot = el("div", {}, skeletonMetrics(4), el("div", { style: { height: "16px" } }),
                  skeletonGrid(4, 8));
  view.append(slot);

  let ev, bm;
  try { [ev, bm] = await Promise.all([api.evaluation(), api.benchmark()]); }
  catch (err) { clear(slot).append(errorState(err, () => evidence(root, ctx))); return; }

  const h = ev.holdout, d = ev.dev;
  clear(slot).append(
    el("div", { class: "section" },
      el("div", { class: "metrics" },
        metric({ k: "Held-out precision", v: pct(h.precision_mean, 2), tone: "ok",
                 note: `worst run ${pct(h.precision_worst, 2)}` }),
        metric({ k: "Held-out recall", v: pct(h.recall_mean, 2),
                 note: `worst run ${pct(h.recall_worst, 2)}` }),
        metric({ k: "Money in wrong groups", v: money(Math.round(h.false_match_cost_inr_total * 100), "INR"),
                 tone: h.false_match_cost_inr_total > 0 ? "risk" : "ok",
                 note: "a wrong match costs more than an exception" }),
        metric({ k: "Peak throughput", v: `${num(bm.peak_records_per_sec)}/s`,
                 note: "single process, no database" }))),

    el("div", { class: "section" },
      el("div", { class: "card" },
        el("div", { class: "card-head" }, el("h3", { text: "Tuned-on vs never-seen" })),
        el("div", { class: "grid-scroll" },
          el("table", { class: "grid" },
            el("thead", {}, el("tr", {},
              el("th", { text: "Set" }), el("th", { class: "amt", text: "Runs" }),
              el("th", { class: "amt", text: "Entries" }), el("th", { class: "amt", text: "Precision" }),
              el("th", { class: "amt", text: "Worst" }), el("th", { class: "amt", text: "Recall" }),
              el("th", { class: "amt", text: "F1" }), el("th", { class: "amt", text: "Exc. category" }))),
            el("tbody", {},
              evRow("Dev — rules tuned here", d, "neutral"),
              evRow("Held-out — never seen", h, "ok")))),
        el("div", { class: "grid-foot" },
          el("span", { text: `Generalisation gap: ${(ev.generalisation_gap.f1 * 100).toFixed(2)} F1 points` }),
          el("span", { class: "spacer" }),
          el("span", { text: h.all_replay_stable ? "All runs replay-stable" : "Replay unstable" })))),

    el("div", { class: "section" },
      el("div", { class: "card" },
        el("div", { class: "card-head" }, el("h3", { text: "Throughput" })),
        el("div", { class: "grid-scroll" },
          el("table", { class: "grid" },
            el("thead", {}, el("tr", {},
              el("th", { class: "amt", text: "Records" }), el("th", { class: "amt", text: "Seconds" }),
              el("th", { class: "amt", text: "Records/sec" }), el("th", { class: "amt", text: "Auto-match" }),
              el("th", { text: "Relative" }))),
            el("tbody", {}, ...bm.runs.map((r) => {
              const max = Math.max(...bm.runs.map((x) => x.records_per_sec));
              return el("tr", {},
                el("td", { class: "amt", text: num(r.records) }),
                el("td", { class: "amt", text: r.seconds.toFixed(2) }),
                el("td", { class: "amt", text: num(r.records_per_sec) }),
                el("td", { class: "amt", text: pct(r.auto_match_rate) }),
                el("td", { style: { width: "160px" } },
                  el("span", { class: "bar-track" },
                    el("span", { class: "bar-fill",
                      style: { width: `${(r.records_per_sec / max) * 100}%`,
                               background: "var(--viz-1)" } }))));
            })))))),

    el("div", { class: "section" },
      el("div", { class: "card" },
        el("div", { class: "card-head" }, el("h3", { text: "Runs that were not perfect" })),
        el("div", { class: "card-body" },
          (() => {
            const bad = (ev.holdout_runs || []).filter((r) => r.precision < 0.999 || r.recall < 0.999);
            if (!bad.length) return el("p", { class: "cell-sub", text: "Every held-out run was perfect." });
            return el("div", { class: "stack gap-2" },
              el("p", { class: "cell-sub",
                text: "Reported rather than hidden. On these, two different same-day subsets "
                    + "sum to the identical rupee — amounts alone cannot decide, so the engine "
                    + "refuses and takes the recall hit." }),
              ...bad.map((r) => el("div", { class: "row gap-3" },
                badge(`${r.profile} · seed ${r.seed}`, "warn", { dot: false }),
                el("span", { class: "cell-sub", text: `precision ${pct(r.precision, 2)}` }),
                el("span", { class: "cell-sub", text: `recall ${pct(r.recall, 2)}` }))));
          })()))));
}

function evRow(name, s, tone) {
  return el("tr", {},
    el("td", {}, badge(name, tone, { dot: false })),
    el("td", { class: "amt", text: num(s.runs) }),
    el("td", { class: "amt", text: num(s.total_entries) }),
    el("td", { class: "amt", text: pct(s.precision_mean, 2) }),
    el("td", { class: "amt", text: pct(s.precision_worst, 2) }),
    el("td", { class: "amt", text: pct(s.recall_mean, 2) }),
    el("td", { class: "amt", text: pct(s.f1_mean, 2) }),
    el("td", { class: "amt", text: pct(s.exception_category_accuracy_mean, 1) }));
}

/* ---------------------------------------------------------------- import --- */

const SOURCES = [
  ["payments", "payment", "Payments", "Gateway captures"],
  ["settlements", "settlement", "Settlements", "Payouts to your bank"],
  ["bank", "bank", "Bank statement", "What actually landed"],
  ["ledger", "ledger", "Ledger", "What the books say"],
];

export async function importView(root, ctx) {
  clear(root);
  const view = el("div", { class: "view" });
  root.append(view);
  view.append(el("div", { class: "view-head" },
    el("p", { text: "Drop in CSV or JSON from any bank or accounting system. Column names "
                  + "are detected automatically — every bank names them differently. You "
                  + "see the proposed mapping and the row quality before anything is run." })));

  const files = {};
  const previews = {};
  const dropsSlot = el("div", { class: "drops" });
  const previewSlot = el("div", { class: "section" });
  const runBtn = el("button", { class: "btn btn-primary", disabled: true },
    icon("play"), "Reconcile these files");

  for (const [field, source, title, hint] of SOURCES) {
    dropsSlot.append(dropzone(field, source, title, hint));
  }

  view.append(
    el("div", { class: "section" }, dropsSlot),
    el("div", { class: "section" },
      el("div", { class: "row gap-3" }, runBtn,
        el("span", { class: "cell-sub",
          text: "Nothing is stored. The run happens in memory; the result is the only copy." })),
    ),
    previewSlot);

  const resultSlot = el("div", {});
  view.append(resultSlot);

  runBtn.addEventListener("click", async () => {
    const fd = new FormData();
    for (const [field, f] of Object.entries(files)) if (f) fd.append(field, f);
    runBtn.dataset.busy = "true";
    const started = performance.now();
    try {
      const result = await api.reconcileUpload(fd);
      ctx.refreshCounts();
      renderResult(resultSlot, result, "Your import",
                   Math.round(performance.now() - started));
      resultSlot.scrollIntoView({ behavior: "smooth", block: "start" });
      toast(`Reconciled ${num(result.metrics.total_entries)} records`, "ok");
    } catch (err) {
      clear(resultSlot).append(errorState(err, null));
      toast(err.message, "err");
    } finally { delete runBtn.dataset.busy; }
  });

  function dropzone(field, source, title, hint) {
    const input = el("input", { type: "file", accept: ".csv,.json", id: `f-${field}` });
    const nameEl = el("div", { class: "drop-hint", text: hint });
    const zone = el("label", { class: "drop", for: `f-${field}` },
      icon("upload", 20), el("div", { class: "drop-name", text: title }), nameEl, input);

    const accept = async (file) => {
      if (!file) return;
      files[field] = file;
      zone.dataset.filled = "true";
      nameEl.textContent = file.name;
      runBtn.disabled = !Object.values(files).some(Boolean);
      try {
        previews[source] = await api.ingestPreview(source, file);
      } catch (err) {
        previews[source] = { error: err.message, source };
      }
      renderPreviews();
    };

    input.addEventListener("change", () => accept(input.files[0]));
    ["dragenter", "dragover"].forEach((ev) =>
      zone.addEventListener(ev, (e) => { e.preventDefault(); zone.dataset.over = "true"; }));
    ["dragleave", "drop"].forEach((ev) =>
      zone.addEventListener(ev, (e) => { e.preventDefault(); delete zone.dataset.over; }));
    zone.addEventListener("drop", (e) => accept(e.dataTransfer.files[0]));
    return zone;
  }

  function renderPreviews() {
    const entries = Object.entries(previews);
    if (!entries.length) { clear(previewSlot); return; }
    clear(previewSlot).append(
      el("div", { class: "section-head" }, el("h3", { text: "What was detected" })),
      el("div", { class: "stack gap-3" }, ...entries.map(([source, p]) => {
        if (p.error) {
          return el("div", { class: "card" }, el("div", { class: "card-body" },
            el("div", { class: "row gap-2" }, badge(source, "risk"),
              el("span", { text: p.error }))));
        }
        const q = p.quality || {};
        return el("div", { class: "card" },
          el("div", { class: "card-head" },
            badge(source, p.usable ? "ok" : "risk"),
            el("span", { class: "spacer" }),
            el("span", { class: "topbar-sub",
              text: `${num(q.valid_rows)} of ${num(q.total_rows)} rows usable` })),
          el("div", { class: "card-body", style: { display: "grid", gap: "10px" } },
            !p.usable
              ? el("div", { class: "rule-why",
                  text: [...(p.missing_required || []).map((f) => `Missing required column: ${f}`),
                         ...Object.entries(p.ambiguous || {}).map(([f, cols]) =>
                           `Ambiguous ${f}: ${cols.join(" / ")} — rename one so it is unambiguous`)]
                        .join(". ") })
              : null,
            el("div", { class: "kv" }, ...Object.entries(p.mapping || {}).map(([k, v]) =>
              el("div", { class: "kv-item" },
                el("div", { class: "kv-k", text: k }),
                el("div", { class: "kv-v truncate", title: v, text: v })))),
            p.split_amount
              ? el("div", { class: "cell-sub",
                  text: "Debit and credit columns were combined into one signed amount." })
              : null,
            q.invalid_rows
              ? el("div", { class: "row gap-2" },
                  badge(`${q.invalid_rows} quarantined`, "warn"),
                  el("span", { class: "cell-sub",
                    text: "Bad rows are held back with a reason; the rest still reconcile." }))
              : null));
      })));
  }
}
