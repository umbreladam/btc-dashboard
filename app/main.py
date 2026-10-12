import asyncio, os, json
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from . import descriptor, schedule, db, importers, portfolio, prices, strike, electrum, example

LDN = ZoneInfo("Europe/London")
STATIC = os.path.join(os.path.dirname(__file__), "static")
app = FastAPI(title="Satoshi BTC Dashboard")
SECRET_KEYS = {"strike_api_key"}


@app.on_event("startup")
async def startup():
    db.init()
    with db.conn() as c:
        if os.environ.get("LOAD_EXAMPLE_DATA", "1") == "1" and not db.get_settings(c).get("example_seeded"):
            prices.refresh_history(c, date(2021, 1, 1))
            example.generate(c, {k: v[0] for k, v in prices.history(c).items()})
            db.set_settings(c, {"example_seeded": True})
    asyncio.create_task(scheduler())


# ---------------- scheduler ----------------
async def scheduler():
    last_cold = 0
    while True:
        try:
            now = datetime.now(LDN)
            with db.conn() as c:
                s = db.get_settings(c)
            if strike_key(s) and schedule.should_run(now, s):
                await asyncio.to_thread(run_strike_job)
            if s.get("prices_day") != now.date().isoformat():
                with db.conn() as c:
                    await asyncio.to_thread(prices.refresh_history, c, date(2021, 1, 1))
                    db.set_settings(c, {"prices_day": now.date().isoformat()})
            if cold_wallets(s) and s["electrum_host"] and asyncio.get_event_loop().time() - last_cold > 1800:
                last_cold = asyncio.get_event_loop().time()
                try:
                    await cold_scan()
                except Exception as e:
                    with db.conn() as c:
                        db.log(c, "cold", f"scan failed: {e}")
        except Exception as e:
            with db.conn() as c:
                db.log(c, "scheduler", repr(e))
        await asyncio.sleep(30)


def strike_key(s) -> str:
    """Key comes from the user's Settings entry; env STRIKE_API_KEY is an optional runtime override."""
    return s.get("strike_api_key") or os.environ.get("STRIKE_API_KEY", "")


def run_strike_job():
    with db.conn() as c:
        s = db.get_settings(c)
        key = strike_key(s)
        if not key:
            return {"error": "No Strike API key saved"}
        today = datetime.now(LDN).date().isoformat()
        try:
            bal = strike.balance_btc(key)
            rate = strike.rate_btc_gbp(key)
            cur = str(s.get("strike_dca_currency") or "GBP").upper()
            amount = float(s["strike_daily_gbp"])
            # Amount in GBP for the ledger: convert via Strike's BTC/<cur> rate if needed
            amount_gbp = amount if cur == "GBP" else round(amount / strike.rate_btc(key, cur) * rate, 2)
        except Exception as e:
            db.log(c, "strike", f"API error: {e}")
            db.set_settings(c, {"strike_last_run": today})
            return {"error": str(e)}
        txs = strike.daily_record(s["strike_last_balance"], bal, rate, amount_gbp)
        now = datetime.now(timezone.utc).isoformat()
        for t in txs:
            t.update(ts=now, source="strike-api", dedupe=f"strike-api|{today}|{t['kind']}")
            db.insert_tx(c, t)
        db.set_settings(c, {"strike_last_balance": bal, "strike_last_run": today,
                            "strike_balance_cache": {"btc": bal, "at": now}})
        msg = f"balance {bal:.8f} BTC, rate £{rate:,.0f}, recorded {len(txs)} tx" + \
              (" (first run: baseline only)" if s["strike_last_balance"] is None else "")
        db.log(c, "strike", msg)
        return {"balance": bal, "rate": rate, "recorded": txs, "message": msg}


def cold_wallets(s):
    """Configured cold-storage wallets. A 1.0.0 single xpub shows up as one wallet until edited."""
    if s.get("cold_wallets"):
        return s["cold_wallets"]
    if s.get("xpub"):
        return [{"id": "legacy", "label": "Cold storage", "spec": s["xpub"], "script": s.get("xpub_script", "auto")}]
    return []


