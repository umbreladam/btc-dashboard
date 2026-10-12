"""Electrum protocol client tested against an in-process fake server (no network)."""
import asyncio, json
from app.electrum import scan, derive

# Public BIP84 test vector (mnemonic "abandon x11 about"), not a real wallet
ZPUB = "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"


def test_scan_gap_limit():
    funded = {derive(ZPUB, 0, 3, 1)[0][2]: 50000, derive(ZPUB, 0, 30, 1)[0][2]: 25000, derive(ZPUB, 1, 0, 1)[0][2]: 1000}

    async def handle(r, w):
        while line := await r.readline():
            m = json.loads(line)
            p = m["params"]
            res = ["fake", "1.4"] if m["method"] == "server.version" else \
                ([{"tx_hash": "00", "height": 1}] if p[0] in funded else []) if m["method"].endswith("get_history") else \
                {"confirmed": funded.get(p[0], 0), "unconfirmed": 0}
            w.write((json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}) + "\n").encode())
            await w.drain()

    async def main(gap):
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            return await asyncio.wait_for(scan(ZPUB, "127.0.0.1", port, gap=gap), 30)
        finally:
            srv.close()  # don't wait_closed(): py3.12 waits for client connections

    # 26 unused addresses between index 3 and 30: missed with gap 20, found with gap 30
    r = asyncio.run(main(20))
    assert round(r["balance"], 8) == 0.00051 and len(r["used_addresses"]) == 2
    r = asyncio.run(main(30))
    assert round(r["balance"], 8) == 0.00076 and len(r["used_addresses"]) == 3
