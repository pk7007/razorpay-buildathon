/* UI primitives: DOM building, icons, badges, states, toasts, drawer.

   Everything renders through `el`, which sets text via textContent — no
   innerHTML on data. Reconciliation rows carry bank narrations, which are
   attacker-controllable text; building them as nodes means a narration can
   never become markup. */

import { label } from "./format.js";

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;          // only for trusted icon SVG
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);

/* Node.append() stringifies null into the literal text "null". Every place we
   build a list of maybe-present sections needs this, not the raw method. */
export function add(host, ...kids) {
  for (const k of kids.flat()) {
    if (k == null || k === false) continue;
    host.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return host;
}
export const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };

/* ---------- icons: one family, 1.6 stroke, currentColor ---------- */
const P = {
  dashboard: "M3 3h7v7H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 14h7v7H3z",
  play: "M5 3.5v17l15-8.5z",
  inbox: "M3 13h5l1.5 3h5L16 13h5M3 13 6 4h12l3 9v7H3z",
  history: "M3 12a9 9 0 1 0 3-6.7M3 4v4h4",
  chart: "M3 3v18h18M7 15v3M12 9v9M17 12v6",
  upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3",
  close: "M6 6l12 12M18 6L6 18",
  check: "M4 12.5l5 5L20 6.5",
  alert: "M12 8v5M12 17h.01M10.3 3.9 1.9 18a2 2 0 0 0 1.7 3h16.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  info: "M12 16v-5M12 8h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z",
  spark: "M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1",
  menu: "M3 6h18M3 12h18M3 18h18",
  chevron: "M9 6l6 6-6 6",
  down: "M6 9l6 6 6-6",
  up: "M6 15l6-6 6 6",
  file: "M14 3v5h5M14 3H6v18h12V8z",
  link: "M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1",
  empty: "M9 3h6l1 4H8zM4 7h16l-1.5 14h-13z",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  note: "M4 4h16v12l-4 4H4zM16 20v-4h4",
  arrow: "M5 12h14M13 6l6 6-6 6",
  scale: "M12 4.5v15M4.5 8h15M8 20h8M4.5 8 2 13.5a2.6 2.6 0 0 0 5 0zM19.5 8 17 13.5a2.6 2.6 0 0 0 5 0z",
};

export function icon(name, size = 16) {
  const d = P[name] || P.info;
  const s = el("span", { class: "ico", "aria-hidden": "true" });
  s.innerHTML =
    `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" ` +
    `stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ` +
    `stroke-linejoin="round"><path d="${d}"/></svg>`;
  return s.firstChild;
}

/* ---------- status vocabulary ----------
   One mapping for the whole app, so a status never means two different things
   in two places. */

const GROUP_TONE = {
  complete:            ["ok",      "Complete"],
  awaiting_settlement: ["neutral", "Awaiting settlement"],
  awaiting_payout:     ["info",    "Awaiting payout"],
  payout_overdue:      ["risk",    "Payout overdue"],
  unbooked_payout:     ["warn",    "Unbooked payout"],
  fully_refunded:      ["neutral", "Fully refunded"],
  ambiguous_split:     ["warn",    "Ambiguous split"],
  partial:             ["warn",    "Partial"],
};

const EXC_TONE = {
  missing_in_bank:   "risk",
  missing_in_ledger: "warn",
  over_refunded:     "risk",
  orphan_chargeback: "risk",
  orphan_refund:     "warn",
  amount_mismatch:   "risk",
  currency_mismatch: "risk",
  fee_mismatch:      "warn",
  duplicate:         "warn",
  fx_or_adjustment:  "warn",
  split_settlement:  "info",
  merged_payout:     "info",
  payout_in_transit: "neutral",
  unknown:           "neutral",
};

/* Open is the default state of every new item, so it cannot also be the alarm
   state — a worklist where every row is red teaches people to ignore red.
   Urgency is carried by the priority column, which is derived from amount and
   category, not by the fact that nobody has touched the row yet. */
const WORK_TONE = {
  open: "warn", investigating: "info", resolved: "ok", written_off: "neutral",
};

const PRIORITY_TONE = {
  critical: "risk", high: "risk", medium: "warn", low: "neutral",
};

export const groupTone = (s) => GROUP_TONE[s] || ["neutral", label(s)];
export const excTone = (c) => EXC_TONE[c] || "neutral";
export const workTone = (s) => WORK_TONE[s] || "neutral";
export const priorityTone = (p) => PRIORITY_TONE[p] || "neutral";

/* `quiet` drops the fill and keeps the dot. Used where a column repeats the
   same value down every row — three filled amber pills on one row is three
   alarms competing, when only one of them is telling you anything. */
export function badge(text, tone = "neutral", { dot = true, title, quiet = false } = {}) {
  return el("span", {
    class: `badge badge-${tone}${dot ? "" : " no-dot"}${quiet ? " quiet" : ""}`,
    text,
    title: title || undefined,
  });
}

export const groupBadge = (status) => {
  const [tone, text] = groupTone(status);
  return badge(text, tone);
};

/* ---------- states ---------- */

export function emptyState({ icon: ic = "empty", title, body, action } = {}) {
  return el("div", { class: "state" },
    el("div", { class: "state-icon" }, icon(ic, 21)),
    el("h3", { text: title }),
    body && el("p", { text: body }),
    action || null,
  );
}

