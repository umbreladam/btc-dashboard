import os, sqlite3, json, hashlib
from contextlib import contextmanager

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "btc-dashboard.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tx (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,              -- ISO8601 UTC
  kind TEXT NOT NULL,            -- buy | sell | transfer | fee
  source TEXT NOT NULL,          -- strike | coinbase | kraken | manual | strike-api | example
  wallet TEXT NOT NULL,          -- wallet holding the BTC: strike | coinbase | kraken | cold | other
  to_wallet TEXT,                -- for transfers
  btc REAL NOT NULL,             -- always positive
  gbp REAL NOT NULL DEFAULT 0,   -- cost incl fees (buy) / net proceeds (sell)
  fee_gbp REAL NOT NULL DEFAULT 0,
  fee_btc REAL NOT NULL DEFAULT 0,  -- network fee on transfer (treated as small disposal-free loss of BTC)
  estimate INTEGER NOT NULL DEFAULT 0,
  example INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  ext_id TEXT,
  dedupe TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS prices (day TEXT PRIMARY KEY, gbp REAL, usd REAL, synthetic INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS joblog (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job TEXT, msg TEXT);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def conn():
    c = connect()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def dedupe_key(t: dict) -> str:
    if t.get("ext_id"):
        base = f"{t['source']}|{t['ext_id']}"
    else:
        base = f"{t['source']}|{t['ts'][:19]}|{t['kind']}|{round(t['btc'], 8)}"
    return hashlib.sha256(base.encode()).hexdigest()[:32]


COLS = ["ts", "kind", "source", "wallet", "to_wallet", "btc", "gbp", "fee_gbp", "fee_btc",
        "estimate", "example", "note", "ext_id"]


def insert_tx(c, t: dict) -> bool:
    row = {k: t.get(k) for k in COLS}
    for k in ("gbp", "fee_gbp", "fee_btc", "estimate", "example"):
        row[k] = row[k] or 0
    row["dedupe"] = t.get("dedupe") or dedupe_key(t)
    cur = c.execute(f"INSERT OR IGNORE INTO tx ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                    list(row.values()))
    return cur.rowcount == 1


DEFAULTS = {
    "other_income": 0, "personal_allowance": 12570, "basic_band": 37700, "basic_rate": 0.18,
    "higher_rate": 0.24, "annual_exempt": 3000, "exempt_used": 0, "sell_fee_pct": 0.0,
    "strike_api_key": "", "strike_job_enabled": False, "strike_daily_gbp": 20, "strike_dca_currency": "GBP",
    "strike_frequency": "daily", "strike_weekday": 0, "strike_monthday": 1, "strike_job_time": "15:25",
    "strike_check_delay_min": 10, "strike_last_balance": None,
    "xpub": "", "xpub_script": "auto", "gap_limit": 25,
    "electrum_host": os.environ.get("ELECTRUM_HOST", ""), "electrum_port": int(os.environ.get("ELECTRUM_PORT", "50001") or 50001),
    "electrum_ssl": False, "price_source": "coingecko",
    "cold_balance_cache": None, "strike_balance_cache": None,
}


def get_settings(c) -> dict:
    s = dict(DEFAULTS)
    for r in c.execute("SELECT k, v FROM settings"):
        s[r["k"]] = json.loads(r["v"])
    return s


def set_settings(c, d: dict):
    for k, v in d.items():
        c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def log(c, job, msg):
    from datetime import datetime, timezone
    c.execute("INSERT INTO joblog(ts,job,msg) VALUES(?,?,?)", (datetime.now(timezone.utc).isoformat(), job, msg))
