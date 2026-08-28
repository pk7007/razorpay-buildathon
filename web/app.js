"use strict";

const $ = (s) => document.querySelector(s);
const el = (t, cls, txt) => {
  const n = document.createElement(t);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};
// an expandable row header: focusable, toggles on Enter/Space, tracks aria-expanded
const expander = (onToggle) => {
  const h = el("div", "row-head");
  h.tabIndex = 0;
  h.setAttribute("role", "button");
  h.setAttribute("aria-expanded", "false");
  const toggle = () => {
    const open = h.getAttribute("aria-expanded") === "true";
    h.setAttribute("aria-expanded", String(!open));
    onToggle(!open);
  };
  h.addEventListener("click", toggle);
  h.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  return h;
};
const INR = (paise) =>
  "₹" + (paise / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const INR2 = (paise) =>
  "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (x) => (x == null ? "–" : (x * 100).toFixed(1) + "%");

const STATUS_PILL = {
  complete: ["ok", "complete"],
  awaiting_settlement: ["neutral", "awaiting settlement"],
  awaiting_payout: ["neutral", "awaiting payout"],
  payout_overdue: ["bad", "payout overdue"],
  unbooked_payout: ["warn", "unbooked payout"],
  partial: ["warn", "partial"],
};
const CAT_PILL = {
  missing_in_bank: "bad", missing_in_ledger: "warn", payout_in_transit: "neutral",
  fee_mismatch: "warn", duplicate: "warn", split_settlement: "neutral",
  merged_payout: "neutral", fx_or_adjustment: "warn", amount_mismatch: "bad", unknown: "neutral",
};

let RESULT = null;
let ENTRY_BY_ID = {};

document.addEventListener("DOMContentLoaded", init);

async function init() {
  try {
    const h = await (await fetch("/api/health")).json();
    $("#resolver-badge").textContent = "resolver: " + h.resolver;
  } catch { /* non-fatal */ }

  try {
    const sets = await (await fetch("/api/datasets")).json();
    const box = $("#dataset-cards");
    box.innerHTML = "";
    for (const d of sets) {
      const c = el("button", "card");
      c.append(el("h3", null, d.label + " month"));
      c.append(el("p", null, d.blurb));
      c.append(el("div", "go", "Reconcile →"));
      c.onclick = () => runDataset(d.name, d.label);
      box.append(c);
    }
  } catch (e) {
    showError("Could not reach the API. Is the server running?");
  }

  $("#run-again").onclick = () => location.reload();
  $("#upload-form").addEventListener("submit", onUpload);
  $("#tabs").addEventListener("click", (e) => {
    if (e.target.tagName === "BUTTON") switchTab(e.target.dataset.tab);
  });
}

function show(id) {
  for (const s of ["intro", "loading", "error", "results"]) $("#" + s).classList.toggle("hidden", s !== id);
}
function showError(msg) { show("error"); $("#error-msg").textContent = msg; }

async function runDataset(name, label) {
  show("loading");
  $("#loading-msg").textContent = `Reconciling the ${label.toLowerCase()} month…`;
  await runRequest("/api/reconcile", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dataset: name }) }, label + " month");
}

async function onUpload(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  for (const [k, v] of [...fd.entries()]) if (v instanceof File && v.size === 0) fd.delete(k);
  if (![...fd.keys()].length) return showError("Choose at least one file first.");
  show("loading");
  $("#loading-msg").textContent = "Reconciling your exports…";
  await runRequest("/api/reconcile/upload", { method: "POST", body: fd }, "Uploaded exports");
}

async function runRequest(url, opts, title) {
  const t0 = performance.now();
  try {
    const res = await fetch(url, opts);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `${res.status} ${res.statusText}`);
    }
    RESULT = await res.json();
    ENTRY_BY_ID = Object.fromEntries((RESULT.entries || []).map((x) => [x.id, x]));
    render(title, Math.round(performance.now() - t0));
  } catch (err) {
    showError(err.message || String(err));
  }
}