def wallet_info(spec, script="auto"):
    """Validate an xpub / descriptor / wallet export. Returns (normalised spec, kind, summary)."""
    if electrum.is_descriptor(spec):
        d = descriptor.from_input(spec)
        d.derive(0, 0, 1)
        return d.canonical(), "descriptor", d.summary()
    k, stype, net = electrum.parse_xpub(spec, script)
    return spec.strip(), "xpub", {"type": stype, "policy": "single-sig", "keys": 1}


def wallet_addresses(spec, script, n):
    out = []
    for chain in (0, 1):
        out += [{"path": p, "address": a, "chain": "receive" if chain == 0 else "change"}
                for p, a, _ in electrum.derive(spec, chain, 0, n, script)]
    return out


async def cold_scan():
    with db.conn() as c:
        s = db.get_settings(c)
    wallets = cold_wallets(s)
    if not wallets:
        raise HTTPException(400, "No cold-storage wallet configured")
    if not s["electrum_host"]:
        raise HTTPException(400, "No Electrum host configured")
    gap = max(int(s["gap_limit"]), 20)
    res = {"confirmed": 0.0, "unconfirmed": 0.0, "balance": 0.0, "used_addresses": [], "wallets": []}
    for w in wallets:
        r = await electrum.scan(w["spec"], s["electrum_host"], s["electrum_port"], bool(s["electrum_ssl"]),
                                gap, w.get("script", "auto"))
        for k in ("confirmed", "unconfirmed", "balance"):
            res[k] += r[k]
        res["used_addresses"] += [dict(a, wallet=w["label"]) for a in r["used_addresses"]]
        res["wallets"].append({"id": w["id"], "label": w["label"], "balance": r["balance"],
                               "unconfirmed": r["unconfirmed"], "used": len(r["used_addresses"])})
    for k in ("confirmed", "unconfirmed", "balance"):
        res[k] = round(res[k], 8)
    with db.conn() as c:
        db.set_settings(c, {"cold_balance_cache": {"btc": res["balance"], "at": datetime.now(timezone.utc).isoformat(),
                                                   "addresses": len(res["used_addresses"]), "wallets": res["wallets"]}})
        db.log(c, "cold", f"balance {res['balance']:.8f} BTC across {len(res['used_addresses'])} used addresses in "
                          f"{len(wallets)} wallet(s): " + ", ".join(f"{w['label']} {w['balance']:.8f}" for w in res["wallets"]))
    return res


# ---------------- API ----------------
@app.get("/api/summary")
def api_summary():
    with db.conn() as c:
        return portfolio.summary(c)


@app.get("/api/series")
def api_series():
    with db.conn() as c:
        return portfolio.series(c)


@app.get("/api/tx")
def api_tx(kind: str = "", source: str = "", wallet: str = "", start: str = "", end: str = "", q: str = ""):
    sql, args = "SELECT * FROM tx WHERE 1=1", []
    for col, v in (("kind", kind), ("source", source), ("wallet", wallet)):
        if v:
            sql += f" AND {col}=?"; args.append(v)
    if start:
        sql += " AND ts>=?"; args.append(start)
    if end:
        sql += " AND ts<=?"; args.append(end + "T23:59:59")
    if q:
        sql += " AND (note LIKE ? OR ext_id LIKE ?)"; args += [f"%{q}%"] * 2
    with db.conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY ts DESC, id DESC", args)]


class TxIn(BaseModel):
    ts: str
    kind: str
    wallet: str = "other"
    to_wallet: str | None = None
    btc: float
    gbp: float = 0
    fee_gbp: float = 0
    fee_btc: float = 0
    note: str | None = None
    estimate: int = 0


