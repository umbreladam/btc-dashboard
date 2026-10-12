# Satoshi BTC Dashboard (Umbrel community app)

Self-hosted, **Bitcoin-only** portfolio dashboard for umbrelOS. Dark CoinMarketCap-style UI, UK CGT
(HMRC share-matching), Coinbase/Kraken/Strike CSV importers, Strike read-only API daily job, and
cold-storage balance (single-sig xpub or **multisig descriptor**, watch-only) via **your own Electrs**, so your keys never leave the Umbrel.

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
  descriptor.py watch-only output descriptors (multisig, BIP-380 checksum, BIP-389 multipath, BIP-67) + wallet-export parsing
  prices.py     spot price (CoinGecko, Kraken fallback) + daily history (Kraken OHLC / CoinGecko), cached in SQLite
tests/          pytest: matching rules, tax bands, importers, Strike job, BIP84 vector, descriptor vectors (Core/BIP-380/67/389), Electrum scan vs fake server
examples/       sample CSVs in each exchange format (EXAMPLE data)
```

## Links
- App source + CI: https://github.com/umbreladam/btc-dashboard
- Community app store (paste this into Umbrel): https://github.com/umbreladam/umbrel-app-store
- Image: `ghcr.io/umbreladam/btc-dashboard` (multi-arch linux/amd64 + linux/arm64, built by GitHub Actions)
- Current release: `ghcr.io/umbreladam/btc-dashboard:1.0.1@sha256:a018c784d595e6d9828827cb66294578d540e5ed6427c6d0d33a1cc616b56498`

## Add the community app store to Umbrel
1. On Umbrel, open **App Store → ⋯ (top right) → Community App Stores**.
2. Paste `https://github.com/umbreladam/umbrel-app-store` and click **Add**.
3. Open the "Satoshi App Store" and install **Satoshi BTC Dashboard**. Umbrel will ask you to install **Electrs** first (declared dependency; Electrs needs Bitcoin Core).

## How to release an update
1. Change the code here, bump `version:` (e.g. `1.0.1`) and `releaseNotes` in `satoshi-btc-dashboard/umbrel-app.yml`, commit and push to `main`.
2. Tag it: `git tag v1.0.1 && git push origin v1.0.1`. CI (`.github/workflows/build.yml`) runs the tests, then builds amd64 + arm64 with buildx/QEMU and pushes `ghcr.io/umbreladam/btc-dashboard:latest` and `:1.0.1` (the version is read from `umbrel-app.yml`).
   **Avoid digest churn:** a push to `main` and a tag push each start a build, and each rebuild gives `:1.0.1` a new digest. Build exactly once: put `[skip ci]` in the release commit message, push `main` and the tag, then run the workflow once on the tag (`gh workflow run build.yml --ref v1.0.1`). Pin the digest from that run.
3. Get the multi-arch digest: `docker buildx imagetools inspect ghcr.io/umbreladam/btc-dashboard:1.0.1` (or `crane digest ...`).
4. In `satoshi-btc-dashboard/docker-compose.yml` set `image: ghcr.io/umbreladam/btc-dashboard:1.0.1@sha256:<digest>`.
5. Commit/push (README and compose-only changes don't trigger a rebuild, so the pinned digest stays valid). Copy `umbrel-app-store.yml` and `satoshi-btc-dashboard/` into the `umbreladam/umbrel-app-store` repo and push. Umbrel then shows an **Update** button.

## First-run setup
- Open the app. You'll see example data. Go to **Import** and upload your exports (Auto-detect works):
  - **Coinbase**: Profile → Statements/Reports → *Transaction history* CSV (columns `ID, Timestamp, Transaction Type, Asset, Quantity Transacted, Price Currency, Price at Transaction, Subtotal, Total (inclusive of fees and/or spread), Fees and/or Spread, Notes`; the preamble lines are skipped; the older format without `ID` works too).
  - **Kraken**: History → Export → **Ledgers** (`txid, refid, time, type, subtype, aclass, asset, [wallet,] amount, fee, balance`; trade legs are joined by `refid`). **Trades** export works too.
  - **Strike**: Settings → Reports / account statement CSV (`Reference, Date & Time (UTC), Transaction Type, Amount GBP, Fee GBP, Amount BTC, Fee BTC, ...`; older `Transaction ID / Completed Date (UTC) / Amount USD` layouts are tolerated, with a warning when no GBP amount exists).
  - Preview shows every parsed row, parse issues, warnings (e.g. non-GBP trades) and duplicates (skipped). Then click Import.
- Withdrawals/sends are imported as **transfers to cold storage** (not disposals). Check them in the Transactions page.
- **Settings → Data → Delete example data.**

### Strike DCA (optional)
You don't need Strike: with the scheduled job off (the default) you can use the app purely with CSV import and manual entries.

**Create a read-only Strike API key:**
1. Sign in at https://dashboard.strike.me.
2. Open the **API Keys** section and create a new key.
3. Name it, e.g. `BTC Dashboard`.
4. Tick **only** `partner.balances.read` (read account balances) and `partner.rates.ticker` (exchange rates). Leave all other permissions unticked.
5. Copy the key (Strike shows it only once), paste it in *Settings → Strike DCA*, press **Save**, then **Test key** (calls `GET https://api.strike.me/v1/balances` and `/rates/ticker`).

With only those two permissions the key is read-only and **cannot move funds**. It is stored only in the app's local SQLite database in `${APP_DATA_DIR}/data` on your Umbrel and is sent only to api.strike.me. It is never part of the image or compose file. (An optional `STRIKE_API_KEY` environment variable overrides it at runtime for development; don't put it in the compose file.)

