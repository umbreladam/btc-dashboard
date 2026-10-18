const $ = s => document.querySelector(s);
// Privacy mode: every absolute money/BTC figure goes through these formatters, which mask it when enabled.
// Percentages and the market BTC price (gbpRaw/usdRaw) are not masked. privacy-init.js sets html.priv before render.
const MASK = "****";
let PRIV = document.documentElement.classList.contains("priv");
const gbpRaw = (v, d = 0) => (v < 0 ? "-" : "") + "£" + Math.abs(v || 0).toLocaleString("en-GB", {minimumFractionDigits: d, maximumFractionDigits: d});
const usdRaw = v => "$" + (v || 0).toLocaleString("en-US", {maximumFractionDigits: 0});
const gbp = (v, d = 0) => PRIV ? "£" + MASK : gbpRaw(v, d);
const usd = v => PRIV ? "$" + MASK : usdRaw(v);
const btc = v => PRIV ? MASK : (v || 0).toFixed(8);
const btc4 = v => PRIV ? MASK + " ₿" : (+v).toFixed(4) + " ₿";
const signed = v => PRIV ? "£" + MASK : (v >= 0 ? "+" : "") + gbpRaw(v);
// Free text (notes, descriptions, messages): in privacy mode every number becomes **** (with any currency symbol /
// unit kept as a word), except dates, times, percentages and "line N" references. Digits inside identifiers
// (hex ids like 5a570b51...) are left alone. Display only: stored data is never changed.
const NUM_RE = /(?<![A-Za-z0-9_])(?:(\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?|\d{1,2}\/\d{1,2}\/\d{2,4}|\d{1,2}:\d{2}(?::\d{2})?(?![\d]|[.,]\d)|\d+(?:\.\d+)?\s?%|(?<=\bline )\d+)|([£$€]\s?)?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?)(?=([A-Za-z]*))/g;
const UNIT_RE = /^(?:k|m|bn|sats?|[A-Z]{2,6}|btc|gbp|usd|eur)?$/;
// BOLT11 Lightning invoices carry the amount in their prefix (lnbc2500u1... = 2500 µBTC): mask those digits too.
const LN_RE = /\b(ln(?:bc|tb|bcrt|sb))(\d+)(?=[munp]?1|\.\.\.|…)/gi;
const maskText = t => { t = String(t ?? ""); return PRIV ? t.replace(LN_RE, "$1" + MASK).replace(NUM_RE, (m, keep, cur, after) =>
  keep || !UNIT_RE.test(after) ? m : (cur ? cur.trim() : "") + MASK) : t; };
const mesc = t => esc(maskText(t));   // mask, then HTML-escape
// Value change indicator: "+1.52% (+£1,650)". Colour follows the sign; privacy masks the £ amount, keeps % + colour.
const chgFmt = r => {
  if (!r || !r.available || r.change_gbp == null) return {text: "—", cls: "flat"};
  const v = Math.round(r.change_gbp), pct = r.pct;
  const cls = v > 0 ? "up" : v < 0 ? "down" : "flat";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  const p = pct == null ? "" : (pct > 0 && v ? "+" : "") + (Math.abs(pct) < 0.005 ? "0.00" : pct.toFixed(2)) + "% ";
  const amt = PRIV ? "£" + MASK : sign + gbpRaw(Math.abs(v));
  return {text: p ? `${p}(${amt})` : amt, cls};
};
function setPrivacy(on) {
  PRIV = !!on; document.documentElement.classList.toggle("priv", PRIV);
  try { localStorage.setItem("satoshi-privacy", PRIV ? "1" : "0"); } catch (e) {}
  const b = document.getElementById("btn-priv");
  if (b) { b.setAttribute("aria-pressed", PRIV); b.title = b.ariaLabel = PRIV ? "Show balances" : "Hide balances (privacy mode)"; }
}
// Every write carries JSON + the X-Satoshi-Request header (server-side CSRF check).
const api = async (url, opt = {}) => {
  if (opt.method && opt.method !== "GET") opt.headers = {"Content-Type": "application/json", "X-Satoshi-Request": "1", ...(opt.headers || {})};
  if (opt.method && opt.method !== "GET" && opt.body === undefined) opt.body = "{}";
  const r = await fetch(url, {credentials: "same-origin", ...opt});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
};
const post = (url, body, method = "POST") => api(url, {method, body: JSON.stringify(body || {})});
const ukTime = ts => new Date(ts).toLocaleString("en-GB", {timeZone: "Europe/London", dateStyle: "medium", timeStyle: "short"});
Chart.defaults.color = "#8a94a3"; Chart.defaults.borderColor = "#252d38"; Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

