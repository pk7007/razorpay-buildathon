"use strict";
/* The exception worklist: the part a finance user actually lives in.
   Kept separate from app.js because it talks to the *stateful* half of the API
   (the persistent queue) rather than the reconciliation half. */

(function () {
  const $ = (s) => document.querySelector(s);
  const el = (t, cls, txt) => {
    const n = document.createElement(t);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  };

  const CAT_PILL = {
    missing_in_bank: "bad", missing_in_ledger: "warn", over_refunded: "bad",
    orphan_chargeback: "bad", orphan_refund: "warn", fee_mismatch: "warn",
    duplicate: "warn", split_settlement: "neutral", payout_in_transit: "neutral",
    currency_mismatch: "bad", amount_mismatch: "bad", unknown: "neutral",
  };
  const STATUS_PILL = {
    open: "bad", investigating: "warn", resolved: "ok", written_off: "neutral",
  };
  const PRIORITY_PILL = {
    critical: "bad", high: "bad", medium: "warn", low: "neutral",
  };
  // what a user is allowed to do next, mirroring the server's state machine so
  // the UI never offers a move the API will reject
  const NEXT = {
    open: ["investigating", "resolved", "written_off"],
    investigating: ["open", "resolved", "written_off"],
    resolved: ["open"],
    written_off: ["open"],
  };

  const money = (minor, cur) => {
    const v = (minor || 0) / 100;
    try {
      return new Intl.NumberFormat(cur === "INR" ? "en-IN" : "en-US", {
        style: "currency", currency: cur || "INR", maximumFractionDigits: 0,
      }).format(v);
    } catch { return (cur || "INR") + " " + v.toFixed(0); }
  };
  const nice = (s) => String(s || "").replace(/_/g, " ");
  const ago = (iso) => {
    if (!iso) return "";
    const secs = (Date.now() - new Date(iso).getTime()) / 1000;
    if (secs < 90) return "just now";
    if (secs < 5400) return Math.round(secs / 60) + "m ago";
    if (secs < 172800) return Math.round(secs / 3600) + "h ago";
    return Math.round(secs / 86400) + "d ago";
  };

  let CURRENT = null;

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.status + " " + res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* keep */ }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }

  // ------------------------------------------------------------------ list

  async function refresh() {
    const params = new URLSearchParams();
    const status = $("#q-status").value;
    const category = $("#q-category").value;
    const search = $("#q-search").value.trim();
    if (status) params.set("status", status);
    if (category) params.set("category", category);
    if (search) params.set("q", search);
    params.set("sort", $("#q-sort").value);
    params.set("limit", "200");

    const list = $("#queue-list");
    list.innerHTML = "";
    list.append(el("p", "hint", "Loading…"));
    try {
      const [data, summary] = await Promise.all([
        api("/api/exceptions?" + params.toString()),
        api("/api/exceptions/summary"),
      ]);
      renderKpis(summary);
      renderCategories(summary);
      renderList(data);
      const badge = $("#queue-badge");
      if (badge) badge.textContent = summary.open_count ? String(summary.open_count) : "";
    } catch (err) {
      list.innerHTML = "";
      list.append(el("p", "hint", "Could not load the worklist: " + err.message));
    }
  }

  function renderKpis(s) {
    const box = $("#queue-kpis");
    box.innerHTML = "";
    const cells = [
      [s.open_count || 0, "open items", s.open_count ? "" : "good"],
      [money(s.open_value_minor, "INR"), "value at stake", ""],
      [s.carried_forward || 0, "carried forward", ""],
      [(s.by_status && s.by_status.resolved) || 0, "resolved", "good"],
    ];
    for (const [v, l, cls] of cells) {
      const k = el("div", "kpi " + cls);
      k.append(el("div", "v", String(v)));
      k.append(el("div", "l", l));
      box.append(k);
    }
  }

  function renderCategories(s) {
    const sel = $("#q-category");
    const keep = sel.value;
    sel.innerHTML = "";
    sel.append(new Option("all categories", ""));
    for (const [cat, n] of Object.entries(s.by_category || {})) {
      sel.append(new Option(`${nice(cat)} (${n})`, cat));
    }
    sel.value = keep;
  }

  function renderList(data) {
    const list = $("#queue-list");
    list.innerHTML = "";
    $("#queue-sub").textContent =
      `${data.total} item${data.total === 1 ? "" : "s"} · exceptions persist between runs, ` +
      `and clear themselves when a later run explains them.`;
    if (!data.items.length) {
      list.append(el("p", "hint", "Nothing here. Either the books tie out, or try a wider filter."));
      return;
    }
    for (const x of data.items) {
      const row = el("div", "row exc");
      const head = el("div", "row-head");
      head.tabIndex = 0;
      head.setAttribute("role", "button");
      head.append(el("span", "pill " + (STATUS_PILL[x.status] || "neutral"), nice(x.status)));
      head.append(el("span", "pill " + (CAT_PILL[x.category] || "neutral"), nice(x.category)));
      head.append(el("span", "pill " + (PRIORITY_PILL[x.priority] || "neutral"), x.priority));
      head.append(el("span", "id", x.entry_id));
      if (x.times_seen > 1) {
        head.append(el("span", "pill warn", `seen ${x.times_seen}×`));
      }
      if (x.assignee) head.append(el("span", "pill neutral", "@" + x.assignee));
      head.append(el("span", "amt", money(x.amount_minor, x.currency)));
      const open = () => openDrawer(x.id);
      head.addEventListener("click", open);
      head.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
      });
      row.append(head, el("div", "row-why", x.rationale));
      list.append(row);
    }
  }

  // ---------------------------------------------------------------- drawer

  async function openDrawer(id) {
    $("#drawer").classList.remove("hidden");
    $("#scrim").classList.remove("hidden");
    const body = $("#drawer-body");
    body.innerHTML = "";
    body.append(el("div", "spinner"));
    try {
      CURRENT = await api("/api/exceptions/" + encodeURIComponent(id));
      renderDrawer(CURRENT);
    } catch (err) {
      body.innerHTML = "";
      body.append(el("p", "hint", err.message));
    }
  }

  function closeDrawer() {
    $("#drawer").classList.add("hidden");
    $("#scrim").classList.add("hidden");
    CURRENT = null;
  }

  function renderDrawer(x) {
    $("#drawer-title").textContent = nice(x.category);
    const b = $("#drawer-body");
    b.innerHTML = "";

    const facts = el("div", "facts");
    const add = (k, v) => {
      const d = el("div", "fact");
      d.append(el("span", "fk", k));
      d.append(el("span", "fv", v));
      facts.append(d);
    };
    add("amount", money(x.amount_minor, x.currency));
    add("entry", x.entry_id);
    add("source", x.source);
    add("value date", x.value_date);
    add("status", nice(x.status));
    add("priority", x.priority);
    add("confidence", (x.confidence * 100).toFixed(0) + "%");
    add("seen in runs", String(x.times_seen));
    if (x.assignee) add("assignee", x.assignee);
    if (x.resolution_reason) add("resolution", x.resolution_reason);
    b.append(facts);

    b.append(el("p", "why-block", x.rationale));
    const act = el("p", "action");
    act.append(el("b", null, "Do: "));
    act.append(document.createTextNode(x.suggested_action));
    b.append(act);

    // actions the server will actually accept
    const bar = el("div", "drawer-actions");
    for (const next of (NEXT[x.status] || [])) {
      const btn = el("button", next === "resolved" ? "run" : "ghost", nice(next));
      btn.onclick = () => move(x.id, next);
      bar.append(btn);
    }
    b.append(bar);

    const assignWrap = el("div", "assign-row");
    const who = el("input", "inp");
    who.placeholder = "assign to…";
    who.value = x.assignee || "";
    const assignBtn = el("button", "ghost", "Assign");
    assignBtn.onclick = async () => {
      await patch(x.id, { assignee: who.value.trim() || null });
    };
    assignWrap.append(who, assignBtn);
    b.append(assignWrap);

    const noteWrap = el("div", "note-row");
    const note = el("textarea", "inp");
    note.placeholder = "Add a note — what you checked, who you called…";
    note.rows = 2;
    const noteBtn = el("button", "ghost", "Add note");
    noteBtn.onclick = async () => {
      const body = note.value.trim();
      if (!body) return;
      try {
        CURRENT = await api(`/api/exceptions/${encodeURIComponent(x.id)}/notes`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ body }),
        });
        renderDrawer(CURRENT);
      } catch (err) { flash(err.message); }
    };
    noteWrap.append(note, noteBtn);
    b.append(noteWrap);

    b.append(el("h4", "hist-h", "History"));
    const hist = el("div", "hist");
    for (const h of x.history || []) {
      const line = el("div", "hist-line");
      line.append(el("span", "hist-kind", nice(h.kind)));
      const txt = h.kind === "status"
        ? `${nice(h.from_status)} → ${nice(h.to_status)}${h.body ? " · " + h.body : ""}`
        : (h.body || "");
      line.append(el("span", "hist-body", txt));
      line.append(el("span", "hist-when", `${h.actor} · ${ago(h.at)}`));
      hist.append(line);
    }
    b.append(hist);
  }

  async function move(id, status) {
    let reason = null;
    if (status === "resolved" || status === "written_off") {
      reason = prompt(`Why is this ${nice(status)}?`, "");
      if (reason === null) return;      // cancelled
    }
    await patch(id, { status, reason });
  }

  async function patch(id, payload) {
    try {
      CURRENT = await api("/api/exceptions/" + encodeURIComponent(id), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      renderDrawer(CURRENT);
      refresh();
    } catch (err) {
      flash(err.message);
    }
  }

  function flash(msg) {
    const n = el("div", "toast", msg);
    document.body.append(n);
    setTimeout(() => n.remove(), 4000);
  }

  // ------------------------------------------------------------------ wire

  function showView(view) {
    const isQueue = view === "queue";
    document.querySelector("main").classList.toggle("hidden", isQueue);
    $("#queue-view").classList.toggle("hidden", !isQueue);
    for (const b of $("#viewnav").children) {
      b.classList.toggle("active", b.dataset.view === view);
    }
    if (isQueue) refresh();
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("#viewnav").addEventListener("click", (e) => {
      if (e.target.dataset && e.target.dataset.view) showView(e.target.dataset.view);
    });
    $("#queue-refresh").onclick = refresh;
    $("#drawer-close").onclick = closeDrawer;
    $("#scrim").onclick = closeDrawer;
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
    });
    for (const id of ["#q-status", "#q-category", "#q-sort"]) {
      $(id).addEventListener("change", refresh);
    }
    let t;
    $("#q-search").addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(refresh, 250);
    });
    // keep the badge live even while the user is on the reconcile view
    api("/api/exceptions/summary").then((s) => {
      const badge = $("#queue-badge");
      if (badge && s.open_count) badge.textContent = String(s.open_count);
    }).catch(() => {});
  });

  window.__queueRefresh = refresh;   // app.js calls this after a run
})();
