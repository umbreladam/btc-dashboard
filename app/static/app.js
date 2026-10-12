const $ = s => document.querySelector(s);
const gbp = (v, d = 0) => (v < 0 ? "-" : "") + "£" + Math.abs(v || 0).toLocaleString("en-GB", {minimumFractionDigits: d, maximumFractionDigits: d});
const usd = v => "$" + (v || 0).toLocaleString("en-US", {maximumFractionDigits: 0});
const btc = v => (v || 0).toFixed(8);
const api = async (url, opt = {}) => {
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
};
const post = (url, body) => api(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
const ukTime = ts => new Date(ts).toLocaleString("en-GB", {timeZone: "Europe/London", dateStyle: "medium", timeStyle: "short"});
Chart.defaults.color = "#8a94a3"; Chart.defaults.borderColor = "#252d38"; Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

let charts = {}, series = [], range = 0;

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
  $("#ticker").innerHTML = `BTC <b>${gbp(s.price.gbp)}</b> · ${usd(s.price.usd)} <span class="sub">(${s.price.source || "offline"})</span>`;
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
  $("#c-basis").textContent = gbp(s.cost_basis_gbp); $("#c-avg").textContent = "Avg buy price " + gbp(s.avg_price_gbp) + " / BTC";
  const cls = s.pnl_gbp >= 0 ? "up" : "down";
  $("#c-pnl").textContent = (s.pnl_gbp >= 0 ? "+" : "") + gbp(s.pnl_gbp); $("#c-pnl").className = "val " + cls;
  $("#c-pnlpct").innerHTML = `<span class="${cls}">${s.pnl_pct >= 0 ? "▲" : "▼"} ${s.pnl_pct.toFixed(2)}%</span>`;
  const t = s.cgt;
  if (t.error) { $("#c-after").textContent = "—"; $("#c-cgt").textContent = t.error; }
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
  series = await api("api/series"); drawCharts();
  const buys = await api("api/tx?kind=buy");
  $("#recent").innerHTML = txHead() + buys.slice(0, 10).map(txRow).join("");
}

function drawCharts() {
  const d = range ? series.slice(-range) : series;
  const labels = d.map(x => x.day);
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
    options: opts(v => (+v).toFixed(4) + " ₿")});
}
document.querySelectorAll(".seg button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".seg button").forEach(x => x.classList.remove("on")); b.classList.add("on"); range = +b.dataset.r; drawCharts();
});

// ---------- transactions ----------
const txHead = () => `<tr><th>Date (UK)</th><th>Type</th><th>Source</th><th>Wallet</th><th class="r">BTC</th><th class="r">GBP</th><th class="r">Price £/BTC</th><th>Note</th><th></th></tr>`;
const txRow = t => `<tr><td>${ukTime(t.ts)}</td><td><span class="tag ${t.kind}">${t.kind}</span>${t.estimate ? '<span class="tag est">estimate</span>' : ""}${t.example ? '<span class="tag ex">example</span>' : ""}</td>
  <td>${t.source}</td><td>${t.wallet}${t.to_wallet ? " → " + t.to_wallet : ""}</td><td class="r">${btc(t.btc)}</td>
  <td class="r">${t.kind === "transfer" ? "" : gbp(t.gbp, 2)}</td><td class="r">${t.kind === "transfer" || !t.btc ? "" : gbp(t.gbp / t.btc)}</td>
  <td class="sub">${(t.note || "").replace(/</g, "&lt;")}</td><td>${t.id ? `<button class="del" data-id="${t.id}" title="Delete">✕</button>` : ""}</td></tr>`;

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
  try { await post("api/tx", f); e.target.reset(); $("#addform").classList.add("hidden"); loadTx(); } catch (err) { alert(err.message); }
};