let charts = {}, series = [], range = "0", intraOff = null, intra = {};

// ---------- routing ----------
function route() {
  const p = (location.hash || "#dash").slice(1);
  document.querySelectorAll("main > section").forEach(s => s.classList.toggle("hidden", s.id !== "page-" + p));
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + p));
  ({dash: loadDash, tx: loadTx, settings: loadSettings}[p] || (() => {}))();
}
window.addEventListener("hashchange", route);

// ---------- dashboard ----------
async function loadDash() {
  const s = await api("api/summary");
  $("#ticker").innerHTML = `BTC <b>${gbpRaw(s.price.gbp)}</b> · ${usdRaw(s.price.usd)} <span class="sub">(${s.price.source || "offline"})</span>`;
  $("#example-banner").classList.toggle("hidden", !s.counts.example); $("#ex-count").textContent = s.counts.example;
  const h = s.holdings;
  $("#c-btc").textContent = btc(h.total) + " ₿";
  $("#c-split").innerHTML = [
    ["#f7931a", "Strike balance", h.strike, h.strike_live ? "live API" : "ledger"],
    ["#3861fb", "Cold storage", h.cold, h.cold_live ? "Electrs" : "ledger"],
    ["#8a94a3", "Other / exchanges", h.other, "ledger"]].map(([c, n, v, src]) =>
    `<div><span><span class="dot" style="background:${c}"></span>${n} <span class="tag">${src}</span></span><b>${btc(v)}</b></div>`).join("") +
    (Math.abs(h.mismatch_btc) > 1e-6 ? `<div class="sub">Live balances differ from transaction ledger by ${btc(h.mismatch_btc)} BTC — import missing records.</div>` : "");
  $("#c-gbp").textContent = gbp(s.value.gbp); $("#c-usd").textContent = usd(s.value.usd);
  loadChange();
  $("#c-basis").textContent = gbp(s.cost_basis_gbp); $("#c-avg").textContent = "Avg buy price " + gbp(s.avg_price_gbp) + " / BTC";
  const cls = s.pnl_gbp >= 0 ? "up" : "down";
  $("#c-pnl").textContent = signed(s.pnl_gbp); $("#c-pnl").className = "val " + cls;
  $("#c-pnlpct").innerHTML = `<span class="${cls}">${s.pnl_pct >= 0 ? "▲" : "▼"} ${s.pnl_pct.toFixed(2)}%</span>`;
  const t = s.cgt;
  if (t.error) { $("#c-after").textContent = "—"; $("#c-cgt").textContent = maskText(t.error); }
  else {
    $("#c-after").textContent = gbp(t.value_after_tax); $("#c-cgt").textContent = `CGT ${gbp(t.tax)} · tax year ${t.tax_year}`;
    $("#cgt-table").innerHTML = [
      ["Proceeds (after " + "selling fee)", gbp(t.proceeds)], ["Realised gains already this tax year", gbp(s.realised_this_tax_year)],
      ["Net gain for " + t.tax_year + " incl. this sale", gbp(t.net_gain)], ["Annual exempt amount remaining", gbp(t.exempt_available)],
      ["Taxable gain", gbp(t.taxable_gain)], ["Basic-rate band remaining", gbp(t.basic_band_left)],
      ["Taxed at basic CGT rate", gbp(t.at_basic)], ["Taxed at higher CGT rate", gbp(t.at_higher)],
      ["CGT due", gbp(t.tax)]].map(([a, b], i, arr) => `<tr class="${i === arr.length - 1 ? "total" : ""}"><td>${a}</td><td>${b}</td></tr>`).join("") +
      `<tr class="total"><td>Value after CGT</td><td>${gbp(t.value_after_tax)}</td></tr>`;
  }
  drawWallets(s);
  series = await api("api/series");
  if (intraOff === null) { try { intraOff = !!(await api("api/settings")).disable_external_prices; } catch (e) { intraOff = false; } }
  document.querySelectorAll(".seg[data-chart] button.intra").forEach(b => b.classList.toggle("hidden", intraOff));
  await drawCharts();
  const buys = await api("api/tx?kind=buy");
  $("#recent").innerHTML = txHead() + buys.slice(0, 10).map(txRow).join("");
}