@app.post("/api/tx")
def api_add(t: TxIn):
    if t.kind not in ("buy", "sell", "transfer") or t.btc <= 0:
        raise HTTPException(400, "kind must be buy/sell/transfer and btc > 0")
    d = t.model_dump()
    ts = datetime.fromisoformat(d["ts"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=LDN)
    d.update(ts=ts.astimezone(timezone.utc).isoformat(), source="manual",
             dedupe=f"manual|{datetime.now().timestamp()}")
    with db.conn() as c:
        db.insert_tx(c, d)
    return {"ok": True}


@app.delete("/api/tx/{tid}")
def api_del(tid: int):
    with db.conn() as c:
        c.execute("DELETE FROM tx WHERE id=?", (tid,))
    return {"ok": True}


@app.post("/api/import/preview")
async def api_preview(file: UploadFile = File(...), source: str = Form("auto")):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    src = importers.detect(text) if source == "auto" else source
    if src not in importers.PARSERS:
        raise HTTPException(400, "Could not detect export type - choose Coinbase, Kraken or Strike")
    rows, errs = importers.PARSERS[src](text)
    with db.conn() as c:
        for r in rows:
            r["dedupe"] = db.dedupe_key(r)
            r["duplicate"] = c.execute("SELECT 1 FROM tx WHERE dedupe=?", (r["dedupe"],)).fetchone() is not None
            r["warning"] = "WARNING" in (r.get("note") or "")
    return {"source": src, "rows": rows, "errors": errs,
            "stats": {"rows": len(rows), "duplicates": sum(r["duplicate"] for r in rows),
                      "buys": sum(r["kind"] == "buy" for r in rows), "sells": sum(r["kind"] == "sell" for r in rows),
                      "transfers": sum(r["kind"] == "transfer" for r in rows)}}


@app.post("/api/import/commit")
def api_commit(payload: dict):
    n = 0
    with db.conn() as c:
        for r in payload.get("rows", []):
            if r.get("duplicate"):
                continue
            n += db.insert_tx(c, r)
    return {"inserted": n}


@app.get("/api/settings")
def api_get_settings():
    with db.conn() as c:
        s = db.get_settings(c)
    s.pop("cold_wallets", None)  # served (abbreviated) by /api/cold/wallets
    for k in SECRET_KEYS:
        s[k] = ("••••" + s[k][-4:]) if s[k] else ""
    s["strike_key_set"] = bool(s["strike_api_key"]) or bool(os.environ.get("STRIKE_API_KEY"))
    try:
        s["strike_next_run"] = schedule.next_check(datetime.now(LDN), s["strike_frequency"], s["strike_job_time"],
                                                   s["strike_weekday"], s["strike_monthday"],
                                                   s["strike_check_delay_min"], s.get("strike_last_run")).isoformat() \
            if s["strike_job_enabled"] else None
    except Exception:
        s["strike_next_run"] = None
    return s


@app.post("/api/settings")
def api_set_settings(d: dict):
    allowed = set(db.DEFAULTS) - {"cold_wallets", "cold_balance_cache", "strike_balance_cache", "strike_last_balance"}
    upd = {k: v for k, v in d.items() if k in allowed}
    if str(upd.get("strike_api_key", "")).startswith("••••"):
        upd.pop("strike_api_key")
    if "strike_job_time" in upd:
        try:
            schedule.parse_time(upd["strike_job_time"])
        except Exception:
            raise HTTPException(400, "Run time must be HH:MM (UK time)")
    if "strike_frequency" in upd and upd["strike_frequency"] not in schedule.FREQUENCIES:
        raise HTTPException(400, "Frequency must be daily, weekly or monthly")
    if "gap_limit" in upd:
        upd["gap_limit"] = max(int(upd["gap_limit"] or 25), 20)
    if upd.get("xpub"):
        try:
            electrum.parse_xpub(upd["xpub"], upd.get("xpub_script", "auto"))
        except Exception as e:
            raise HTTPException(400, f"Invalid extended public key: {e}")
    with db.conn() as c:
        old = db.get_settings(c)
        if "xpub" in upd and upd["xpub"] != old["xpub"]:
            db.set_settings(c, {"cold_balance_cache": None})
        if upd.get("strike_api_key") == "" and old["strike_api_key"]:
            db.set_settings(c, {"strike_balance_cache": None, "strike_last_balance": None})
        db.set_settings(c, upd)
    return {"ok": True}


@app.get("/api/xpub/addresses")
def api_addresses(n: int = 5):
    with db.conn() as c:
        s = db.get_settings(c)
    w = cold_wallets(s)
    if not w:
        return []
    return [{"path": p, "address": a} for p, a, _ in electrum.derive(w[0]["spec"], 0, 0, n, w[0].get("script", "auto"))]


class WalletIn(BaseModel):
    label: str = ""
    spec: str
    script: str = "auto"
    n: int = 5


def _bad(e):
    return HTTPException(400, f"Invalid xpub / descriptor: {e}")


@app.get("/api/cold/wallets")
def api_wallets():
    with db.conn() as c:
        s = db.get_settings(c)
    out = []
    for w in cold_wallets(s):
        try:
            _, kind, summ = wallet_info(w["spec"], w.get("script", "auto"))
        except Exception as e:
            kind, summ = "invalid", {"error": str(e)}
        sp = w["spec"]
        out.append({"id": w["id"], "label": w["label"], "kind": kind, "summary": summ,
                    "preview": sp[:24] + "…" + sp[-12:] if len(sp) > 40 else sp})
    return out


@app.post("/api/cold/preview")
def api_wallet_preview(w: WalletIn):
    """First receive + change addresses for an UNSAVED xpub/descriptor, to compare with your wallet."""
    try:
        spec, kind, summ = wallet_info(w.spec, w.script)
        return {"kind": kind, "summary": summ, "descriptor": spec if kind == "descriptor" else None,
                "addresses": wallet_addresses(spec, w.script, max(1, min(w.n, 50)))}
    except Exception as e:
        raise _bad(e)


@app.post("/api/cold/wallets")
def api_wallet_add(w: WalletIn):
    try:
        spec, kind, summ = wallet_info(w.spec, w.script)
    except Exception as e:
        raise _bad(e)
    import secrets
    with db.conn() as c:
        s = db.get_settings(c)
        ws = [dict(x) for x in cold_wallets(s)]
        label = (w.label or "").strip()[:60] or f"Wallet {len(ws) + 1}"
        ws.append({"id": secrets.token_hex(4), "label": label, "spec": spec, "script": w.script})
        db.set_settings(c, {"cold_wallets": ws, "xpub": "", "cold_balance_cache": None})
    return {"ok": True, "kind": kind, "summary": summ}


@app.delete("/api/cold/wallets/{wid}")
def api_wallet_del(wid: str):
    with db.conn() as c:
        s = db.get_settings(c)
        ws = [x for x in cold_wallets(s) if x["id"] != wid]
        db.set_settings(c, {"cold_wallets": ws, "xpub": "", "cold_balance_cache": None})
    return {"ok": True}


@app.get("/api/cold/wallets/{wid}/addresses")
def api_wallet_addresses(wid: str, n: int = 5):
    with db.conn() as c:
        s = db.get_settings(c)
    for w in cold_wallets(s):
        if w["id"] == wid:
            return wallet_addresses(w["spec"], w.get("script", "auto"), max(1, min(n, 50)))
    raise HTTPException(404, "Unknown wallet")


@app.post("/api/cold/scan")
async def api_cold():
    try:
        return await cold_scan()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Electrum error: {e}")


@app.post("/api/strike/run")
def api_strike_run():
    return run_strike_job()


@app.post("/api/strike/test")
def api_strike_test():
    with db.conn() as c:
        key = strike_key(db.get_settings(c))
    if not key:
        raise HTTPException(400, "No Strike API key saved")
    try:
        return {"balance": strike.balance_btc(key), "rate_gbp": strike.rate_btc_gbp(key)}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/prices/refresh")
def api_prices():
    with db.conn() as c:
        return {"days": prices.refresh_history(c, date(2021, 1, 1))}


@app.post("/api/example/load")
def api_example_load():
    with db.conn() as c:
        return {"inserted": example.generate(c, {k: v[0] for k, v in prices.history(c).items()})}


@app.delete("/api/example")
def api_example_delete():
    with db.conn() as c:
        n = c.execute("DELETE FROM tx WHERE example=1").rowcount
        db.set_settings(c, {"example_seeded": True})
    return {"deleted": n}


@app.get("/api/log")
def api_log():
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM joblog ORDER BY id DESC LIMIT 50")]


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
