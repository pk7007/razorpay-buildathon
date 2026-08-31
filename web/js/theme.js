/* Runs before first paint so a dark-mode user never gets a white flash.

   It is a separate file rather than an inline <script> because the app's CSP
   is `script-src 'self'` — no inline execution, no exceptions carved out for
   convenience. */
try {
  var t = localStorage.getItem("afc-theme");
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
} catch (e) { /* private browsing: fall back to the system preference */ }