// ---------- value change over a period ----------
const CHG_PERIODS = ["1h", "1d", "1w", "1m", "1y", "4y", "all"];
let chgPeriod = (() => { try { const v = localStorage.getItem("satoshi-change-period"); return CHG_PERIODS.includes(v) ? v : "1d"; } catch (e) { return "1d"; } })();
let chgSeq = 0;
async function loadChange() {
  document.querySelectorAll("#chg-seg button").forEach(b => { b.classList.toggle("on", b.dataset.p === chgPeriod); b.setAttribute("aria-pressed", b.dataset.p === chgPeriod); });
  const seq = ++chgSeq, el = $("#c-chg");
  let r = null;
  try { r = await api("api/change?period=" + chgPeriod); } catch (e) { r = null; }
  if (seq !== chgSeq) return;   // a newer period was chosen meanwhile
  const f = chgFmt(r);
  el.textContent = f.text; el.className = "chg " + f.cls;
  el.title = r && r.available ? (chgPeriod === "all" ? "Unrealised PnL vs cost basis" : "Market change, excluding money paid in/out during the period") : (r && r.reason) || "Unavailable";
}
document.querySelectorAll("#chg-seg button").forEach(b => b.onclick = () => {
  chgPeriod = b.dataset.p; try { localStorage.setItem("satoshi-change-period", chgPeriod); } catch (e) {}
  loadChange();
});

const WCOL = {Multisig: ["#3861fb", "#7c5cff", "#2fb5ff", "#5a7dff"], "Single-sig": ["#16c784", "#0fa3a3", "#6be0a8"], Exchange: ["#f7931a", "#ffb547", "#e2711d"], Other: ["#8a94a3"], Cold: ["#5f6b7a"]};
function drawWallets(s) {
  const rows = s.wallets || [], used = {};
  const col = r => { const p = WCOL[r.type] || WCOL.Other; used[r.type] = (used[r.type] || 0) + 1; return p[(used[r.type] - 1) % p.length]; };
  rows.forEach(r => r.color = col(r));
  const ago = ts => ts ? ukTime(ts) : null;
  $("#wallets-panel").innerHTML = rows.map(r => `<div class="wrow">
    <div class="wn"><span class="dot" style="background:${r.color}"></span>${esc(r.name)} <span class="badge ${esc(r.type)}">${esc(r.type)}${r.policy && r.policy !== "single-sig" && r.type === "Multisig" ? " " + esc(r.policy) : ""}</span></div>
    <div class="wb">${btc(r.btc)} ₿</div>
    <div class="wd">${mesc(r.description)}<div class="wm">${r.pending ? "Not scanned yet" : r.scanned_at ? (r.source === "electrs" ? "Scanned " : "Updated ") + ago(r.scanned_at) : "From transaction ledger"}</div></div>
    <div class="wv">${gbp(r.gbp)} · ${usd(r.usd)}<br><b>${r.pct.toFixed(1)}%</b></div>
    <div class="pctbar"><i style="width:${Math.max(0, Math.min(100, r.pct))}%;background:${r.color}"></i></div></div>`).join("") +
    `<div class="wrow total"><div class="wn">Total (${rows.length} wallets)</div><div class="wb">${btc(s.holdings.total)} ₿</div><div class="wd"></div><div class="wv">${gbp(s.value.gbp)} · ${usd(s.value.usd)}<br><b>100%</b></div></div>`;
  const pos = rows.filter(r => r.btc > 0);
  charts.d?.destroy();
  charts.d = new Chart($("#ch-donut"), {type: "doughnut", data: {labels: pos.map(r => r.name), datasets: [{data: pos.map(r => r.btc), backgroundColor: pos.map(r => r.color), borderColor: "#151a21", borderWidth: 2}]},
    options: {responsive: true, maintainAspectRatio: false, cutout: "62%", plugins: {legend: {position: "bottom", labels: {boxWidth: 12}},
      tooltip: {callbacks: {label: c => `${c.label}: ${btc(c.parsed)} ₿ (${(c.parsed / s.holdings.total * 100).toFixed(1)}%)`}}}}});
}

