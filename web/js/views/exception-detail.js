/* Exception detail — the investigation surface.

   A finance user opens this to answer one question: *why didn't this tie out,
   and what do I do?* So the order is fixed — what it is, why the engine says
   so, what it checked, what to do, then the paper trail.

   Nothing here is invented. The "what the engine checked" list describes the
   rules that actually ran for that category; it never asserts a number the API
   did not return. */

import { api } from "../api.js";
import { money, label, sentence, titleCase, ago, date } from "../format.js";
import {
  el, clear, add, icon, badge, drawer, closeDrawer, toast,
  workTone, priorityTone, confidence, skeleton, errorState,
} from "../ui.js";

/* What each rule family actually examined. Wording matches the engine. */
const CHECKS = {
  missing_in_bank: [
    "Searched every bank credit within the payout window for a comparable amount",
    "No settlement or payout references this entry",
    "The money is booked but has not arrived — this is recoverable revenue",
  ],
  missing_in_ledger: [
    "Matched the bank credit against every settlement by UTR — no match",
    "Searched the ledger for an entry of a comparable amount — none found",
    "Income has been received but never recorded",
  ],
  fee_mismatch: [
    "A settlement shares this UTR, so the payout is identified",
    "The credit is short of the settlement net by a small amount",
    "Consistent with a bank or processing charge deducted in transit",
  ],
  duplicate: [
    "Another row in the same source carries the same amount and date",
    "Both sit inside a group that already reconciles once",
    "Booking it twice would overstate income",
  ],
  orphan_refund: [
    "The refund names a payment id that is not in this batch",
    "No payment was matched on amount alone — the engine refuses to guess",
    "The original payment is most likely in an earlier period",
  ],
  orphan_chargeback: [
    "The chargeback names a payment that is not in this batch",
    "The disputed payment is likely from an earlier period",
  ],
  over_refunded: [
    "Refunds against this payment sum to more than the payment itself",
    "That is arithmetically impossible — the source data disagrees with itself",
  ],
  split_settlement: [
    "Several same-day subsets sum to the same payout amount",
    "Amounts alone cannot decide which payments this batch paid out",
    "Resolving it needs the settlement recon report, or a human",
  ],
  currency_mismatch: [
    "A counterpart exists at the same numeric amount but in another currency",
    "The engine never nets across currencies without an explicit rate",
  ],
  payout_in_transit: [
    "A settlement was raised but the bank credit has not landed yet",
    "Within the normal payout cycle — expected to clear",
  ],
  amount_mismatch: [
    "A near-amount counterpart exists but no clean match was possible",
  ],
  unknown: [
    "No structural signal tied this entry to anything else",
  ],
};

const NEEDS_REASON = new Set(["resolved", "written_off"]);

const NEXT_STATUS = {
  open:          [["investigating", "Start investigating", ""],
                  ["resolved", "Resolve", "primary"],
                  ["written_off", "Write off", ""]],
  investigating: [["resolved", "Resolve", "primary"],
                  ["written_off", "Write off", ""],
                  ["open", "Back to open", ""]],
  resolved:      [["open", "Reopen", ""]],
  written_off:   [["open", "Reopen", ""]],
};

