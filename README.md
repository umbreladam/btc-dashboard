# Satoshi BTC Dashboard (Umbrel community app)

Self-hosted, **Bitcoin-only** portfolio dashboard for umbrelOS. Dark CoinMarketCap-style UI, UK CGT
(HMRC share-matching), Coinbase/Kraken/Strike CSV importers, Strike read-only API daily job, and
cold-storage balance via **your own Electrs**, so the xpub never leaves the Umbrel.

> Ships with clearly labelled **EXAMPLE data** (purple "example" tags plus a yellow banner). Delete it in
> *Settings → Data → Delete example data* after importing your real records. **Not tax advice.**

## Layout
```
umbrel-app-store.yml            # community store manifest (id: satoshi)
satoshi-btc-dashboard/          # Umbrel app folder (id must be prefixed with store id)
  umbrel-app.yml                # manifest, bump `version` for updates
  docker-compose.yml            # app_proxy + web service, /data volume, Electrs env
  data/.gitkeep                 # persistent data dir (${APP_DATA_DIR}/data -> /data)
Dockerfile, requirements.txt    # single image, pure-Python deps (amd64 + arm64)
app/                            # FastAPI backend + static frontend (Chart.js vendored, no CDN)
  cgt.py        HMRC same-day / 30-day / Section 104 matching + tax calculation
  importers.py  Coinbase, Kraken (ledgers.csv & trades.csv), Strike CSV parsers
  strike.py     Strike API (GET /balances, GET /rates/ticker) + daily-job logic
  electrum.py   xpub/ypub/zpub derivation (embit) + Electrum protocol client, gap-limit scan
  prices.py     spot price (CoinGecko, Kraken fallback) + daily history (Kraken OHLC / CoinGecko), cached in SQLite
tests/          pytest: matching rules, tax bands, importers, Strike job, BIP84 vector, Electrum scan vs fake server
examples/       sample CSVs in each exchange format (EXAMPLE data)
```

## Build and push the image
You need a container registry. These steps use GitHub Container Registry; replace `YOUR_GITHUB_USER`.
```bash
echo $GHCR_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin   # PAT with write:packages
docker buildx create --use   # once
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/YOUR_GITHUB_USER/satoshi-btc-dashboard:1.0.0 --push .
```
Make the package **public** in GitHub (Packages → settings) so Umbrel can pull it without credentials.
Then put the tag (best with digest: `...:1.0.0@sha256:...`, shown by `docker buildx imagetools inspect`) in
`satoshi-btc-dashboard/docker-compose.yml`, and replace the `YOUR_*` placeholders in `umbrel-app.yml`.

## Add the community app store to Umbrel
1. Push this folder to a **public** GitHub repo (e.g. `YOUR_GITHUB_USER/umbrel-satoshi-store`). `umbrel-app-store.yml` must be at the repo root.
2. On Umbrel, open **App Store → ⋯ (top right) → Community App Stores**, paste the repo URL and click **Add**.
3. Open the "Satoshi App Store" and install **Satoshi BTC Dashboard**. Umbrel will ask you to install **Electrs** first (declared dependency; Electrs needs Bitcoin Core).

## First-run setup
- Open the app. You'll see example data. Go to **Import** and upload your exports (Auto-detect works):
  - **Coinbase**: Profile → Statements/Reports → *Transaction history* CSV (columns `ID, Timestamp, Transaction Type, Asset, Quantity Transacted, Price Currency, Price at Transaction, Subtotal, Total (inclusive of fees and/or spread), Fees and/or Spread, Notes`; the preamble lines are skipped; the older format without `ID` works too).
  - **Kraken**: History → Export → **Ledgers** (`txid, refid, time, type, subtype, aclass, asset, [wallet,] amount, fee, balance`; trade legs are joined by `refid`). **Trades** export works too.
  - **Strike**: Settings → Reports / account statement CSV (`Reference, Date & Time (UTC), Transaction Type, Amount GBP, Fee GBP, Amount BTC, Fee BTC, ...`; older `Transaction ID / Completed Date (UTC) / Amount USD` layouts are tolerated, with a warning when no GBP amount exists).
  - Preview shows every parsed row, parse issues, warnings (e.g. non-GBP trades) and duplicates (skipped). Then click Import.
