"""Cold-storage balance via YOUR OWN Electrum server (Electrs on Umbrel).

The xpub never leaves the box: addresses are derived locally and only script hashes are
sent to the configured Electrum server (default: the local Umbrel Electrs app).
"""
import asyncio, json, ssl, hashlib
from embit import bip32, script
from embit.networks import NETWORKS

# SLIP-132 version bytes -> script type
VERSIONS = {
    "xpub": "p2pkh", "ypub": "p2sh-p2wpkh", "zpub": "p2wpkh",
    "tpub": "p2pkh", "upub": "p2sh-p2wpkh", "vpub": "p2wpkh",
}


def parse_xpub(x: str, override: str = "auto"):
    x = x.strip()
    k = bip32.HDKey.from_string(x)
    stype = VERSIONS.get(x[:4], "p2wpkh") if override in ("", "auto") else override
    net = NETWORKS["test"] if x[0] in "tuv" else NETWORKS["main"]
    return k, stype, net


def _script(pub, stype):
    if stype == "p2pkh":
        return script.p2pkh(pub)
    if stype == "p2sh-p2wpkh":
        return script.p2sh(script.p2wpkh(pub))
    if stype == "p2tr":
        return script.p2tr(pub)
    return script.p2wpkh(pub)


def derive(x: str, chain: int, start: int, n: int, override="auto"):
    k, stype, net = parse_xpub(x, override)
    out = []
    for i in range(start, start + n):
        pub = k.derive([chain, i]).key
        sc = _script(pub, stype)
        sh = hashlib.sha256(sc.data).digest()[::-1].hex()
        out.append((f"{chain}/{i}", sc.address(net), sh))
    return out


class Client:
    def __init__(self, host, port, use_ssl=False, timeout=15):
        self.host, self.port, self.ssl, self.timeout = host, int(port), use_ssl, timeout
        self.id = 0

    async def __aenter__(self):
        ctx = None
        if self.ssl:
            ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        self.r, self.w = await asyncio.wait_for(asyncio.open_connection(self.host, self.port, ssl=ctx), self.timeout)
        await self.call("server.version", ["satoshi-btc-dashboard", "1.4"])
        return self

    async def __aexit__(self, *a):
        self.w.close()

    async def call(self, method, params):
        return (await self.batch([(method, params)]))[0]

    async def batch(self, calls):
        reqs = []
        for m, p in calls:
            self.id += 1
            reqs.append({"jsonrpc": "2.0", "id": self.id, "method": m, "params": p})
        self.w.write(("\n".join(json.dumps(r) for r in reqs) + "\n").encode())
        await self.w.drain()
        res = {}
        while len(res) < len(reqs):
            line = await asyncio.wait_for(self.r.readline(), self.timeout)
            if not line:
                raise ConnectionError("Electrum server closed connection")
            msg = json.loads(line)
            if "id" in msg and msg["id"] is not None:
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                res[msg["id"]] = msg.get("result")
        return [res[r["id"]] for r in reqs]


async def scan(xpub, host, port, use_ssl=False, gap=25, override="auto"):
    """Gap-limit scan of receive (0) and change (1) chains. Returns balance in BTC + used addresses."""
    total_conf = total_unconf = 0
    used = []
    async with Client(host, port, use_ssl) as c:
        for chain in (0, 1):
            idx, empty = 0, 0
            while empty < gap:
                addrs = derive(xpub, chain, idx, gap, override)
                hist = await c.batch([("blockchain.scripthash.get_history", [a[2]]) for a in addrs])
                bals = await c.batch([("blockchain.scripthash.get_balance", [a[2]]) for a in addrs])
                for (path, addr, _), h, b in zip(addrs, hist, bals):
                    if h:
                        empty = 0
                        total_conf += b["confirmed"]; total_unconf += b["unconfirmed"]
                        used.append({"path": path, "address": addr, "txs": len(h),
                                     "balance": (b["confirmed"] + b["unconfirmed"]) / 1e8})
                    else:
                        empty += 1
                        if empty >= gap:
                            break
                idx += gap
    return {"confirmed": total_conf / 1e8, "unconfirmed": total_unconf / 1e8,
            "balance": (total_conf + total_unconf) / 1e8, "used_addresses": used}
