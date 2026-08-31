/* Worklist — the screen a finance user lives in.

   Filters are URL state, so a filtered view is shareable and survives a back
   button. The grid shows six columns; everything else lives in the detail
   drawer, because a 30-column table is a spreadsheet, not a workflow. */

import { api } from "../api.js";
import { money, num, label, date, titleCase } from "../format.js";
import {
  el, clear, icon, badge, emptyState, errorState, skeletonGrid,
  excTone, workTone, priorityTone, metric,
} from "../ui.js";
import { showException } from "./exception-detail.js";

const STATUSES = ["open", "investigating", "resolved", "written_off"];
const SORTS = [
  ["amount_minor", "Amount"],
  ["value_date", "Value date"],
  ["times_seen", "Age (runs seen)"],
  ["updated_at", "Last touched"],
  ["priority", "Priority"],
];

export async function exceptions(root, ctx, params = {}) {
  clear(root);
  const view = el("div", { class: "view" });
  root.append(view);

  const state = {
    status: params.status ?? "open",
    category: params.category ?? "",
    q: params.q ?? "",
    sort: params.sort ?? "amount_minor",
    order: params.order ?? "desc",
  };

  view.append(el("div", { class: "view-head" },
    el("p", { text: "Items the engine could not resolve. They persist between runs, "
                  + "keep whatever you write on them, and clear themselves when a "
                  + "later run explains them." })));

  const metricsSlot = el("div", { class: "section" });
  const filterSlot = el("div", {});
  const gridSlot = el("div", {}, skeletonGrid(8, 6));
  /* Changing a filter changes the table silently for a sighted user, who can
     see it; a screen reader user needs to be told what the filter did. */
  const announce = el("div", { class: "sr-only", role: "status", "aria-live": "polite" });
  view.append(metricsSlot, filterSlot, announce, gridSlot);

  let summary;
  try {
    summary = await api.exceptionsSummary();
  } catch (err) {
    clear(gridSlot).append(errorState(err, () => exceptions(root, ctx, params)));
    return;
  }

  renderMetrics();
  renderFilters();
  await load();

  function renderMetrics() {
    const s = summary;
    clear(metricsSlot).append(el("div", { class: "metrics" },
      /* The API's open_count folds "investigating" in — it is the size of the
         queue, not the count of untouched rows. Label it as what it is. */
      metric({ k: "Unresolved", v: num(s.open_count || 0),
               tone: s.open_count ? "warn" : "ok",
               note: (s.by_status || {}).investigating
                 ? `${(s.by_status || {}).open || 0} open, `
                   + `${(s.by_status || {}).investigating} being investigated`
                 : "waiting on a human" }),
      metric({ k: "Value at stake", v: money(s.open_value_minor || 0, "INR"),
               tone: s.open_value_minor ? "risk" : "",
               note: "across open items" }),
      metric({ k: "Carried forward", v: num(s.carried_forward || 0),
               note: "survived more than one run" }),
      metric({ k: "Resolved", v: num((s.by_status || {}).resolved || 0),
               tone: (s.by_status || {}).resolved ? "ok" : "",
               note: "closed, with a reason" })));
  }

  function renderFilters() {
    const search = el("input", {
      class: "input", type: "search", placeholder: "Search entry id or reason…",
      value: state.q, "aria-label": "Search the worklist",
    });
    let t;
    search.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => { state.q = search.value.trim(); sync(); load(); }, 220);
    });

    const statusChips = el("div", { class: "chips", role: "group", "aria-label": "Status" },
      chip("All", state.status === "", () => { state.status = ""; sync(); load(); }),
      ...STATUSES.map((s) =>
        chip(titleCase(s), state.status === s,
             () => { state.status = s; sync(); load(); },
             (summary.by_status || {})[s])));

    const catSel = el("select", { class: "select", "aria-label": "Filter by category" },
      el("option", { value: "", text: "All categories" }),
      ...Object.entries(summary.by_category || {}).map(([c, n]) =>
        el("option", { value: c, text: `${label(c)} (${n})`,
                       selected: state.category === c })));
    catSel.addEventListener("change", () => { state.category = catSel.value; sync(); load(); });

    const sortSel = el("select", { class: "select", "aria-label": "Sort by" },
      ...SORTS.map(([v, t2]) =>
        el("option", { value: v, text: `Sort: ${t2}`, selected: state.sort === v })));
    sortSel.addEventListener("change", () => { state.sort = sortSel.value; sync(); load(); });

    const orderBtn = el("button", {
      class: "btn btn-icon", "aria-label": "Toggle sort direction",
      title: state.order === "desc" ? "Descending" : "Ascending",
    }, icon(state.order === "desc" ? "down" : "up"));
    orderBtn.addEventListener("click", () => {
      state.order = state.order === "desc" ? "asc" : "desc"; sync(); renderFilters(); load();
    });

    clear(filterSlot).append(
      el("div", { class: "filters" },
        el("div", { class: "search-wrap" }, icon("search"), search),
        statusChips,
        el("span", { class: "grow" }),
        catSel, sortSel, orderBtn));
  }

  function chip(text, active, onClick, count) {
    return el("button", {
      class: "chip", "aria-pressed": String(!!active), onClick,
    }, text, count != null ? el("span", { class: "n", text: String(count) }) : null);
  }

  function sync() {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(state)) {
      if (v && !(k === "sort" && v === "amount_minor") && !(k === "order" && v === "desc")) {
        q.set(k, v);
      }
    }
    const qs = q.toString();
    history.replaceState(null, "", `#/exceptions${qs ? `?${qs}` : ""}`);
  }

  async function load() {
    clear(gridSlot).append(skeletonGrid(8, 6));
    let data;
    try {
      data = await api.exceptions({ ...state, limit: 200 });
    } catch (err) {
      clear(gridSlot).append(errorState(err, load));
      return;
    }

    if (!data.items.length) {
      /* Three different empty states, because they mean three different things:
         nothing has ever run, the queue has been worked to zero, or the filters
         are simply too narrow. Telling a first-time user that "every exception
         has been dealt with" is a lie about work they have not done. */
      const filtered = !!(state.q || state.category || state.status !== "open");
      const neverRun = !summary.total;
      const empty = neverRun
        ? { icon: "play", title: "Nothing to work on yet",
            body: "Run a reconciliation. Anything the engine cannot resolve on its "
                + "own arrives here, with the reason and a suggested action.",
            action: el("button", { class: "btn btn-primary",
              onClick: () => (location.hash = "#/reconcile") },
              icon("play"), "Run a reconciliation") }
        : filtered
          ? { icon: "empty", title: "No items match these filters",
              body: "Try a wider status, or clear the search.",
              action: el("button", { class: "btn", onClick: () => {
                state.q = ""; state.category = ""; state.status = "open";
                sync(); renderFilters(); load();
              } }, "Reset filters") }
          : { icon: "check", title: "Nothing open",
              body: `All ${num(summary.total)} exceptions have been closed. New ones `
                  + "appear here after the next run.",
              action: el("button", { class: "btn",
                onClick: () => { state.status = ""; sync(); renderFilters(); load(); } },
                "Show closed items") };

      announce.textContent = empty.title;
      clear(gridSlot).append(emptyState(empty));
      return;
    }

    announce.textContent = `${data.items.length} of ${data.total} exceptions shown`;

    const rows = data.items.map((x) => {
      const tr = el("tr", { dataset: { clickable: "1" }, tabindex: "0" },
        el("td", {}, badge(label(x.status), workTone(x.status), { quiet: true })),
        el("td", {},
          el("div", {}, badge(label(x.category), excTone(x.category))),
          x.times_seen > 1
            ? el("div", { class: "cell-sub", style: { marginTop: "3px" },
                          text: `seen ${x.times_seen}×` })
            : null),
        el("td", {},
          el("div", { class: "id", text: x.entry_id }),
          el("div", { class: "cell-sub truncate", style: { maxWidth: "440px" },
                      title: x.rationale, text: x.rationale })),
        el("td", {}, badge(x.priority, priorityTone(x.priority), { dot: false })),
        el("td", { class: "cell-sub", text: date(x.value_date) }),
        el("td", { class: "amt", text: money(x.amount_minor, x.currency) }));

      const open = () => showException(x.id, {
        onChange: async () => {
          summary = await api.exceptionsSummary().catch(() => summary);
          renderMetrics(); renderFilters(); load(); ctx.refreshCounts();
        },
      });
      tr.addEventListener("click", open);
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
      return tr;
    });

    clear(gridSlot).append(
      el("div", { class: "grid-wrap" },
        el("div", { class: "grid-scroll" },
          el("table", { class: "grid" },
            el("thead", {}, el("tr", {},
              el("th", { text: "Status" }), el("th", { text: "Category" }),
              el("th", { text: "Entry & reason" }), el("th", { text: "Priority" }),
              el("th", { text: "Value date" }), el("th", { class: "amt", text: "Amount" }))),
            el("tbody", {}, ...rows))),
        el("div", { class: "grid-foot" },
          el("span", { text: `Showing ${num(data.items.length)} of ${num(data.total)}` }),
          el("span", { class: "spacer" }),
          el("span", { text: "Select a row to investigate" }))));
  }
}
