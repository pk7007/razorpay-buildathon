/* The frontend's money and date formatting, tested.
 *
 * 2,445 lines of JavaScript shipped with no automated tests at all — every
 * check on it was a human looking at a screen. That is the wrong protection for
 * the layer that decides whether a number reads as ₹1.25 crore or ₹125.
 *
 * Node's built-in test runner, so this costs no dependency and no node_modules:
 *
 *     node --test web/tests/
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ago, date, dateShort, label, minorUnits, money, moneyShort, ms, num, pct,
  sentence, symbolFor, titleCase,
} from "../js/format.js";

describe("money", () => {
  it("formats paise as rupees with Indian digit grouping", () => {
    assert.equal(money(0), "₹0.00");
    assert.equal(money(100), "₹1.00");
    assert.equal(money(123456), "₹1,234.56");
    // the lakh/crore grouping, which is the whole reason not to use toLocaleString('en-US')
    assert.equal(money(1_00_00_000_00), "₹1,00,00,000.00");
    assert.equal(money(12_10_00_000_0), "₹1,21,00,000.00");
  });

  it("keeps the sign on a debit", () => {
    assert.equal(money(-11800), "-₹118.00");
  });

  it("never loses a paisa", () => {
    assert.equal(money(1), "₹0.01");
    assert.equal(money(99), "₹0.99");
    assert.equal(money(101), "₹1.01");
  });

  it("uses the right minor units per currency", () => {
    assert.equal(minorUnits("INR"), 100);
    assert.equal(minorUnits("USD"), 100);
    assert.equal(minorUnits("JPY"), 1, "yen has no minor unit");
    assert.equal(minorUnits("KWD"), 1000, "the dinar has three");
    assert.equal(minorUnits(undefined), 100, "defaults to INR");
  });

  it("formats a zero-decimal currency without decimals", () => {
    assert.equal(money(5000, "JPY"), "¥5,000");
  });

  it("formats a three-decimal currency with three", () => {
    // 1234 fils is 1.234 dinar. Formatting to two places drops a fils, which
    // is how a display bug turns into a reconciliation people do not trust.
    assert.match(money(1234, "KWD"), /1\.234/);
    assert.match(money(1000, "BHD"), /1\.000/);
  });

  it("has a symbol for the currencies the engine models", () => {
    assert.equal(symbolFor("INR"), "₹");
    assert.equal(symbolFor("USD"), "$");
    assert.equal(symbolFor("EUR"), "€");
    assert.equal(symbolFor("GBP"), "£");
    assert.match(symbolFor("ZZZ"), /^ZZZ/, "an unknown code falls back to the code");
  });
});

describe("moneyShort", () => {
  it("abbreviates in lakh and crore, not million", () => {
    assert.equal(moneyShort(1_25_00_000_00, "INR"), "₹1.25Cr");
    assert.equal(moneyShort(1_82_000_00, "INR"), "₹1.82L");
    assert.equal(moneyShort(9_053_07, "INR"), "₹9,053");
  });

  it("leaves small amounts alone, at full precision", () => {
    assert.equal(moneyShort(0, "INR"), "₹0.00");
    assert.equal(moneyShort(100, "INR"), "₹1.00");
  });

  it("does not abbreviate a non-INR currency into lakhs", () => {
    const out = moneyShort(1_00_00_000_00, "USD");
    assert.ok(!out.includes("Cr") && !out.includes("L"),
      `lakh/crore is an INR convention; got ${out}`);
  });
});

describe("pct and num", () => {
  it("renders a rate as a percentage", () => {
    assert.equal(pct(1), "100.0%");
    assert.equal(pct(0.983), "98.3%");
    assert.equal(pct(0), "0.0%");
    assert.equal(pct(0.9928, 2), "99.28%");
    assert.equal(pct(1, 0), "100%");
  });

  it("groups counts", () => {
    assert.equal(num(0), "0");
    assert.equal(num(1234), "1,234");
    assert.equal(num(58908), "58,908");
  });
});

describe("dates", () => {
  it("renders an ISO date readably", () => {
    assert.equal(date("2026-07-01"), "01 Jul 2026");
    assert.equal(dateShort("2026-07-01"), "01 Jul");
  });

  it("survives a missing or unparseable date instead of printing Invalid Date", () => {
    for (const bad of [null, undefined, "", "not-a-date"]) {
      const out = date(bad);
      assert.ok(!/Invalid/.test(out), `date(${JSON.stringify(bad)}) = ${out}`);
    }
  });

  it("describes how long ago something happened", () => {
    const now = Date.now();
    assert.match(ago(new Date(now - 30_000).toISOString()), /just now|s ago/);
    assert.match(ago(new Date(now - 5 * 60_000).toISOString()), /5m ago/);
    assert.match(ago(new Date(now - 3 * 3_600_000).toISOString()), /3h ago/);
  });
});

describe("ms", () => {
  it("renders engine timings", () => {
    assert.equal(ms(0), "0 ms");
    assert.equal(ms(21), "21 ms");
    assert.match(ms(4140), /^4\.1\d? ?s$/);
  });
});

describe("label casing", () => {
  it("turns a snake_case category into words", () => {
    assert.equal(label("missing_in_bank"), "missing in bank");
    assert.equal(label("payout_overdue"), "payout overdue");
    assert.equal(label(null), "");
  });

  it("sentence-cases rather than Title-Cases a state", () => {
    assert.equal(sentence("missing_in_bank"), "Missing in bank",
      "Title Case makes a state read like a product name");
    assert.equal(sentence(""), "");
  });

  it("title-cases where a title is wanted", () => {
    assert.equal(titleCase("written_off"), "Written Off");
  });
});