export async function showException(id, { onChange } = {}) {
  const body = el("div", {}, skeleton(6));
  const handle = drawer({ title: "Loading…", body });

  let x;
  try {
    x = await api.exception(id);
  } catch (err) {
    add(clear(body), errorState(err, () => { closeDrawer(); showException(id, { onChange }); }));
    return;
  }
  render(x);

  function render(exc) {
    handle.setTitle(sentence(exc.category), `${exc.entry_id} · ${exc.source}`);

    add(clear(body),
      /* 1. what it is ------------------------------------------------------ */
      el("div", { class: "row gap-2", style: { flexWrap: "wrap", marginBottom: "14px" } },
        badge(label(exc.status), workTone(exc.status)),
        badge(`${exc.priority} priority`, priorityTone(exc.priority)),
        exc.times_seen > 1
          ? badge(`seen ${exc.times_seen}×`, "warn",
                  { title: "Carried forward — it has survived more than one run" })
          : null,
        exc.assignee ? badge(exc.assignee, "accent", { dot: false }) : null),

      el("div", { class: "metric", style: { marginBottom: "14px" } },
        el("div", { class: "metric-k", text: "Amount at stake" }),
        el("div", { class: "metric-v", text: money(exc.amount_minor, exc.currency) }),
        el("div", { class: "metric-note",
                    text: `${exc.source} · value date ${date(exc.value_date)}` })),

      /* 2. why the engine says so ------------------------------------------ */
      section("Why this did not reconcile",
        el("div", { class: "rule-why", text: exc.rationale })),

      /* 3. what it checked -------------------------------------------------- */
      section("What the engine checked",
        el("div", { class: "evidence" },
          el("div", { class: "evidence-head" }, icon("scale", 14), "Evidence"),
          el("ul", {}, ...(CHECKS[exc.category] || CHECKS.unknown).map((c) =>
            el("li", { text: c }))),
          confidence(exc.confidence),
          el("div", { class: "cell-sub", style: { marginTop: "6px", fontSize: "11px" },
                      text: "Confidence describes how strongly the rule matched. It is "
                          + "the engine's certainty about the category, not a promise "
                          + "about the money." }))),

      /* 4. what to do -------------------------------------------------------- */
      section("Suggested action",
        el("div", { class: "row gap-2", style: { alignItems: "flex-start" } },
          icon("arrow", 16),
          el("div", { class: "grow", text: exc.suggested_action }))),

      exc.resolution_reason
        ? section("Resolution", el("div", { class: "rule-why", text: exc.resolution_reason }))
        : null,

      /* 5. add to the record ------------------------------------------------ */
      section("Add a note", noteBox(exc)),
      section("Assign", assignBox(exc)),

      /* 6. the paper trail -------------------------------------------------- */
      section(`History (${(exc.history || []).length})`, timeline(exc.history || [])),
    );

    /* footer: only transitions the server will accept ---------------------- */
    const foot = handle.panel.querySelector(".drawer-foot")
      || (() => {
        const f = el("div", { class: "drawer-foot" });
        handle.panel.append(f);
        return f;
      })();
    buttons(foot, exc);
  }

  function buttons(foot, exc) {
    clear(foot);
    for (const [to, text, kind] of (NEXT_STATUS[exc.status] || [])) {
      foot.append(el("button", {
        class: `btn ${kind === "primary" ? "btn-primary" : ""}`,
        onClick: () => (NEEDS_REASON.has(to) ? askReason(foot, exc, to, text) : move(exc, to)),
      }, text));
    }
  }

  /* Closing an exception is a bookkeeping event: the reason becomes part of the
     audit trail, so it is asked for in the panel — where the evidence is still
     on screen — rather than in a browser prompt that hides it. */
  function askReason(foot, exc, to, verb) {
    clear(foot);
    const input = el("input", {
      class: "input", "data-autofocus": "",
      placeholder: to === "resolved"
        ? "What settled it? (e.g. bank confirmed the credit on 12 Jul)"
        : "Why is this being written off?",
      "aria-label": `Reason for ${label(to)}`,
    });
    const confirm = el("button", { class: "btn btn-primary" }, verb);
    confirm.addEventListener("click", () => move(exc, to, input.value.trim() || null, confirm));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") confirm.click();
      if (e.key === "Escape") { e.stopPropagation(); buttons(foot, exc); }
    });
    foot.append(
      el("div", { class: "grow" }, input),
      el("button", { class: "btn", onClick: () => buttons(foot, exc) }, "Cancel"),
      confirm);
    input.focus();
  }

  async function move(exc, to, reason = null, btn = null) {
    if (btn) btn.dataset.busy = "true";
    try {
      const updated = await api.patchException(exc.id, { status: to, reason, actor: "you" });
      render(updated);
      toast(`Marked ${label(to)}`, "ok");
      onChange && onChange();
    } catch (err) {
      toast(err.message, "err");
      if (btn) delete btn.dataset.busy;
    }
  }

  function noteBox(exc) {
    const ta = el("textarea", {
      class: "textarea", rows: "2",
      placeholder: "What you checked, who you called, what they said…",
      "aria-label": "Note",
    });
    const btn = el("button", { class: "btn btn-sm" }, icon("note"), "Add note");
    btn.addEventListener("click", async () => {
      const text = ta.value.trim();
      if (!text) { ta.focus(); return; }
      btn.dataset.busy = "true";
      try {
        render(await api.addNote(exc.id, text));
        toast("Note added", "ok");
        onChange && onChange();
      } catch (err) { toast(err.message, "err"); }
      finally { delete btn.dataset.busy; }
    });
    return el("div", { class: "stack gap-2" }, ta, el("div", {}, btn));
  }

  function assignBox(exc) {
    const input = el("input", {
      class: "input", value: exc.assignee || "", placeholder: "Name or email",
      "aria-label": "Assign to",
    });
    const btn = el("button", { class: "btn btn-sm" }, icon("user"), "Assign");
    btn.addEventListener("click", async () => {
      btn.dataset.busy = "true";
      try {
        render(await api.patchException(exc.id, {
          assignee: input.value.trim() || null, actor: "you",
        }));
        toast(input.value.trim() ? `Assigned to ${input.value.trim()}` : "Unassigned", "ok");
        onChange && onChange();
      } catch (err) { toast(err.message, "err"); }
      finally { delete btn.dataset.busy; }
    });
    return el("div", { class: "row gap-2" }, el("div", { class: "grow" }, input), btn);
  }
}

function section(title, ...content) {
  return el("div", { style: { marginBottom: "16px" } },
    el("h4", { class: "metric-k", style: { marginBottom: "6px" }, text: title }),
    ...content);
}

function timeline(history) {
  if (!history.length) {
    return el("p", { class: "cell-sub", text: "Nothing has happened to this item yet." });
  }
  const tone = { created: "", status: "acc", note: "", assign: "",
                 auto_resolved: "ok", reopened: "warn" };
  return el("div", { class: "timeline" }, ...history.map((h) => {
    let what;
    if (h.kind === "status") {
      what = el("span", {}, "Moved ", el("b", { text: label(h.from_status) }),
                 " → ", el("b", { text: label(h.to_status) }),
                 h.body ? ` · ${h.body}` : "");
    } else if (h.kind === "auto_resolved") {
      what = el("span", {}, el("b", { text: "Auto-resolved" }),
                 " — a later run explained this entry");
    } else if (h.kind === "reopened") {
      what = el("span", {}, el("b", { text: "Reopened" }), " — it came back");
    } else {
      what = el("span", { text: h.body || titleCase(h.kind) });
    }
    return el("div", { class: "tl-item" },
      el("span", { class: `tl-dot ${tone[h.kind] || ""}` }),
      el("div", { class: "tl-body" },
        el("div", { class: "tl-what" }, what),
        el("div", { class: "tl-when", text: `${h.actor} · ${ago(h.at)}` })));
  }));
}