// Ranges: "1d"/"1w" use intraday points (exact-timestamp holdings x intraday price); others filter the daily series by date.
async function rangeData() {
  if (range === "1d" || range === "1w") {
    const days = range === "1d" ? 1 : 7;
    if (!intra[days] || Date.now() - intra[days].at > 120e3) intra[days] = {at: Date.now(), r: await api("api/series/intraday?days=" + days).catch(() => ({available: false}))};
    const r = intra[days].r;
    if (r.available && r.points.length) return r.points.map(p => ({...p, label: days === 1 ? p.day.slice(11) : new Date(p.t * 1000).toLocaleString("en-GB", {timeZone: "Europe/London", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"})}));
    document.querySelectorAll(".seg[data-chart] button.intra").forEach(b => b.classList.add("hidden")); intraOff = true;
    range = "0"; document.querySelectorAll(".seg[data-chart] button").forEach(x => x.classList.toggle("on", x.dataset.r === "0"));
  }
  const n = +range;
  if (!n) return series.map(x => ({...x, label: x.day}));
  const cut = new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);
  return series.filter(x => x.day >= cut).map(x => ({...x, label: x.day}));
}
async function drawCharts() {
  const d = await rangeData();
  const labels = d.map(x => x.label);
  const opts = (fmt) => ({responsive: true, maintainAspectRatio: false, interaction: {mode: "index", intersect: false},
    plugins: {legend: {labels: {boxWidth: 12}}, tooltip: {callbacks: {label: c => c.dataset.label + ": " + fmt(c.parsed.y)}}},
    scales: {x: {type: "category", ticks: {maxTicksLimit: 8}, grid: {display: false}}, y: {ticks: {callback: v => fmt(v)}}},
    elements: {point: {radius: 0}}});
  charts.v?.destroy(); charts.b?.destroy();
  charts.v = new Chart($("#ch-value"), {type: "line", data: {labels, datasets: [
    {label: "Value (GBP)", data: d.map(x => x.value), borderColor: "#f7931a", backgroundColor: "rgba(247,147,26,.12)", fill: true, borderWidth: 2, tension: .2},
    {label: "Cost basis (GBP)", data: d.map(x => x.cost), borderColor: "#3861fb", borderWidth: 2, stepped: true}]}, options: opts(v => gbp(v))});
  charts.b = new Chart($("#ch-btc"), {type: "line", data: {labels, datasets: [
    {label: "BTC held", data: d.map(x => x.btc), borderColor: "#16c784", backgroundColor: "rgba(22,199,132,.12)", fill: true, borderWidth: 2, stepped: true}]},
    options: opts(btc4)});
}
document.querySelectorAll(".seg[data-chart] button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".seg[data-chart] button").forEach(x => x.classList.remove("on")); b.classList.add("on"); range = b.dataset.r; drawCharts();
});

// ---------- transactions ----------
const txHead = () => `<tr><th>Date (UK)</th><th>Type</th><th>Source</th><th>Wallet</th><th class="r">BTC</th><th class="r">GBP</th><th class="r">Price £/BTC</th><th>Note</th><th></th></tr>`;
const NATIVE_TYPES = ["buy", "income", "acquisition", "sell", "spend", "gift", "disposal", "fee", "transfer", "lost"];
const txType = t => { const m = /\[([a-z]+)\]\s*$/.exec(t.note || ""); return m && NATIVE_TYPES.includes(m[1]) ? m[1] : (t.kind === "transfer" && t.to_wallet === "lost" ? "lost" : t.kind); };
const txBtc = t => txType(t) === "lost" && !t.btc ? t.fee_btc : t.btc;
const txRow = t => `<tr><td>${ukTime(t.ts)}</td><td><span class="tag ${t.kind} t-${txType(t)}" title="${t.kind}">${txType(t)}</span>${t.estimate ? '<span class="tag est">estimate</span>' : ""}${t.example ? '<span class="tag ex">example</span>' : ""}</td>
  <td>${t.source}</td><td>${t.wallet}${t.to_wallet ? " → " + t.to_wallet : ""}</td><td class="r">${btc(txBtc(t))}</td>
  <td class="r">${t.kind === "transfer" ? "" : gbp(t.gbp, 2)}</td><td class="r">${t.kind === "transfer" || !t.btc ? "" : gbp(t.gbp / t.btc)}</td>
  <td class="sub">${mesc(t.note)}</td><td>${t.id ? `<button class="del" data-id="${t.id}" title="Delete">✕</button>` : ""}</td></tr>`;

async function loadTx() {
  const q = new URLSearchParams({kind: $("#f-kind").value, source: $("#f-source").value, wallet: $("#f-wallet").value,
    start: $("#f-start").value, end: $("#f-end").value, q: $("#f-q").value});
  const rows = await api("api/tx?" + q);
  const b = rows.filter(r => r.kind === "buy"), sb = b.reduce((a, r) => a + r.btc, 0), sg = b.reduce((a, r) => a + r.gbp, 0);
  $("#tx-stats").textContent = `${rows.length} transactions · ${b.length} buys · ${btc(sb)} BTC bought for ${gbp(sg, 2)}` + (sb ? ` (avg ${gbp(sg / sb)}/BTC)` : "");
  $("#txtable").innerHTML = txHead() + rows.map(txRow).join("");
}
["#f-kind", "#f-source", "#f-wallet", "#f-start", "#f-end", "#f-q"].forEach(s => $(s).addEventListener("input", loadTx));
document.addEventListener("click", async e => {
  if (e.target.classList.contains("del") && confirm("Delete this transaction?")) {
    await api("api/tx/" + e.target.dataset.id, {method: "DELETE"}); route();
  }
});
$("#btn-add").onclick = () => $("#addform").classList.toggle("hidden");
$("#f-add").onsubmit = async e => {
  e.preventDefault(); const f = Object.fromEntries(new FormData(e.target));
  ["btc", "gbp", "fee_gbp", "fee_btc"].forEach(k => f[k] = +f[k] || 0); if (!f.to_wallet) f.to_wallet = null;
  try { await post("api/tx", f); e.target.reset(); $("#addform").classList.add("hidden"); loadTx(); } catch (err) { alert(maskText(err.message)); }
};

// ---------- import ----------
let pending = null;
$("#f-import").onsubmit = async e => {
  e.preventDefault();
  try {
    const fd = new FormData(e.target), file = fd.get("file");
    if (!file || !file.size) throw new Error("Choose a CSV file");
    if (file.size > 20e6) throw new Error("File too large (max 20 MB)");
    const isPdf = /\.pdf$/i.test(file.name) || file.type === "application/pdf";
    const b64 = buf => { let s = "", b = new Uint8Array(buf); for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000)); return btoa(s); };
    $("#preview").innerHTML = '<p class="sub">Reading file…</p>';
    const body = {source: fd.get("source"), filename: file.name};
    if (isPdf) body.pdf_b64 = b64(await file.arrayBuffer()); else body.text = await file.text();
    pending = await post("api/import/preview", body);
    renderPreview();
  } catch (err) { $("#preview").innerHTML = `<p class="err">${mesc(err.message)}</p>`; }
};
function renderPreview() {
    const p = pending, s = p.stats;
    $("#preview").innerHTML = `<h4>Preview — detected <b>${p.source}</b></h4>
      <p>${s.rows} rows: ${s.buys} buys, ${s.sells} sells, ${s.transfers} transfers · <b>${s.duplicates}</b> duplicates will be skipped</p>
      ${p.errors.length ? `<div class="err">${p.errors.length} issues:<br>${p.errors.slice(0, 30).map(mesc).join("<br>")}</div>` : '<div class="ok">No parse errors</div>'}
      <div class="tablewrap"><table>${txHead()}${p.rows.map(r => txRow(r).replace("<tr>", `<tr class="${r.duplicate ? "dup" : ""} ${r.warning ? "warn" : ""}">`)).join("")}</table></div>
      <div class="btns" style="margin-top:12px"><button class="primary" id="btn-commit">Import ${s.rows - s.duplicates} new rows</button><button id="btn-cancel">Cancel</button></div>`;
    $("#btn-commit").onclick = async () => { const r = await post("api/import/commit", {rows: pending.rows}); pending = null; $("#preview").innerHTML = `<p class="ok">Imported ${r.inserted} transactions.</p>`; };
    $("#btn-cancel").onclick = () => { $("#preview").innerHTML = ""; pending = null; };
}

// ---------- settings ----------
async function loadSettings() {
  const s = await api("api/settings"); const f = $("#f-settings");
  for (const el of f.elements) if (el.name && el.name in s) { if (el.type === "checkbox") el.checked = !!s[el.name]; else el.value = s[el.name] ?? ""; }
  $("#strike-key-state").textContent = s.strike_key_set ? (s.strike_key_from_env ? "(set via environment)" : "(saved, encrypted - leave blank to keep)") : "(none saved)";
  f.elements.strike_api_key.value = ""; f.elements.strike_api_key_clear.checked = false;
  $("#override-phrase").textContent = s.override_phrase;
  const ef = s.electrum_effective, um = s.electrs_umbrel;
  $("#electrum-now").innerHTML = ef ? `Electrum server: <b>${esc(ef.host)}:${ef.port}</b> ${ef.overridden ? '<span class="err">custom server (sees your addresses)</span>' : '<span class="ok">Umbrel Electrs (locked)</span>'}${ef.ssl ? " · TLS " + (ef.pinned ? "pinned" : "CA-verified") : " · TCP"}`
    : '<span class="err">Electrs not detected. Install the Electrs app on your Umbrel.</span>';
  if (ef && ef.overridden) $("#electrum-adv").open = true;
  if (!f.elements.electrum_port_override.value || f.elements.electrum_port_override.value === "0") f.elements.electrum_port_override.value = 50001;
  $("#dca-frequency").value = s.strike_job_enabled ? (s.strike_frequency || "daily") : "off";
  dcaState = s; applyFreq();
  loadWallets();
  await loadLog();
}
async function loadLog() {
  const lg = await api("api/log");
  $("#log").innerHTML = lg.map(l => `${ukTime(l.ts)} [${esc(l.job)}] ${mesc(l.msg)}`).join("<br>") || "No jobs run yet.";
}
// ---------- Strike DCA: show only the schedule fields for the chosen frequency ----------
let dcaState = {};
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const ordinal = n => n + ([, "st", "nd", "rd"][(n % 100 >> 3 ^ 1) && n % 10] || "th");
function dcaStatus(freq, s) {
  if (freq === "off") return "Scheduled Strike check is off. Choose Daily, Weekly or Monthly to turn it on.";
  if (!s.strike_key_set) return "Add your Strike API key above (and Save) to turn on the scheduled check.";
  if (!s.strike_job_enabled || s.strike_frequency !== freq) return "Press Save settings to turn on this schedule.";
  return s.strike_next_run ? `Next balance check: ${ukTime(s.strike_next_run)}` : "";
}
function dcaExplain(freq, f) {
  if (freq === "off") return "The app won't check Strike automatically. Add your buys with Import (CSV) or Transactions → Add, or press Run job now.";
  const t = f.strike_job_time.value || "15:25", m = +f.strike_check_delay_min.value || 0;
  const amt = ({GBP: "£", USD: "$", EUR: "€"}[f.strike_dca_currency.value] || "") + (+f.strike_daily_gbp.value || 0);
  const when = freq === "daily" ? `Every day at ${t}` : freq === "weekly" ? `Every ${WEEKDAYS[+f.strike_weekday.value] || "Monday"} at ${t}`
    : `On the ${ordinal(+f.strike_monthday.value || 1)} of each month at ${t}`;
  return `${when} (UK time, matching your Strike recurring buy), the app reads your Strike balance ${m} minutes later. ` +
    `An increase is recorded as a ${amt} buy. If the balance fell, it records an estimated ${amt} buy at Strike's rate plus an estimated transfer of the rest to cold storage.` +
    (freq === "monthly" ? " Days 29–31 run on the last day in shorter months." : "");
}
function applyFreq() {
  const f = $("#f-settings"), freq = $("#dca-frequency").value;
  document.querySelectorAll("[data-freq]").forEach(el => el.classList.toggle("hidden", !el.dataset.freq.split(" ").includes(freq)));
  if (freq !== "off") f.elements.strike_frequency.value = freq;
  f.elements.strike_job_enabled.value = freq === "off" ? "false" : "true";
  $("#dca-explain").textContent = maskText(dcaExplain(freq, f));
  $("#strike-next").textContent = dcaStatus(freq, dcaState);
}
$("#dca-frequency").addEventListener("change", applyFreq);
["strike_job_time", "strike_check_delay_min", "strike_weekday", "strike_monthday", "strike_daily_gbp", "strike_dca_currency"].forEach(n => $("#f-settings").elements[n].addEventListener("input", applyFreq));

$("#f-settings").onsubmit = async e => {
  e.preventDefault(); const d = Object.fromEntries(new FormData(e.target));
  for (const k of ["other_income", "personal_allowance", "basic_band", "basic_rate", "higher_rate", "annual_exempt", "exempt_used", "sell_fee_pct", "strike_daily_gbp", "strike_weekday", "strike_monthday", "strike_check_delay_min", "gap_limit", "electrum_port_override"]) d[k] = +d[k];
  d.electrum_ssl = d.electrum_ssl === "true"; d.electrum_override = d.electrum_override === "true";
  d.disable_external_prices = d.disable_external_prices === "true"; d.strike_api_key_clear = d.strike_api_key_clear === "true";
  d.strike_job_enabled = d.strike_job_enabled === "true";
  try { await post("api/settings", d); $("#save-msg").innerHTML = '<span class="ok">Saved</span>'; loadSettings(); } catch (err) { $("#save-msg").innerHTML = `<span class="err">${mesc(err.message)}</span>`; }
};
const act = (id, fn) => $(id).onclick = async () => { try { await fn(); } catch (e) { alert(maskText(e.message)); } loadSettings(); };
act("#btn-strike-test", async () => { const r = await post("api/strike/test"); alert(`Strike OK: balance ${btc(r.balance)} BTC, rate ${gbpRaw(r.rate_gbp)}`); });
act("#btn-strike-run", async () => { const r = await post("api/strike/run"); alert(maskText(r.message || r.error)); });
act("#btn-scan", async () => { $("#scan-out").textContent = "Scanning via Electrum…"; const r = await post("api/cold/scan");
  $("#scan-out").innerHTML = `<span class="ok">Balance ${btc(r.balance)} BTC</span> (${btc(r.unconfirmed)} unconfirmed) across ${r.used_addresses.length} used addresses` + (r.wallets || []).map(w => `<br>${esc(w.name)}: ${btc(w.balance)} BTC (${w.used} used addresses)`).join(""); });
const esc = t => String(t ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const addrList = r => r.map(a => `${a.chain} ${esc(a.path)} <code>${esc(a.address)}</code>`).join("<br>");
const ADDR_TYPES = [["auto", "Auto-detect (recommended)"], ["p2wpkh", "Native SegWit (bc1q…)"], ["p2sh-p2wpkh", "Nested SegWit (3…)"], ["p2pkh", "Legacy (1…)"], ["p2tr", "Taproot (bc1p…)"]];
const ADDR_LABEL = Object.fromEntries(ADDR_TYPES);
const descSumm = s => [s.policy, s.script_type, s.network, s.fingerprints && s.fingerprints.length ? "fingerprints " + s.fingerprints.join(", ") : "", s.checksum_verified ? "checksum OK" : "", s.change_chain_inferred ? "change chain /1/* inferred" : ""].filter(Boolean).map(esc).join(" · ");
async function loadWallets() {
  const ws = await api("api/cold/wallets"); const el = $("#cold-wallets");
  el.innerHTML = ws.length ? ws.map(w => `<div class="cw" data-id="${esc(w.id)}"><b>${esc(w.name)}</b> <span class="badge ${esc(w.type)}">${esc(w.type)}</span>${w.type_overridden ? ' <span class="sub">(set manually; detected ' + esc(w.detected_type) + ')</span>' : ""}
    <div class="sub">${mesc(w.description)}</div><div class="sub">${descSumm(w)} · ${w.input === "descriptor" ? "descriptor" : "xpub"}${w.balance != null ? " · " + btc(w.balance) + " BTC" : ""}${w.scanned_at ? " · scanned " + ukTime(w.scanned_at) : ""}</div>
    ${w.first_receive ? `<div class="sub">First receive address: <code>${esc(w.first_receive)}</code> <span class="sub">(check it matches your wallet app)</span></div>` : ""}
    ${w.detected_scripts && w.detected_scripts.length > 1 ? `<div class="sub">History found in ${w.detected_scripts.length} address types (${w.detected_scripts.map(x => esc(ADDR_LABEL[x] || x)).join(", ")}): balances are added together.</div>` : ""}
    <div class="btns" style="margin-top:6px"><button type="button" data-edit>Edit</button><button type="button" data-addr>Show first addresses</button><button type="button" class="danger" data-del>Remove</button></div>
    <div class="edit"><input data-f="name" maxlength="60" value="${esc(w.name)}" aria-label="Name"><input data-f="description" class="pmask" maxlength="200" value="${esc(w.description || "")}" placeholder="Description" aria-label="Description">
      <select data-f="type" aria-label="Type label"><option value="auto">Auto (${esc(w.detected_type)})</option><option ${w.type_overridden && w.type === "Multisig" ? "selected" : ""}>Multisig</option><option ${w.type_overridden && w.type === "Single-sig" ? "selected" : ""}>Single-sig</option></select>
      ${w.input === "xpub" ? `<label class="sub">Address type <select data-f="address_type" aria-label="Address type">${ADDR_TYPES.map(([v, l]) => `<option value="${v}" ${(w.address_type || "auto") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>` : ""}
      <button type="button" class="primary" data-save>Save</button></div></div>`).join("") : "No cold-storage wallets yet.";
  el.querySelectorAll(".cw").forEach(row => {
    const id = row.dataset.id;
    row.querySelector("[data-edit]").onclick = () => row.classList.toggle("editing");
    row.querySelector("[data-save]").onclick = async () => { try { const g = k => row.querySelector(`[data-f=${k}]`).value;
      const body = {name: g("name"), description: g("description"), type: g("type")};
      if (row.querySelector("[data-f=address_type]")) body.address_type = g("address_type");
      await post(`api/cold/wallets/${id}`, body, "PUT"); loadWallets(); } catch (e) { alert(maskText(e.message)); } };
    row.querySelector("[data-addr]").onclick = async () => { try { const r = await api(`api/cold/wallets/${id}/addresses?n=5`); $("#scan-out").innerHTML = "Verify these match your wallet:<br>" + addrList(r); } catch (e) { alert(maskText(e.message)); } };
    row.querySelector("[data-del]").onclick = async () => { if (!confirm("Remove this cold-storage wallet from the app?")) return; await api(`api/cold/wallets/${id}`, {method: "DELETE"}); loadWallets(); };
  });
}
const cwMethod = () => { const d = $("#cw-method").value === "descriptor"; $("#cw-script-wrap").classList.toggle("hidden", d);
  $("#cw-spec-lbl").textContent = d ? "Output descriptor or wallet export (Sparrow, Specter, Nunchuk/BSMS, Caravan, Electrum, Coldcard)" : "Extended public key (xpub / ypub / zpub)";
  $("#cw-spec").placeholder = d ? "wsh(sortedmulti(2,[a1b2c3d4/48h/0h/0h/2h]xpub.../<0;1>/*,...))#checksum  or paste the export file" : "zpub6r..."; };
$("#cw-method").onchange = cwMethod; cwMethod();
const cwBody = () => ({name: $("#cw-label").value, description: $("#cw-desc").value, type: $("#cw-type").value, spec: $("#cw-spec").value,
  script: $("#cw-method").value === "descriptor" ? "auto" : $("#cw-script").value, n: 5});
act("#btn-cw-preview", async () => { const r = await post("api/cold/preview", cwBody());
  $("#scan-out").innerHTML = `<b>${esc(r.name || "New wallet")}</b> <span class="badge ${esc(r.type)}">${esc(r.type)}</span> ${descSumm(r)}<br>Verify these match your wallet (not saved yet):<br>` + addrList(r.addresses); });
act("#btn-cw-add", async () => { const r = await post("api/cold/wallets", cwBody()); ["#cw-spec", "#cw-label", "#cw-desc"].forEach(x => $(x).value = ""); $("#cw-type").value = "auto";
  $("#scan-out").innerHTML = `<span class="ok">Added</span> <b>${esc(r.wallet.name)}</b> (${esc(r.wallet.type)}, ${descSumm(r.wallet)}). Use "Show first addresses" to double-check, then Scan now.`; loadWallets(); });
act("#btn-prices", async () => { const r = await post("api/prices/refresh"); alert(`Cached ${r.days} daily prices`); });
act("#btn-ex-load", async () => { const r = await post("api/example/load"); alert(`Inserted ${r.inserted} example rows`); });
act("#btn-ex-del", async () => { if (!confirm("Delete all EXAMPLE transactions?")) return; const r = await api("api/example", {method: "DELETE"}); alert(`Deleted ${r.deleted}`); });

route();

// ---------- privacy toggle ----------
setPrivacy(PRIV);
$("#btn-priv").onclick = () => {
  setPrivacy(!PRIV);
  const p = (location.hash || "#dash").slice(1);   // re-render figures without reloading (and losing) settings form edits
  if (p === "settings") { loadWallets(); applyFreq(); loadLog(); } else if (p === "import" && pending) renderPreview(); else if (p === "dash" || p === "tx") route();
};