// ---------- import ----------
let pending = null;
$("#f-import").onsubmit = async e => {
  e.preventDefault();
  try {
    const p = await api("api/import/preview", {method: "POST", body: new FormData(e.target)}); pending = p;
    const s = p.stats;
    $("#preview").innerHTML = `<h4>Preview — detected <b>${p.source}</b></h4>
      <p>${s.rows} rows: ${s.buys} buys, ${s.sells} sells, ${s.transfers} transfers · <b>${s.duplicates}</b> duplicates will be skipped</p>
      ${p.errors.length ? `<div class="err">${p.errors.length} issues:<br>${p.errors.slice(0, 30).join("<br>")}</div>` : '<div class="ok">No parse errors</div>'}
      <div class="tablewrap"><table>${txHead()}${p.rows.map(r => txRow(r).replace("<tr>", `<tr class="${r.duplicate ? "dup" : ""} ${r.warning ? "warn" : ""}">`)).join("")}</table></div>
      <div class="btns" style="margin-top:12px"><button class="primary" id="btn-commit">Import ${s.rows - s.duplicates} new rows</button><button id="btn-cancel">Cancel</button></div>`;
    $("#btn-commit").onclick = async () => { const r = await post("api/import/commit", {rows: pending.rows}); $("#preview").innerHTML = `<p class="ok">Imported ${r.inserted} transactions.</p>`; };
    $("#btn-cancel").onclick = () => $("#preview").innerHTML = "";
  } catch (err) { $("#preview").innerHTML = `<p class="err">${err.message}</p>`; }
};

// ---------- settings ----------
async function loadSettings() {
  const s = await api("api/settings"); const f = $("#f-settings");
  for (const el of f.elements) if (el.name && el.name in s) el.value = s[el.name] ?? "";
  const lg = await api("api/log");
  $("#log").innerHTML = lg.map(l => `${ukTime(l.ts)} [${l.job}] ${l.msg}`).join("<br>") || "No jobs run yet.";
}
$("#f-settings").onsubmit = async e => {
  e.preventDefault(); const d = Object.fromEntries(new FormData(e.target));
  for (const k of ["other_income", "personal_allowance", "basic_band", "basic_rate", "higher_rate", "annual_exempt", "exempt_used", "sell_fee_pct", "strike_daily_gbp", "gap_limit", "electrum_port"]) d[k] = +d[k];
  d.electrum_ssl = d.electrum_ssl === "true";
  try { await post("api/settings", d); $("#save-msg").innerHTML = '<span class="ok">Saved</span>'; loadSettings(); } catch (err) { $("#save-msg").innerHTML = `<span class="err">${err.message}</span>`; }
};
const act = (id, fn) => $(id).onclick = async () => { try { await fn(); } catch (e) { alert(e.message); } loadSettings(); };
act("#btn-strike-test", async () => { const r = await post("api/strike/test"); alert(`Strike OK: balance ${btc(r.balance)} BTC, rate ${gbp(r.rate_gbp)}`); });
act("#btn-strike-run", async () => { const r = await post("api/strike/run"); alert(r.message || r.error); });
act("#btn-scan", async () => { $("#scan-out").textContent = "Scanning via Electrum…"; const r = await post("api/cold/scan");
  $("#scan-out").innerHTML = `<span class="ok">Balance ${btc(r.balance)} BTC</span> (${btc(r.unconfirmed)} unconfirmed) across ${r.used_addresses.length} used addresses`; });
act("#btn-addr", async () => { const r = await api("api/xpub/addresses?n=5"); $("#scan-out").innerHTML = "Verify these match your wallet:<br>" + r.map(a => `${a.path} ${a.address}`).join("<br>"); });
act("#btn-prices", async () => { const r = await post("api/prices/refresh"); alert(`Cached ${r.days} daily prices`); });
act("#btn-ex-load", async () => { const r = await post("api/example/load"); alert(`Inserted ${r.inserted} example rows`); });
act("#btn-ex-del", async () => { if (!confirm("Delete all EXAMPLE transactions?")) return; const r = await api("api/example", {method: "DELETE"}); alert(`Deleted ${r.deleted}`); });

route();