export function errorState(err, onRetry) {
  const e = err || {};
  return el("div", { class: "state is-error" },
    el("div", { class: "state-icon" }, icon("alert", 21)),
    el("h3", { text: e.message || "Something went wrong" }),
    e.hint && el("p", { text: e.hint }),
    e.requestId && el("p", { class: "mono", style: { fontSize: "11px" },
                             text: `request ${e.requestId}` }),
    onRetry && el("button", { class: "btn", onClick: onRetry }, icon("history"), "Try again"),
  );
}

export function skeleton(lines = 3, widths = ["100%", "82%", "64%"]) {
  return el("div", {}, ...Array.from({ length: lines }, (_, i) =>
    el("div", { class: "skel skel-line", style: { width: widths[i % widths.length] } })));
}

export function skeletonMetrics(n = 4) {
  return el("div", { class: "metrics" }, ...Array.from({ length: n }, () =>
    el("div", { class: "metric" },
      el("div", { class: "skel skel-line", style: { width: "52%", height: "9px" } }),
      el("div", { class: "skel", style: { width: "72%", height: "26px", marginTop: "6px" } }))));
}

export function skeletonGrid(rows = 6, cols = 5) {
  const head = el("tr", {}, ...Array.from({ length: cols }, () =>
    el("th", {}, el("div", { class: "skel skel-line", style: { width: "60%" } }))));
  const body = Array.from({ length: rows }, () =>
    el("tr", {}, ...Array.from({ length: cols }, () =>
      el("td", {}, el("div", { class: "skel skel-line",
                               style: { width: `${45 + Math.random() * 45}%` } })))));
  return el("div", { class: "grid-wrap" },
    el("div", { class: "grid-scroll" },
      el("table", { class: "grid" }, el("thead", {}, head), el("tbody", {}, ...body))));
}

/* ---------- toasts ---------- */

let toastHost = null;
export function toast(message, tone = "") {
  if (!toastHost) {
    toastHost = el("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(toastHost);
  }
  const ic = tone === "err" ? "alert" : tone === "ok" ? "check" : "info";
  const node = el("div", { class: `toast ${tone}` }, icon(ic), el("span", { text: message }));
  toastHost.append(node);
  setTimeout(() => node.remove(), tone === "err" ? 6000 : 3600);
  return node;
}

/* ---------- drawer ----------
   Focus is trapped while open and returned to the trigger on close, so a
   keyboard user is never dropped at the top of the document. */

let openDrawer = null;

export function drawer({ title, subtitle, body, footer, onClose }) {
  closeDrawer();
  const opener = document.activeElement;

  const scrim = el("div", { class: "scrim" });
  const panel = el("div", {
    class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": title || "Details",
  });

  const closeBtn = el("button", {
    class: "btn btn-ghost btn-icon", "aria-label": "Close", onClick: () => closeDrawer(),
  }, icon("close"));

  add(panel,
    el("div", { class: "drawer-head" },
      el("div", { class: "grow" },
        el("div", { class: "drawer-title", text: title || "" }),
        /* Always present, even when empty: callers fill it in after their
           fetch resolves, and a missing node silently drops that context. */
        el("div", { class: "topbar-sub", text: subtitle || "" })),
      closeBtn),
    el("div", { class: "drawer-body" }, body || ""),
    footer ? el("div", { class: "drawer-foot" }, footer) : null,
  );

  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); closeDrawer(); return; }
    if (e.key !== "Tab") return;
    const f = panel.querySelectorAll(
      'a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex="-1"])');
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };

  scrim.addEventListener("click", () => closeDrawer());
  document.addEventListener("keydown", onKey);
  document.body.append(scrim, panel);
  document.body.style.overflow = "hidden";

  openDrawer = { scrim, panel, onKey, opener, onClose };
  (panel.querySelector("[data-autofocus]") || closeBtn).focus();

  /* Panels open before their data lands, so the heading starts as a
     placeholder. Retitling has to move the accessible name too, or a screen
     reader announces "Loading…" for a dialog that has finished loading. */
  const setTitle = (t, sub) => {
    panel.querySelector(".drawer-title").textContent = t;
    panel.setAttribute("aria-label", t);
    if (sub != null) panel.querySelector(".drawer-head .topbar-sub").textContent = sub;
  };
  return { panel, close: closeDrawer, setTitle };
}

export function closeDrawer() {
  if (!openDrawer) return;
  const { scrim, panel, onKey, opener, onClose } = openDrawer;
  document.removeEventListener("keydown", onKey);
  scrim.remove(); panel.remove();
  document.body.style.overflow = "";
  openDrawer = null;
  if (opener && document.contains(opener)) opener.focus();
  if (onClose) onClose();
}

export const isDrawerOpen = () => openDrawer !== null;

/* ---------- small composites ---------- */

export function metric({ k, v, note, tone = "", icon: ic, title }) {
  return el("div", { class: `metric ${tone ? `is-${tone}` : ""}`, title },
    el("div", { class: "metric-k" }, ic ? icon(ic, 13) : null, el("span", { text: k })),
    el("div", { class: "metric-v", text: v }),
    note ? el("div", { class: "metric-note", text: note }) : null);
}

export function kv(pairs) {
  return el("div", { class: "kv" }, ...pairs.filter(Boolean).map(([k, v]) =>
    el("div", { class: "kv-item" },
      el("div", { class: "kv-k", text: k }),
      el("div", { class: "kv-v", text: v == null ? "—" : String(v) }))));
}

export function confidence(value) {
  const p = Math.max(0, Math.min(1, value || 0));
  return el("div", { class: "conf" },
    el("span", { class: "topbar-sub", text: "confidence" }),
    el("div", { class: "conf-track" },
      el("div", { class: "conf-fill", style: { width: `${p * 100}%` } })),
    el("strong", { text: `${Math.round(p * 100)}%` }));
}