function render(title, roundtripMs) {
  const m = RESULT.metrics, mo = RESULT.money;
  $("#result-title").textContent = title;
  show("results");

  const kpis = [
    ["auto-match", pct(m.auto_match_rate), "good"],
    ["precision", pct(m.precision), m.precision === 1 ? "good" : ""],
    ["recall", pct(m.recall), m.recall === 1 ? "good" : ""],
    ["exception accuracy", m.exception_category_accuracy == null ? "n/a" : pct(m.exception_category_accuracy), ""],
    ["engine time", m.latency_ms + " ms", ""],
    ["replay", m.replay_stable ? "stable ✓" : "unstable", m.replay_stable ? "good" : ""],
  ];
  const kb = $("#kpis"); kb.innerHTML = "";
  for (const [l, v, cls] of kpis) {
    const k = el("div", "kpi " + cls);
    k.append(el("div", "v", v));
    k.append(el("div", "l", l));
    kb.append(k);
  }

  renderMoney(mo);
  renderGroups();
  renderExceptions();
  renderAudit();
  renderMetrics(m, roundtripMs);
  switchTab("groups", false);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderMoney(mo) {
  const parts = [
    ["reconciled", mo.reconciled_paise, "var(--ok)", "tied across payment, settlement, bank and ledger"],
    ["in transit", mo.in_transit_paise, "var(--muted)", "settled or awaiting payout within the normal cycle"],
    ["recoverable", mo.recoverable_paise, "var(--bad)", "booked revenue that never reached the bank — chase it"],
    ["unrecorded", mo.unrecorded_paise, "var(--warn)", "bank credits with no ledger entry"],
  ];
  const tot = parts.reduce((s, p) => s + p[1], 0) || 1;
  const bar = $("#money-bar"); bar.innerHTML = "";
  for (const [name, val, col] of parts) {
    if (val <= 0) continue;
    const s = el("span");
    s.style.width = `max(6px, ${(100 * val / tot).toFixed(2)}%)`;
    s.style.background = col;
    s.title = `${name}: ${INR(val)}`;
    bar.append(s);
  }
  const leg = $("#money-legend"); leg.innerHTML = "";
  for (const [name, val, col, note] of parts) {
    const li = el("div", "li");
    const sw = el("span", "sw"); sw.style.background = col;
    li.append(sw);
    li.append(document.createTextNode(name + " "));
    li.append(el("span", "amt", INR(val)));
    li.append(el("small", null, note));
    leg.append(li);
  }
}

function entryTable(ids) {
  const wrap = el("div", "row-detail");
  const t = el("table");
  t.innerHTML = "<tr><th>id</th><th>source</th><th>date</th><th class='num'>amount</th><th>reference</th><th>narration</th></tr>";
  for (const id of ids) {
    const e = ENTRY_BY_ID[id];
    const tr = el("tr");
    if (!e) { tr.innerHTML = `<td>${id}</td><td colspan=5>(not found)</td>`; t.append(tr); continue; }
    tr.innerHTML =
      `<td>${e.id}</td><td>${e.source}</td><td>${e.value_date}</td>` +
      `<td class='num'>${INR2(e.amount_paise)}</td><td>${e.reference || "–"}</td><td>${e.narration || "–"}</td>`;
    t.append(tr);
  }
  wrap.append(t);
  return wrap;
}

function renderGroups() {
  const groups = RESULT.groups;
  const counts = {};
  for (const g of groups) counts[g.status] = (counts[g.status] || 0) + 1;
  const fb = $("#group-filters"); fb.innerHTML = "";
  const mk = (key, label, n) => {
    const c = el("button", "chip" + (key === "all" ? " active" : ""), `${label} ${n}`);
    c.dataset.key = key;
    c.onclick = () => {
      [...fb.children].forEach((x) => x.classList.toggle("active", x === c));
      paintGroups(key);
    };
    fb.append(c);
  };
  mk("all", "all", groups.length);
  for (const [k, n] of Object.entries(counts)) mk(k, (STATUS_PILL[k] || [, k])[1], n);
  paintGroups("all");
}

function paintGroups(filter) {
  const list = $("#groups-list"); list.innerHTML = "";
  const groups = RESULT.groups.filter((g) => filter === "all" || g.status === filter);
  for (const g of groups) {
    const row = el("div", "row");
    const detail = entryTable(g.entry_ids);
    detail.classList.add("hidden");
    const head = expander((open) => detail.classList.toggle("hidden", !open));
    const [cls, label] = STATUS_PILL[g.status] || ["neutral", g.status];
    head.append(el("span", "pill " + cls, label));
    head.append(el("span", "id", g.group_id + " · " + g.sources.join("+")));
    head.append(el("span", "pill neutral", "conf " + g.confidence.toFixed(2)));
    head.append(el("span", "amt", INR(g.amount_paise)));
    head.setAttribute("aria-label",
      `Group ${g.group_id}, ${label}, ${INR(g.amount_paise)} — expand for entries`);
    const why = el("div", "row-why", g.rule + " — " + g.rationale);
    row.append(head, why, detail);
    list.append(row);
  }
  if (!groups.length) list.append(el("p", "hint", "Nothing in this bucket."));
}

function renderExceptions() {
  const list = $("#exceptions-list"); list.innerHTML = "";
  $('[data-tab="exceptions"]').textContent = `Exceptions (${RESULT.exceptions.length})`;
  if (!RESULT.exceptions.length) {
    list.append(el("p", "hint", "No exceptions — every entry reconciled."));
    return;
  }
  for (const x of RESULT.exceptions) {
    const row = el("div", "row exc");
    const detail = entryTable([x.entry_id]);
    detail.classList.add("hidden");
    const head = expander((open) => detail.classList.toggle("hidden", !open));
    head.append(el("span", "pill " + (CAT_PILL[x.category] || "neutral"),
      x.category.replace(/_/g, " ")));
    head.append(el("span", "id", x.entry_id + " · " + x.source));
    head.append(el("span", "pill neutral", "conf " + x.confidence.toFixed(2)));
    head.append(el("span", "amt", INR2(x.amount_paise)));
    head.setAttribute("aria-label",
      `${x.category.replace(/_/g, " ")}, ${x.entry_id}, ${INR2(x.amount_paise)} — expand for the row`);
    const why = el("div", "row-why", x.rationale);
    const action = el("div", "action");
    action.append(el("b", null, "Do: "));
    action.append(document.createTextNode(x.suggested_action));
    row.append(head, why, action, detail);
    list.append(row);
  }
}

function renderAudit() {
  const list = $("#audit-list"); list.innerHTML = "";
  for (const a of RESULT.audit) {
    const row = el("div", "row");
    row.append(el("span", "seq", "#" + a.seq));
    const sc = el("span", "stage-cell");
    sc.append(el("span", "pill neutral", a.stage));
    row.append(sc);
    const why = el("div", "why");
    why.innerHTML = `<b>${a.outcome}</b> · ${a.rule} · conf ${a.confidence} — ${escapeHtml(a.rationale)}`;
    row.append(why);
    list.append(row);
  }
}

function renderMetrics(m, roundtripMs) {
  const target = { precision: 0.99, recall: 0.95, f1: 0.97, auto_match_rate: 0.9, exception_category_accuracy: 0.8 };
  const cells = [
    ["precision", m.precision], ["recall", m.recall], ["F1", m.f1],
    ["auto-match rate", m.auto_match_rate], ["exception accuracy", m.exception_category_accuracy],
  ];
  let html = '<div class="metric-grid">';
  for (const [label, val] of cells) {
    const key = label.toLowerCase().replace(/[ -]/g, "_").replace("f1", "f1");
    const t = target[key === "f1" ? "f1" : key] ?? null;
    const w = val == null ? 0 : Math.round(val * 100);
    html += `<div class="m"><div class="mv">${pct(val)}</div><div class="ml">${label}</div>` +
      `<div class="bar"><i style="width:${w}%"></i></div>` +
      (t ? `<div class="mt">target ≥ ${(t * 100).toFixed(0)}%</div>` : "") + `</div>`;
  }
  html += "</div>";

  html += '<div class="metric-grid" style="margin-top:12px">';
  html += metricCard("deterministic groups", pct(m.deterministic_share));
  html += metricCard("structural groups", pct(m.structural_share));
  html += metricCard("resolver groups", pct(m.resolver_share));
  html += metricCard("entries", m.total_entries);
  html += metricCard("matched / exceptions", `${m.matched_entries} / ${m.exceptions}`);
  html += metricCard("engine latency", m.latency_ms + " ms");
  html += metricCard("request round-trip", roundtripMs + " ms");
  if (m.llm_calls) {
    html += metricCard("LLM calls", m.llm_calls);
    html += metricCard("LLM tokens", `${m.llm_input_tokens} in / ${m.llm_output_tokens} out`);
    html += metricCard("LLM cost", "$" + m.llm_cost_usd.toFixed(4));
  }
  html += "</div>";
  $("#metrics-body").innerHTML = html;
}
function metricCard(label, val) {
  return `<div class="m"><div class="mv">${val}</div><div class="ml">${label}</div></div>`;
}

function switchTab(name, keepScroll = true) {
  for (const b of $("#tabs").children) {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  }
  for (const p of document.querySelectorAll(".tabpane")) p.classList.add("hidden");
  $("#tab-" + name).classList.remove("hidden");
  if (keepScroll) {
    const tabsTop = $("#tabs").getBoundingClientRect().top + window.scrollY - 70;
    if (window.scrollY > tabsTop) window.scrollTo({ top: tabsTop });
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
