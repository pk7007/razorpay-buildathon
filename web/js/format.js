/* Formatting. Money is the whole product, so it gets first-class treatment:
   correct minor units per currency, Indian digit grouping for INR, and a
   compact form for headline figures that never loses the exact value from the
   title attribute. */

const MINOR = { INR: 100, USD: 100, EUR: 100, GBP: 100, AED: 100, SGD: 100,
                AUD: 100, CAD: 100, JPY: 1, KWD: 1000, BHD: 1000 };

const LOCALE = { INR: "en-IN" };

export const minorUnits = (ccy) => MINOR[(ccy || "INR").toUpperCase()] ?? 100;

/** Full precision, e.g. ₹1,23,456.78 */
export function money(minor, ccy = "INR", { decimals = true } = {}) {
  const c = (ccy || "INR").toUpperCase();
  const div = minorUnits(c);
  const value = (minor || 0) / div;
  const frac = div > 1 && decimals ? 2 : 0;
  try {
    return new Intl.NumberFormat(LOCALE[c] || "en-US", {
      style: "currency", currency: c,
      minimumFractionDigits: frac, maximumFractionDigits: frac,
    }).format(value);
  } catch {
    return `${c} ${value.toFixed(frac)}`;
  }
}

/** Headline form: ₹2.4Cr, ₹1.82L, ₹9,053. Exact value belongs in a title. */
export function moneyShort(minor, ccy = "INR") {
  const c = (ccy || "INR").toUpperCase();
  const v = (minor || 0) / minorUnits(c);
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  const sym = symbolFor(c);
  // Indian numbering is what a finance user in this market reads fastest.
  if (c === "INR") {
    if (a >= 1e7) return `${sign}${sym}${trim(a / 1e7)}Cr`;
    if (a >= 1e5) return `${sign}${sym}${trim(a / 1e5)}L`;
  } else {
    if (a >= 1e9) return `${sign}${sym}${trim(a / 1e9)}B`;
    if (a >= 1e6) return `${sign}${sym}${trim(a / 1e6)}M`;
    if (a >= 1e3) return `${sign}${sym}${trim(a / 1e3)}K`;
  }
  return money(minor, c, { decimals: a < 1000 });
}

const trim = (n) => (n >= 100 ? Math.round(n) : Number(n.toFixed(n >= 10 ? 1 : 2))).toString();

export function symbolFor(ccy) {
  const map = { INR: "₹", USD: "$", EUR: "€", GBP: "£", JPY: "¥" };
  return map[(ccy || "INR").toUpperCase()] || `${ccy} `;
}

export const pct = (x, dp = 1) =>
  x == null || Number.isNaN(x) ? "—" : `${(x * 100).toFixed(dp)}%`;

export const num = (n) =>
  n == null ? "—" : new Intl.NumberFormat("en-IN").format(n);

export function date(iso) {
  if (!iso) return "—";
  const d = new Date(iso.length <= 10 ? `${iso}T00:00:00` : iso);
  if (Number.isNaN(+d)) return iso;
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

export function dateShort(iso) {
  if (!iso) return "—";
  const d = new Date(iso.length <= 10 ? `${iso}T00:00:00` : iso);
  if (Number.isNaN(+d)) return iso;
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
}

export function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(s)) return "";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  if (s < 2592000) return `${Math.round(s / 86400)}d ago`;
  return date(iso);
}

export function ms(v) {
  if (v == null) return "—";
  return v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(2)} s`;
}

/** snake_case -> readable words, used for statuses and categories. */
export const label = (s) => String(s || "").replace(/_/g, " ");

/* Sentence case, not Title Case: "Missing in bank" is a description of a
   state, and Title Case makes every state read like a product name. */
export const sentence = (s) => {
  const t = label(s);
  return t ? t[0].toUpperCase() + t.slice(1) : "";
};

export const titleCase = (s) =>
  label(s).replace(/\b\w/g, (m) => m.toUpperCase());
