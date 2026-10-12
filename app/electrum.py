"""Cold-storage balance via YOUR OWN Electrum server (Electrs on Umbrel).

Watch-only. The xpub/descriptor never leaves the box: addresses are derived locally and only script hashes are
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


def is_descriptor(x: str) -> bool:
    from .descriptor import looks_like_descriptor_or_export
    return looks_like_descriptor_or_export(x)


def derive(x: str, chain: int, start: int, n: int, override="auto"):
    """x is a single xpub/ypub/zpub or an output descriptor (single- or multisig)."""
    if is_descriptor(x):
        from .descriptor import from_input
        return from_input(x).derive(chain, start, n)
    k, stype, net = parse_xpub(x, override)
    out = []
    for i in range(start, start + n):
        pub = k.derive([chain, i]).key
        sc = _script(pub, stype)
        sh = hashlib.sha256(sc.data).digest()[::-1].hex()
        out.append((f"{chain}/{i}", sc.address(net), sh))
    return out


_HOST_RE = __import__("re").compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def validate_host(host: str, port) -> tuple[str, int]:
    """Hostname or IP literal (no scheme, path, credentials or spaces) and a port 1-65535."""
    import ipaddress
    h = str(host or "").strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        ipaddress.ip_address(h)
    except ValueError:
        if not _HOST_RE.match(h):
            raise ValueError("Electrum host must be a hostname or IP address")
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError("Electrum port must be a number")
    if not 1 <= p <= 65535:
        raise ValueError("Electrum port must be 1-65535")
    return h, p


def norm_fingerprint(fp: str) -> str:
    f = str(fp or "").replace(":", "").replace(" ", "").lower()
    if f.startswith("sha256"):
        f = f[6:].lstrip("=")
    if f and (len(f) != 64 or any(c not in "0123456789abcdef" for c in f)):
        raise ValueError("Certificate fingerprint must be a SHA-256 hex digest (64 hex chars, colons allowed)")
    return f


class Client:
    def __init__(self, host, port, use_ssl=False, timeout=15, cert_fp=""):
        self.host, self.port, self.ssl, self.timeout = host, int(port), use_ssl, timeout
        self.cert_fp = norm_fingerprint(cert_fp)
        self.id = 0

    async def __aenter__(self):
        ctx = None
        if self.ssl:
            ctx = ssl.create_default_context()
            if self.cert_fp:  # pinned (e.g. self-signed electrs cert): verify by exact SHA-256 below instead of CA chain
                ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        self.r, self.w = await asyncio.wait_for(asyncio.open_connection(
            self.host, self.port, ssl=ctx, server_hostname=self.host if ctx else None), self.timeout)
        if ctx and self.cert_fp:
            der = self.w.get_extra_info("ssl_object").getpeercert(binary_form=True)
            if not der or hashlib.sha256(der).hexdigest() != self.cert_fp:
                self.w.close()
                raise ssl.SSLError("Electrum server certificate does not match the pinned fingerprint")
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


async def scan(xpub, host, port, use_ssl=False, gap=25, override="auto", cert_fp=""):
    """Gap-limit scan of receive (0) and change (1) chains. Returns balance in BTC + used addresses."""
    total_conf = total_unconf = 0
    used = []
    if is_descriptor(xpub):
        from .descriptor import from_input
        d = from_input(xpub)  # parse once
        chains, derive_fn = d.chains(), (lambda chain, i, n: d.derive(chain, i, n))
    else:
        parse_xpub(xpub, override)
        chains, derive_fn = (0, 1), (lambda chain, i, n: derive(xpub, chain, i, n, override))
    gap = max(int(gap), 20)
    async with Client(host, port, use_ssl, cert_fp=cert_fp) as c:
        for chain in chains:
            idx, empty = 0, 0
            while empty < gap:
                addrs = derive_fn(chain, idx, gap)
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
