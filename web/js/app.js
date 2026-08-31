/* App shell: rail, topbar, routing, and the counts that make the rail useful.

   The open-exception count lives in the rail because it is the one number a
   controller wants visible on every screen — it is the size of their queue. */

import { api } from "./api.js";
import { num } from "./format.js";
import { el, clear, icon, toast, closeDrawer, isDrawerOpen } from "./ui.js";
import { createRouter } from "./router.js";

import { dashboard } from "./views/dashboard.js";
import { reconcile } from "./views/reconcile.js";
import { exceptions } from "./views/exceptions.js";
import { runs, evidence, importView } from "./views/misc.js";

const NAV = [
  { group: "Close" },
  { id: "dashboard",  label: "Overview",    icon: "dashboard" },
  { id: "reconcile",  label: "Reconcile",   icon: "play" },
  { id: "exceptions", label: "Worklist",    icon: "inbox", counter: true },
  { group: "Records" },
  { id: "runs",       label: "Run history", icon: "history" },
  { id: "import",     label: "Import files", icon: "upload" },
  { group: "Proof" },
  { id: "evidence",   label: "Accuracy",    icon: "chart" },
];

const VIEWS = { dashboard, reconcile, exceptions, runs, evidence, import: importView };
const TITLES = {
  dashboard: "Overview", reconcile: "Reconcile", exceptions: "Worklist",
  runs: "Run history", import: "Import files", evidence: "Accuracy",
};

/* ---------- theme: explicit choice wins, system is the default ---------- */