- Withdrawals/sends are imported as **transfers to cold storage** (not disposals). Check them in the Transactions page.
- **Settings → Data → Delete example data.**

### Strike API key
*Settings → Strike*: paste a **read-only** key (create it at dashboard.strike.me → API keys; scopes for balances + rates only).
"Test key" calls `GET https://api.strike.me/v1/balances` and `/rates/ticker` with `Authorization: Bearer <key>`.
A scheduler runs daily at **15:25 Europe/London** (configurable):
- first run stores a baseline balance;
- balance up → records the increase as a £20 buy (amount configurable);
- balance down → records an **estimated** buy of £20 ÷ Strike BTC/GBP rate plus an **estimated** transfer of the rest to cold storage (both tagged *estimate*).
When you later import the Strike CSV, delete the overlapping `strike-api` rows (filter by source) so buys aren't counted twice.

### Cold storage xpub
*Settings → Cold storage*: paste your **xpub / ypub / zpub** (script type auto-detected from the prefix, or choose one, including Taproot).
Click **Show first addresses** and check they match your wallet, then **Scan now**. Receive and change chains are scanned with a gap limit (default 25, min 20).
- Umbrel default Electrs: `APP_ELECTRS_NODE_IP=10.21.21.10`, `APP_ELECTRS_NODE_PORT=50001` (from getumbrel/umbrel-apps `electrs/exports.sh`), injected automatically as the default host/port. Plain TCP, no SSL.
- The xpub is stored only in SQLite in `/data` and is only used to derive addresses locally. Only script hashes are sent, and only to your Electrum server. Prices come from public endpoints with no account data.
- Once scanned, the live cold balance (and live Strike balance) replace the ledger figures in the "Total BTC held" split. If they differ from your transaction records, a warning shows the gap.

## Tax method (UK)
- Disposals are matched **same-day → next 30 days (B&B, earliest first) → Section 104 pool**, using UK-local dates. Same-day matching across all days is done before any 30-day matching.
- Cost basis shown = Section 104 pool cost. Fees are added to cost and deducted from proceeds.
- **Value if sold today** = a hypothetical disposal of everything at spot (minus an optional selling fee %), plus gains already realised this tax year, minus the remaining annual exempt amount (£3,000 by default, with an "already used" field). Tax is 18% within the remaining basic-rate band (band £37,700 above the £12,570 personal allowance, tapered over £100k income) and 24% above. All values are configurable.
- Transfers between your own wallets are not disposals. The network fee they burn reduces the pool quantity but keeps its cost (a simplified, conservative treatment: strictly, HMRC treats a fee paid in crypto as a small disposal).
- Rewards/"income" rows from Coinbase are imported as acquisitions at market value and flagged in the note.

## Updates
Data lives in `${APP_DATA_DIR}/data` (mounted at `/data`) and survives updates and reinstalls of the image.
To release: build and push a new tag (e.g. `1.0.1`), update `image:` in `docker-compose.yml`, bump `version:` and `releaseNotes` in `umbrel-app.yml`, and push to the store repo. Umbrel then shows an **Update** button.

## Local development
```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt pytest
python -m pytest -q
DATA_DIR=./devdata uvicorn app.main:app --port 8765    # http://localhost:8765
pip install playwright && python screenshot.py           # screenshots/ (uses /usr/bin/google-chrome)
```
Env: `DATA_DIR`, `ELECTRUM_HOST`, `ELECTRUM_PORT`, `LOAD_EXAMPLE_DATA=0` to start empty.

Security: the app relies on Umbrel's `app_proxy` login. Don't expose the port directly to the internet.