**Schedule (all in Settings):** enable/disable, amount and currency (GBP/USD/EUR; non-GBP amounts are converted to GBP with Strike's rates), frequency **daily**, **weekly** (pick the weekday) or **monthly** (pick the day; 29-31 fall back to the last day of shorter months), buy time (UK) and how many minutes after it to check the balance (default 10). On scheduled days only:
- first run stores a baseline balance;
- balance up: records the increase as a buy of the configured amount;
- balance down: records an **estimated** buy of the configured amount at Strike's rate plus an **estimated** transfer of the rest to cold storage (both tagged *estimate*).
When you later import the Strike CSV, delete the overlapping `strike-api` rows (filter by source) so buys aren't counted twice.

### Cold storage (xpub or multisig descriptor, watch-only)
*Settings → Cold storage*: add one or more wallets, each with a label (e.g. a 2-of-3 multisig vault plus a single-sig wallet). For each, paste one of:
- a single-sig **xpub / ypub / zpub** (script type auto-detected from the prefix, or choose one, including Taproot);
- an **output descriptor** (needed for **multisig**: an xpub alone can't describe a multisig wallet). Supported: `wsh(sortedmulti(...))`, `wsh(multi(...))`, `sh(wsh(sortedmulti/multi(...)))`, `sh(sortedmulti/multi(...))`, plus single-sig `wpkh(...)`, `sh(wpkh(...))`, `pkh(...)`, `tr(KEY)`. Keys may carry origins (`[fingerprint/48h/0h/0h/2h]xpub...`) and be xpub/Ypub/Zpub/tpub/Vpub-style; use `/0/*` (change `/1/*` is then inferred) or multipath `/<0;1>/*` (BIP-389). A trailing `#checksum` is verified (BIP-380); without one the descriptor is still accepted;
- or a pasted **wallet export**: Sparrow / Specter / Nunchuk (BSMS) / Bitcoin Core JSON or text containing a descriptor, **Caravan** JSON (quorum + extendedPublicKeys), an **Electrum** multisig wallet file (`wallet_type: "2of3"`, `x1/`...), or a **Coldcard**-style config (`Policy: 2 of 3`, `Format: P2WSH`, `Derivation:`, `XFP: xpub` lines). Key lists are assumed to be `sortedmulti` (what all these coordinators use). If an export isn't recognised, paste the descriptor.

Where to find the descriptor:
- **Sparrow**: open the wallet → *Settings* → *Script Policy* → *Edit…* shows the descriptor (or *Export…* → *Output Descriptor*).
- **Specter Desktop**: wallet → *Settings* → *Export* (export to wallet software): paste the JSON or its `descriptor`.
- **Nunchuk**: wallet → ⋯ → *Export wallet configuration* (BSMS/descriptor file): paste its contents.
- **Caravan**: *Download wallet configuration* (JSON): paste the JSON.
- **Electrum**: *Wallet → Information* shows the cosigner master public keys; paste the wallet file JSON, or build the descriptor yourself (`wsh(sortedmulti(2,Zpub1/<0;1>/*,Zpub2/<0;1>/*,Zpub3/<0;1>/*))`).

Click **Show first addresses** (works before saving, and per saved wallet; shows receive `0/i` and change `1/i`) and check they match your wallet (e.g. Sparrow's *Addresses* tab), then **Scan now**. Receive and change chains are scanned with a gap limit (default 25, min 20), per wallet; the total and a per-wallet breakdown are shown.
- **Watch-only.** Descriptors and xpubs contain only public keys: they can't spend. The app never builds, signs or exports transactions or PSBTs, and rejects private keys (xprv) if pasted. But they are **privacy-sensitive**: anyone with them can see all your addresses, balances and history. Treat them like a bank statement.
- They are stored only in SQLite in `/data` and used only to derive addresses locally. Only script hashes are sent, and only to your Electrum server. They are never sent to any third party. Prices come from public endpoints with no account data.
- Umbrel default Electrs: `APP_ELECTRS_NODE_IP=10.21.21.10`, `APP_ELECTRS_NODE_PORT=50001` (from getumbrel/umbrel-apps `electrs/exports.sh`), injected automatically as the default host/port. Plain TCP, no SSL.
- A 1.0.0 single xpub carries over as a wallet called "Cold storage".
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