const THEME_KEY = "afc-theme";
function readTheme() {
  try { return localStorage.getItem(THEME_KEY) || "system"; } catch { return "system"; }
}
function applyTheme(mode) {
  if (mode === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
  try { localStorage.setItem(THEME_KEY, mode); } catch { /* private mode */ }
}

/* ---------- shell ---------- */

const rail = el("aside", { class: "rail", id: "rail" });
const main = el("main", { class: "main" });
const content = el("div", { id: "content", tabindex: "-1" });
const pageTitle = el("h1", { text: "Overview" });
const pageSub = el("span", { class: "topbar-sub" });
const counts = {};
let runShortcut = null;
let railToggle = null;

/* Off-canvas on a phone, the rail covers the page. Anything that obscures the
   page has to close on Escape as well as on a tap, and it has to hand focus
   over when it opens or a keyboard user is left behind it. */
function setRail(open) {
  rail.dataset.open = String(open);
  if (railToggle) railToggle.setAttribute("aria-expanded", String(open));
  if (open) {
    const first = rail.querySelector(".nav-item");
    if (first) first.focus();
  } else if (railToggle && document.activeElement && rail.contains(document.activeElement)) {
    railToggle.focus();
  }
}

function buildRail() {
  const nav = el("nav", { class: "nav", "aria-label": "Sections" });
  for (const item of NAV) {
    if (item.group) { nav.append(el("div", { class: "nav-label", text: item.group })); continue; }
    const badgeEl = item.counter ? el("span", { class: "nav-count" }) : null;
    if (badgeEl) counts[item.id] = badgeEl;
    nav.append(el("a", {
      class: "nav-item", href: `#/${item.id}`, dataset: { route: item.id },
    }, icon(item.icon), el("span", { text: item.label }), badgeEl));
  }

  const themeBtn = el("button", {
    class: "nav-item", type: "button",
    onClick: () => {
      const next = { system: "light", light: "dark", dark: "system" }[readTheme()];
      applyTheme(next);
      paintThemeBtn(themeBtn);
      toast(`Theme: ${next}`, "");
    },
  });
  paintThemeBtn(themeBtn);

  rail.append(
    el("div", { class: "brand" },
      el("div", { class: "brand-mark" }, icon("scale", 17)),
      el("div", {},
        el("div", { class: "brand-name", text: "Finance Controller" }),
        el("div", { class: "brand-sub", text: "Reconciliation" }))),
    nav,
    el("div", { class: "rail-foot" }, themeBtn,
      el("a", { class: "nav-item", href: "/docs", target: "_blank", rel: "noopener" },
        icon("file"), el("span", { text: "API reference" }))));
}

function paintThemeBtn(btn) {
  const mode = readTheme();
  clear(btn).append(icon(mode === "dark" ? "spark" : "info"),
    el("span", { text: `Theme: ${mode}` }));
}

function buildTopbar() {
  const toggle = el("button", {
    class: "btn btn-ghost btn-icon rail-toggle", "aria-label": "Open navigation",
    "aria-expanded": "false", "aria-controls": "rail",
    onClick: () => setRail(rail.dataset.open !== "true"),
  }, icon("menu"));
  railToggle = toggle;

  const scrim = el("div", { class: "rail-scrim", onClick: () => setRail(false) });

  runShortcut = el("button", {
    class: "btn btn-sm", onClick: () => (location.hash = "#/reconcile"),
  }, icon("play"), "Run reconciliation");

  return el("header", { class: "topbar" }, toggle,
    el("div", { class: "grow" }, pageTitle, pageSub),
    el("div", { class: "topbar-actions" }, runShortcut),
    scrim);
}

/* ---------- counts ---------- */

async function refreshCounts() {
  try {
    const s = await api.exceptionsSummary();
    const n = s.open_count || 0;
    const node = counts.exceptions;
    if (!node) return;
    node.textContent = n ? num(n) : "";
    node.classList.toggle("alert", n > 0);
    node.title = n ? `${n} unresolved exceptions` : "";
  } catch { /* the rail badge is a nicety, never a blocker */ }
}

/* ---------- routing ---------- */

const ctx = { refreshCounts };

function navigate(route, params, isCurrent) {
  for (const a of rail.querySelectorAll(".nav-item[data-route]")) {
    if (a.dataset.route === route) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  pageTitle.textContent = TITLES[route] || "Overview";
  // On the reconcile screen the shortcut points at the screen you are already
  // on, so it is noise rather than a shortcut.
  if (runShortcut) runShortcut.classList.toggle("hidden", route === "reconcile");
  document.title = `${TITLES[route] || "Overview"} · AI Finance Controller`;
  if (isDrawerOpen()) closeDrawer();
  setRail(false);

  clear(content);
  const view = VIEWS[route] || dashboard;
  Promise.resolve(view(content, ctx, params)).catch((err) => {
    if (!isCurrent()) return;
    console.error(err);
    toast(err.message || "That screen failed to load", "err");
  });
  window.scrollTo({ top: 0, behavior: "auto" });
}

/* ---------- boot ---------- */

applyTheme(readTheme());
buildRail();
main.append(buildTopbar(), content);
document.getElementById("app").append(rail, main);

const router = createRouter(Object.keys(VIEWS), navigate);
router.start();
refreshCounts();

/* Keyboard: g-then-key jumps between sections, the way every console a
   finance analyst already uses does. Escape closes whatever is open. */
let pending = null;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && rail.dataset.open === "true") {
    e.preventDefault();
    setRail(false);
    return;
  }
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || e.metaKey || e.ctrlKey) return;
  if (e.key === "g") { pending = setTimeout(() => (pending = null), 1200); return; }
  if (!pending) return;
  clearTimeout(pending); pending = null;
  const jump = { o: "dashboard", r: "reconcile", w: "exceptions",
                 h: "runs", i: "import", a: "evidence" }[e.key];
  if (jump) { e.preventDefault(); location.hash = `#/${jump}`; }
});

/* Health line in the topbar: states what resolved this run, rather than
   letting the reader assume a model was involved when none was. */
api.health().then((h) => {
  pageSub.textContent = h.resolver === "llm"
    ? "LLM-assisted resolver · deterministic first"
    : "Deterministic resolver · no model in the loop";
}).catch(() => { pageSub.textContent = ""; });
