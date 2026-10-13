import asyncio, os, json, logging
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from . import statements, descriptor, schedule, db, importers, portfolio, prices, strike, electrum, example, security, wallets

os.umask(0o077)  # every file this process creates (DB, WAL, key file) is owner-only
LDN = ZoneInfo("Europe/London")
STATIC = os.path.join(os.path.dirname(__file__), "static")
app = FastAPI(title="Satoshi BTC Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
log = logging.getLogger("satoshi")
TRUSTED_PEERS = security.trusted_peers()
OVERRIDE_PHRASE = "this reveals my addresses"
GENERIC_WALLET_ERR = "Could not read that xpub / descriptor / wallet export"


@app.middleware("http")
async def guard(request: Request, call_next):
    peer = request.client.host if request.client else None
    if peer not in TRUSTED_PEERS:
        return JSONResponse({"detail": "Forbidden: open this app through Umbrel"}, status_code=403)
    if not security.csrf_ok(request.method, request.headers, request.headers.get("host")):
        return JSONResponse({"detail": "Forbidden: missing CSRF header or JSON content type"}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


def init_storage():
    db.init()
    db.secure_files()
    with db.conn() as c:
        s = db.get_settings(c)
        upd = {}
        ws, changed = wallets.migrate(s)
        if changed:
            upd.update(cold_wallets=ws, xpub="")
        raw = s.get("strike_api_key") or ""
        if raw and not raw.startswith("enc:"):
            upd["strike_api_key"] = security.encrypt(db.DATA_DIR, raw)
        if upd:
            db.set_settings(c, upd)
            if changed:
                db.log(c, "migrate", f"wallets migrated to schema {wallets.SCHEMA} ({len(ws)} wallet(s))")


@app.on_event("startup")
async def startup():
    init_storage()
    if os.environ.get("BTC_DASH_NO_SCHEDULER") == "1":
        return
    with db.conn() as c:
        s = db.get_settings(c)
        if os.environ.get("LOAD_EXAMPLE_DATA", "1") == "1" and not s.get("example_seeded"):
            if not s.get("disable_external_prices"):
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
            if s.get("prices_day") != now.date().isoformat() and not s.get("disable_external_prices"):
                with db.conn() as c:
                    await asyncio.to_thread(prices.refresh_history, c, date(2021, 1, 1))
                    db.set_settings(c, {"prices_day": now.date().isoformat()})
            if cold_wallets(s) and electrum_target(s) and asyncio.get_event_loop().time() - last_cold > 1800:
                last_cold = asyncio.get_event_loop().time()
                try:
                    await cold_scan()
                except Exception as e:
                    with db.conn() as c:
                        db.log(c, "cold", f"scan failed ({safe_err(e)})")
        except Exception as e:
            with db.conn() as c:
                db.log(c, "scheduler", type(e).__name__)
        await asyncio.sleep(30)


def safe_err(e) -> str:
    """Error text that can't carry key material: exception type plus messages from our own network layer only."""
    if isinstance(e, HTTPException):
        return str(e.detail)
    if isinstance(e, (OSError, asyncio.TimeoutError, ConnectionError)):
        return f"{type(e).__name__}: connection failed" if not str(e) or "pinned" not in str(e) else str(e)
    return type(e).__name__


def strike_key(s) -> str:
    """Key comes from the user's Settings entry (encrypted at rest); env STRIKE_API_KEY is an optional override."""
    return security.decrypt(db.DATA_DIR, s.get("strike_api_key") or "") or os.environ.get("STRIKE_API_KEY", "")


def electrum_target(s):
    """(host, port, ssl, cert_fp). Locked to Umbrel's Electrs unless an explicit, confirmed override is set."""
    if s.get("electrum_override") and s.get("electrum_override_confirm") == OVERRIDE_PHRASE and s.get("electrum_host_override"):
        return (s["electrum_host_override"], int(s.get("electrum_port_override") or 50001),
                bool(s.get("electrum_ssl")), s.get("electrum_cert_fp") or "")
    host, port = db.umbrel_electrs()
    if not host:
        return None
    return host, port, bool(s.get("electrum_ssl")), s.get("electrum_cert_fp") or ""


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
            amount_gbp = amount if cur == "GBP" else round(amount / strike.rate_btc(key, cur) * rate, 2)
        except Exception as e:
            msg = f"HTTP {e.response.status_code}" if hasattr(e, "response") and e.response is not None else type(e).__name__
            db.log(c, "strike", f"API error: {msg}")
            db.set_settings(c, {"strike_last_run": today})
            return {"error": f"Strike API error: {msg}"}
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
    ws, _ = wallets.migrate(s)
    return ws


def wallet_addresses(spec, script, n):
    out = []
    for chain in (0, 1):
        out += [{"path": p, "address": a, "chain": "receive" if chain == 0 else "change"}
                for p, a, _ in electrum.derive(spec, chain, 0, n, script)]
    return out


async def cold_scan():
    with db.conn() as c:
        s = db.get_settings(c)
    ws = cold_wallets(s)
    if not ws:
        raise HTTPException(400, "No cold-storage wallet configured")
    tgt = electrum_target(s)
    if not tgt:
        raise HTTPException(400, "Electrs not found: install the Electrs app on Umbrel")
    gap = max(int(s["gap_limit"]), 20)
    now = datetime.now(timezone.utc).isoformat()
    res = {"confirmed": 0.0, "unconfirmed": 0.0, "balance": 0.0, "used_addresses": [], "wallets": []}
    for w in ws:
        r = await electrum.scan(w["spec"], tgt[0], tgt[1], tgt[2], gap, w.get("script", "auto"), cert_fp=tgt[3])
        for k in ("confirmed", "unconfirmed", "balance"):
            res[k] += r[k]
        res["used_addresses"] += [dict(a, wallet=w["name"]) for a in r["used_addresses"]]
        res["wallets"].append({"id": w["id"], "name": w["name"], "balance": round(r["balance"], 8),
                               "unconfirmed": r["unconfirmed"], "used": len(r["used_addresses"]), "at": now})
    for k in ("confirmed", "unconfirmed", "balance"):
        res[k] = round(res[k], 8)
    with db.conn() as c:
        db.set_settings(c, {"cold_balance_cache": {"btc": res["balance"], "at": now,
                                                   "addresses": len(res["used_addresses"]), "wallets": res["wallets"]}})
        db.log(c, "cold", f"balance {res['balance']:.8f} BTC across {len(res['used_addresses'])} used addresses in "
                          f"{len(ws)} wallet(s): " + ", ".join(f"{w['name']} {w['balance']:.8f}" for w in res["wallets"]))
    return res


# ---------------- API ----------------
@app.get("/api/summary")
def api_summary():
    with db.conn() as c:
        s = db.get_settings(c)
        return portfolio.summary(c, strike_key(s), [wallets.public_view(w) for w in cold_wallets(s)])


@app.get("/api/series")
def api_series():
    with db.conn() as c:
        return portfolio.series(c)


@app.get("/api/series/intraday")
def api_series_intraday(days: int = 1):
    """1D / 1W ranges. Needs an intraday price feed: unavailable when external price fetches are disabled."""
    days = 7 if days > 1 else 1
    with db.conn() as c:
        if db.get_settings(c).get("disable_external_prices"):
            return {"available": False, "reason": "External price fetches are disabled", "points": []}
    pts = prices.intraday(days)
    if not pts:
        return {"available": False, "reason": "Intraday prices unavailable", "points": []}
    with db.conn() as c:
        return {"available": True, "points": portfolio.intraday(c, pts)}


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


class ImportIn(BaseModel):
    text: str = ""
    pdf_b64: str = ""     # PDF statements are sent base64-encoded (still JSON, so the CSRF checks apply)
    source: str = "auto"
    filename: str = ""


@app.post("/api/import/preview")
def api_preview(p: ImportIn):
    """CSV is sent as JSON text (not multipart) so every write request carries the JSON + CSRF-header checks."""
    if len(p.text) > 20_000_000 or len(p.pdf_b64) > 28_000_000:
        raise HTTPException(413, "File too large")
    if p.pdf_b64:
        try:
            src, rows, errs = statements.parse_pdf_b64(p.pdf_b64, p.source)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception:
            raise HTTPException(400, "Could not read this PDF")
    else:
        text = p.text.lstrip("\ufeff")
        parsers = {**importers.PARSERS, **statements.CSV_PARSERS}
        src = (importers.detect(text) or statements.detect_csv(text)) if p.source == "auto" else p.source
        if src not in parsers:
            raise HTTPException(400, "Could not detect export type - choose the exchange and statement type")
        rows, errs = parsers[src](text)
    with db.conn() as c:
        occ = {}
        for r in rows:
            sig = (r["source"], r["ts"][:19], r["kind"], round(r["btc"], 8))
            if not r.get("ext_id"):
                occ[sig] = r["occurrence"] = occ.get(sig, 0) + 1
            r["dedupe"] = db.dedupe_key(r)
            # duplicate if this id was imported before, or the same trade already came in via the other format
            # (PDF vs CSV): the n-th identical id-less row is a duplicate only if the DB already has n such rows
            same = c.execute("SELECT count(*) FROM tx WHERE source=? AND substr(ts,1,19)=? AND kind=? AND abs(btc-?)<5e-9",
                             (r["source"], r["ts"][:19], r["kind"], r["btc"])).fetchone()[0]
            r["duplicate"] = (c.execute("SELECT 1 FROM tx WHERE dedupe=?", (r["dedupe"],)).fetchone() is not None
                              or same >= (r.get("occurrence") or 1))
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


HIDDEN_SETTINGS = {"cold_wallets", "xpub", "xpub_script", "strike_api_key", "cold_balance_cache", "strike_balance_cache",
                   "electrum_host", "electrum_port"}


@app.get("/api/settings")
def api_get_settings():
    with db.conn() as c:
        s = db.get_settings(c)
    out = {k: v for k, v in s.items() if k not in HIDDEN_SETTINGS}
    out["strike_api_key"] = ""  # write-only
    out["strike_key_set"] = bool(s.get("strike_api_key")) or bool(os.environ.get("STRIKE_API_KEY"))
    out["strike_key_from_env"] = bool(os.environ.get("STRIKE_API_KEY")) and not s.get("strike_api_key")
    host, port = db.umbrel_electrs()
    out["electrs_umbrel"] = {"host": host, "port": port}
    tgt = electrum_target(s)
    out["electrum_effective"] = {"host": tgt[0], "port": tgt[1], "ssl": tgt[2], "pinned": bool(tgt[3]),
                                 "overridden": tgt[0] != host or tgt[1] != port} if tgt else None
    out["override_phrase"] = OVERRIDE_PHRASE
    try:
        out["strike_next_run"] = schedule.next_check(datetime.now(LDN), s["strike_frequency"], s["strike_job_time"],
                                                     s["strike_weekday"], s["strike_monthday"],
                                                     s["strike_check_delay_min"], s.get("strike_last_run")).isoformat() \
            if s["strike_job_enabled"] else None
    except Exception:
        out["strike_next_run"] = None
    return out


SETTABLE = set(db.DEFAULTS) - {"cold_wallets", "cold_balance_cache", "strike_balance_cache", "strike_last_balance", "xpub",
                               "xpub_script"}


@app.post("/api/settings")
def api_set_settings(d: dict):
    upd = {k: v for k, v in d.items() if k in SETTABLE}
    if "strike_api_key" in upd:
        k = str(upd.pop("strike_api_key") or "").strip()
        if k and not k.startswith("••••"):
            upd["strike_api_key"] = security.encrypt(db.DATA_DIR, k)
    if d.get("strike_api_key_clear"):
        upd["strike_api_key"] = ""
    if "strike_job_time" in upd:
        try:
            schedule.parse_time(upd["strike_job_time"])
        except Exception:
            raise HTTPException(400, "Run time must be HH:MM (UK time)")
    if "strike_frequency" in upd and upd["strike_frequency"] not in schedule.FREQUENCIES:
        raise HTTPException(400, "Frequency must be daily, weekly or monthly")
    if "gap_limit" in upd:
        upd["gap_limit"] = max(int(upd["gap_limit"] or 25), 20)
    for b in ("electrum_ssl", "electrum_override", "disable_external_prices", "strike_job_enabled"):
        if b in upd:
            upd[b] = upd[b] is True or str(upd[b]).lower() == "true"
    try:
        if "electrum_cert_fp" in upd:
            upd["electrum_cert_fp"] = electrum.norm_fingerprint(upd["electrum_cert_fp"])
        if upd.get("electrum_override"):
            if str(upd.get("electrum_override_confirm", "")).strip().lower() != OVERRIDE_PHRASE:
                raise ValueError(f'To use a non-Umbrel Electrum server type "{OVERRIDE_PHRASE}" to confirm')
            upd["electrum_override_confirm"] = OVERRIDE_PHRASE
            upd["electrum_host_override"], upd["electrum_port_override"] = electrum.validate_host(
                upd.get("electrum_host_override"), upd.get("electrum_port_override") or 50001)
        elif "electrum_override" in upd:
            upd.update(electrum_override_confirm="", electrum_host_override="", electrum_port_override=50001)
        else:
            for k in ("electrum_override_confirm", "electrum_host_override", "electrum_port_override"):
                upd.pop(k, None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with db.conn() as c:
        old = db.get_settings(c)
        if upd.get("strike_api_key") == "" and old["strike_api_key"]:
            db.set_settings(c, {"strike_balance_cache": None, "strike_last_balance": None})
        if electrum_target(old) != electrum_target({**old, **upd}):
            db.set_settings(c, {"cold_balance_cache": None})
        db.set_settings(c, upd)
    return {"ok": True}


# ---------------- cold wallets (key material is write-only) ----------------
class WalletIn(BaseModel):
    name: str = ""
    label: str = ""  # 1.0.1 field name
    description: str = ""
    type: str | None = None  # None/"auto" = auto-detect, or "Multisig"/"Single-sig"
    spec: str
    script: str = "auto"
    n: int = 5


class WalletEdit(BaseModel):
    name: str | None = None
    description: str | None = None
    type: str | None = None  # "auto" resets to detected


def _bad():
    return HTTPException(400, GENERIC_WALLET_ERR + ". Check it is complete and contains only PUBLIC keys.")


@app.get("/api/cold/wallets")
def api_wallets():
    with db.conn() as c:
        s = db.get_settings(c)
    cache = {w["id"]: w for w in (s.get("cold_balance_cache") or {}).get("wallets", []) if "id" in w}
    return [dict(wallets.public_view(w), balance=cache.get(w["id"], {}).get("balance"),
                 scanned_at=cache.get(w["id"], {}).get("at") or (s.get("cold_balance_cache") or {}).get("at")
                 if w["id"] in cache else None) for w in cold_wallets(s)]


@app.post("/api/cold/preview")
def api_wallet_preview(w: WalletIn):
    """First receive + change addresses for an UNSAVED xpub/descriptor. Returns name + fingerprints + type only."""
    try:
        info = wallets.detect(w.spec, w.script)
        addrs = wallet_addresses(info["spec"], w.script, max(1, min(w.n, 50)))
    except Exception:
        raise _bad()
    return {"name": wallets.clean(w.name or w.label, 60), "type": wallets.check_type(w.type) if w.type in wallets.TYPES else info["detected_type"],
            "detected_type": info["detected_type"], "policy": info["policy"], "fingerprints": info["fingerprints"],
            "script_type": info["script_type"], "network": info["network"], "checksum_verified": info["checksum_verified"],
            "change_chain_inferred": info["change_chain_inferred"], "addresses": addrs}


@app.post("/api/cold/wallets")
def api_wallet_add(w: WalletIn):
    with db.conn() as c:
        s = db.get_settings(c)
        ws = cold_wallets(s)
        try:
            nw = wallets.make(w.name or w.label, w.description, w.spec, w.script, w.type, len(ws))
        except ValueError as e:
            raise _bad() if GENERIC_WALLET_ERR in str(e) else HTTPException(400, str(e))
        if any(x.get("spec") == nw["spec"] for x in ws):
            raise HTTPException(409, "That wallet is already added")
        nw["schema"] = wallets.SCHEMA
        ws.append(nw)
        db.set_settings(c, {"cold_wallets": ws, "xpub": "", "cold_balance_cache": None})
    return {"ok": True, "wallet": wallets.public_view(nw)}


@app.put("/api/cold/wallets/{wid}")
def api_wallet_edit(wid: str, e: WalletEdit):
    """Edit name / description / type label. Key material can't be changed or read: remove + re-add instead."""
    with db.conn() as c:
        s = db.get_settings(c)
        ws = cold_wallets(s)
        for w in ws:
            if w["id"] == wid:
                if e.name is not None:
                    w["name"] = wallets.clean(e.name, 60) or w["name"]
                if e.description is not None:
                    w["description"] = wallets.clean(e.description, 200)
                if e.type is not None:
                    try:
                        w["type_override"] = wallets.check_type(e.type)
                    except ValueError as x:
                        raise HTTPException(400, str(x))
                db.set_settings(c, {"cold_wallets": ws, "xpub": ""})
                return {"ok": True, "wallet": wallets.public_view(w)}
    raise HTTPException(404, "Unknown wallet")


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
            try:
                return wallet_addresses(w["spec"], w.get("script", "auto"), max(1, min(n, 50)))
            except Exception:
                raise _bad()
    raise HTTPException(404, "Unknown wallet")


@app.post("/api/cold/scan")
async def api_cold():
    try:
        return await cold_scan()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Electrum error ({safe_err(e)})")


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
        code = getattr(getattr(e, "response", None), "status_code", None)
        raise HTTPException(502, f"Strike API error ({'HTTP %s' % code if code else type(e).__name__})")


@app.post("/api/prices/refresh")
def api_prices():
    with db.conn() as c:
        if db.get_settings(c).get("disable_external_prices"):
            raise HTTPException(400, "External price fetches are disabled in Settings")
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
