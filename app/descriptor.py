"""Watch-only output descriptors (BIP-380/381/382/383/389, BIP-67) and wallet-export parsing.

Public-key only. This module derives addresses/scripts for balance lookups; it never builds,
signs or exports transactions or PSBTs, and descriptors are never sent anywhere (only script
hashes go to your own Electrum server).
"""
import hashlib, json, re
from embit import bip32, base58, script
from embit.networks import NETWORKS

# ---------------- BIP-380 checksum ----------------
INPUT_CHARSET = ("0123456789()[],'/*abcdefgh@:$%{}IJKLMNOPQRSTUVWXYZ&+-.;<=>?!^_|~"
                 "ijklmnopqrstuvwxyzABCDEFGH`#\"\\ ")
CHECKSUM_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(c, val):
    c0 = c >> 35
    c = ((c & 0x7ffffffff) << 5) ^ val
    for i, g in enumerate((0xf5dee51989, 0xa9fdca3312, 0x1bab10e32d, 0x3706b1677a, 0x644d626ffd)):
        if c0 & (1 << i):
            c ^= g
    return c


def checksum(desc: str) -> str:
    c, cls, clscount = 1, 0, 0
    for ch in desc:
        pos = INPUT_CHARSET.find(ch)
        if pos == -1:
            raise ValueError(f"Invalid character {ch!r} in descriptor")
        c = _polymod(c, pos & 31)
        cls = cls * 3 + (pos >> 5)
        clscount += 1
        if clscount == 3:
            c = _polymod(c, cls); cls = clscount = 0
    if clscount:
        c = _polymod(c, cls)
    for _ in range(8):
        c = _polymod(c, 0)
    c ^= 1
    return "".join(CHECKSUM_CHARSET[(c >> (5 * (7 - j))) & 31] for j in range(8))


def split_checksum(s: str):
    """Return (descriptor, had_checksum). Raises ValueError if a present checksum is wrong."""
    s = "".join(s.split())  # descriptors contain no whitespace; tolerate line-wrapped pastes
    if "#" not in s:
        checksum(s)  # charset validation
        return s, False
    body, _, cs = s.rpartition("#")
    if len(cs) != 8:
        raise ValueError(f"Expected 8 character checksum, not {len(cs)} characters")
    if checksum(body) != cs:
        raise ValueError(f"Descriptor checksum mismatch (got #{cs}, expected #{checksum(body)}); "
                         "check you copied the whole descriptor")
    return body, True


# ---------------- keys ----------------
# SLIP-132 version bytes -> canonical xpub/tpub
_PUB_MAIN, _PUB_TEST = bytes.fromhex("0488b21e"), bytes.fromhex("043587cf")
SLIP132 = {
    "0488b21e": ("main", None), "049d7cb2": ("main", "sh-wpkh"), "04b24746": ("main", "wpkh"),
    "0295b43f": ("main", "sh-wsh"), "02aa7ed3": ("main", "wsh"),
    "043587cf": ("test", None), "044a5262": ("test", "sh-wpkh"), "045f1cf6": ("test", "wpkh"),
    "024289ef": ("test", "sh-wsh"), "02575483": ("test", "wsh"),
}


def parse_extkey(s: str):
    """Any SLIP-132 public extended key -> (HDKey, net name, script hint). Private keys rejected."""
    raw = base58.decode_check(s)
    ver = raw[:4].hex()
    if ver not in SLIP132:
        if ver in ("0488ade4", "04358394", "049d7878", "04b2430c", "02aa7a99", "0295b005"):
            raise ValueError("That is a PRIVATE key. Only paste public keys (xpub/Zpub...); never share private keys")
        raise ValueError(f"Unknown extended key version {ver}")
    net, hint = SLIP132[ver]
    k = bip32.HDKey.parse((_PUB_MAIN if net == "main" else _PUB_TEST) + raw[4:])
    return k, net, hint


def _parse_index(p: str) -> int:
    h = p[-1:] in ("'", "h", "H")
    n = int(p[:-1] if h else p)
    if n < 0 or n >= 2 ** 31:
        raise ValueError(f"Path index out of range: {p}")
    return n + (2 ** 31 if h else 0)


def fmt_path(path):
    return "/".join(f"{i - 2**31}h" if i >= 2 ** 31 else str(i) for i in path)


class Key:
    """[fp/origin]KEY/path/<a;b>/* - either an extended key (ranged) or a fixed hex pubkey."""

    def __init__(self, s: str):
        self.src = s
        self.fingerprint = self.origin = self.multipath = None
        m = re.fullmatch(r"(?:\[([0-9a-fA-F]{8})((?:/[0-9]+['hH]?)*)\])?([^/\[\]]+)((?:/[^/]+)*)", s)
        if not m:
            if s.count("[") > 1:
                raise ValueError("Multiple key origins for one key")
            raise ValueError(f"Cannot parse key expression {s[:40]!r}")
        fp, origin, k, suffix = m.groups()
        if s.startswith("[") and not fp:
            raise ValueError("Key origin fingerprint must be 8 hex characters")
        if fp:
            self.fingerprint = fp.lower()
            self.origin = [_parse_index(p) for p in origin.split("/")[1:]]
        self.hd = None
        self.net, self.hint = "main", None
        if re.fullmatch(r"0[23][0-9a-fA-F]{64}", k):
            if suffix:
                raise ValueError("A plain public key cannot have a derivation path")
            self.pub = bytes.fromhex(k)
            self.steps, self.ranged = [], False
            return
        self.hd, self.net, self.hint = parse_extkey(k)
        steps = suffix.split("/")[1:]
        self.ranged = bool(steps) and steps[-1] == "*"
        if any(p.endswith(("*'", "*h", "*H")) for p in steps):
            raise ValueError("Hardened wildcard derivation (/*') needs private keys; not supported for watch-only")
        if self.ranged:
            steps = steps[:-1]
        if "*" in steps:
            raise ValueError("'*' may only appear as the last step")
        # each step: int or tuple of ints (multipath, BIP-389)
        self.steps = []
        self.multipath = None
        for p in steps:
            mp = re.fullmatch(r"<([^>]+)>", p)
            if mp:
                if self.multipath is not None:
                    raise ValueError("Only one multipath <a;b> group per key")
                alts = tuple(_parse_index(x) for x in mp.group(1).split(";"))
                if len(alts) < 2:
                    raise ValueError("Multipath group needs at least two indexes")
                self.multipath = len(alts)
                self.steps.append(alts)
            else:
                i = _parse_index(p)
                if i >= 2 ** 31:
                    raise ValueError("Hardened derivation after the xpub needs private keys; not supported")
                self.steps.append(i)

    def n_paths(self):
        return self.multipath or 1

    def pubkey(self, branch: int, idx=None) -> bytes:
        if self.hd is None:
            return self.pub
        path = [s[branch] if isinstance(s, tuple) else s for s in self.steps]
        if self.ranged:
            path.append(idx)
        return self.hd.derive(path).key.sec() if path else self.hd.key.sec()

    def to_str(self, branch=None):
        o = f"[{self.fingerprint}{'/' + fmt_path(self.origin) if self.origin else ''}]" if self.fingerprint else ""
        if self.hd is None:
            return o + self.pub.hex()
        steps = []
        for s in self.steps:
            if isinstance(s, tuple):
                steps.append(str(s[branch]) if branch is not None else "<" + ";".join(map(str, s)) + ">")
            else:
                steps.append(str(s))
        if self.ranged:
            steps.append("*")
        return o + self.hd.to_base58() + "".join("/" + p for p in steps)


# ---------------- descriptors ----------------
def _split_args(s: str):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([<":
            depth += 1
        elif ch in ")]>":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _unwrap(s, name):
    if s.startswith(name + "(") and s.endswith(")"):
        return s[len(name) + 1:-1]
    return None


SCRIPT_TYPES = {  # wrapper -> label
    "wsh": "P2WSH multisig (native SegWit, bc1q...)", "sh-wsh": "P2SH-P2WSH multisig (nested SegWit, 3...)",
    "sh": "P2SH multisig (legacy, 3...)", "wpkh": "P2WPKH (bc1q...)", "sh-wpkh": "P2SH-P2WPKH (3...)",
    "pkh": "P2PKH (1...)", "tr": "P2TR key-path (bc1p...)",
}


def multisig_script(m: int, pubs, sort=False) -> bytes:
    pubs = sorted(pubs) if sort else list(pubs)  # BIP-67: lexicographic order of compressed keys
    n = len(pubs)
    b = bytes([0x50 + m])
    for p in pubs:
        b += bytes([len(p)]) + p
    return b + bytes([0x50 + n, 0xae])


class Descriptor:
    def __init__(self, text: str):
        body, self.had_checksum = split_checksum(text)
        self.body = body
        inner = None
        for wrap in (("sh", "wsh"), ("wsh",), ("sh", "wpkh"), ("wpkh",), ("pkh",), ("tr",), ("sh",)):
            cur = body
            for w in wrap:
                cur = _unwrap(cur, w)
                if cur is None:
                    break
            if cur is not None:
                self.wrap, inner = "-".join(wrap), cur
                break
        if inner is None:
            raise ValueError("Unsupported descriptor. Supported: wsh(sortedmulti/multi), sh(wsh(...)), "
                             "sh(sortedmulti/multi), wpkh, sh(wpkh), pkh, tr(key)")
        if self.wrap in ("wpkh", "sh-wpkh", "pkh", "tr"):
            self.m, self.sorted = 1, False
            self.keys = [Key(inner)]
            self.multisig = False
        else:
            self.multisig = True
            if inner.startswith("sortedmulti("):
                self.sorted, args = True, inner[len("sortedmulti("):-1]
            elif inner.startswith("multi("):
                self.sorted, args = False, inner[len("multi("):-1]
            else:
                raise ValueError("Only multi(...) / sortedmulti(...) are supported inside sh()/wsh()")
            if not inner.endswith(")"):
                raise ValueError("Unbalanced parentheses")
            parts = _split_args(args)
            if not parts[0].isdigit():
                raise ValueError("Threshold must be a number")
            self.m = int(parts[0])
            self.keys = [Key(k) for k in parts[1:]]
            n = len(self.keys)
            if not 1 <= self.m <= n:
                raise ValueError(f"Threshold {self.m} must be between 1 and the number of keys ({n})")
            if n > (15 if self.wrap == "sh" else 20):
                raise ValueError(f"Too many keys ({n}) for {self.wrap}()")
        nets = {k.net for k in self.keys if k.hd}
        if len(nets) > 1:
            raise ValueError("Mixed mainnet and testnet keys")
        self.net = nets.pop() if nets else "main"
        npaths = {k.n_paths() for k in self.keys if k.multipath}
        if len(npaths) > 1:
            raise ValueError("Multipath groups have mismatched lengths")
        self.n_paths = npaths.pop() if npaths else 1
        self.ranged = any(k.ranged for k in self.keys)
        # Receive/change: multipath <0;1> gives both. A single-path descriptor ending in /0/* gets
        # its change chain by swapping that last step to 1 (what Sparrow & co. do).
        self.auto_change = False
        if self.n_paths == 1 and self.ranged and all(k.hd is None or (k.steps and k.steps[-1] == 0) for k in self.keys):
            for k in self.keys:
                if k.hd is not None:
                    k.steps[-1] = (0, 1); k.multipath = 2
            self.n_paths, self.auto_change = 2, True

    @property
    def network(self):
        return NETWORKS["main" if self.net == "main" else "test"]

    def chains(self):
        return list(range(min(self.n_paths, 2)))

    def script_pubkey(self, branch: int, idx: int):
        pubs = [k.pubkey(branch, idx) for k in self.keys]
        if not self.multisig:
            p = pubs[0]
            from embit.ec import PublicKey
            pk = PublicKey.parse(p)
            return {"wpkh": script.p2wpkh, "pkh": script.p2pkh, "tr": script.p2tr,
                    "sh-wpkh": lambda k: script.p2sh(script.p2wpkh(k))}[self.wrap](pk)
        ws = script.Script(multisig_script(self.m, pubs, self.sorted))
        if self.wrap == "wsh":
            return script.p2wsh(ws)
        if self.wrap == "sh-wsh":
            return script.p2sh(script.p2wsh(ws))
        if len(ws.data) > 520:
            raise ValueError("P2SH redeem script too large")
        return script.p2sh(ws)

    def derive(self, chain: int, start: int, n: int):
        out = []
        for i in range(start, start + n):
            sc = self.script_pubkey(chain, i)
            sh = hashlib.sha256(sc.data).digest()[::-1].hex()
            out.append((f"{chain}/{i}", sc.address(self.network), sh))
        return out

    def summary(self):
        kind = f"{self.m}-of-{len(self.keys)} {'sortedmulti' if self.sorted else 'multi'}" if self.multisig else "single-sig"
        return {"type": SCRIPT_TYPES[self.wrap], "policy": kind, "keys": len(self.keys),
                "fingerprints": [k.fingerprint for k in self.keys if k.fingerprint],
                "network": "mainnet" if self.net == "main" else "testnet",
                "checksum_verified": self.had_checksum, "change_chain_inferred": self.auto_change}

    def canonical(self):
        """Multipath form with checksum, e.g. wsh(sortedmulti(2,[fp/48h/0h/0h/2h]xpub.../<0;1>/*,...))#cs"""
        ks = ",".join(k.to_str() for k in self.keys)
        inner = f"{'sortedmulti' if self.sorted else 'multi'}({self.m},{ks})" if self.multisig else ks
        for w in reversed(self.wrap.split("-")):
            inner = f"{w}({inner})"
        return inner + "#" + checksum(inner)


# ---------------- wallet exports -> descriptor ----------------
_FMT = {"p2wsh": "wsh", "wsh": "wsh", "p2sh-p2wsh": "sh-wsh", "p2wsh-p2sh": "sh-wsh", "sh-wsh": "sh-wsh",
        "p2sh_p2wsh": "sh-wsh", "p2wsh_p2sh": "sh-wsh", "p2sh": "sh", "sh": "sh"}
_DESC_RE = re.compile(r"(?:sh|wsh|wpkh|pkh|tr)\([^\s\"']+")


def _norm_path(p):
    p = (p or "").strip().lstrip("mM").strip("/")
    return "/" + p.replace("'", "h") if p else ""


def _build(m, fmt, keys):
    """keys: list of (fingerprint|None, path|'', xpub). Uses sortedmulti (all common coordinators do)."""
    w = _FMT.get(str(fmt).lower().replace(" ", ""))
    if not w:
        raise ValueError(f"Unknown multisig script type {fmt!r}")
    ks = []
    for fp, path, x in keys:
        o = f"[{fp.lower()}{_norm_path(path)}]" if fp else ""
        ks.append(f"{o}{x}/<0;1>/*")
    inner = f"sortedmulti({int(m)},{','.join(ks)})"
    for part in reversed(w.split("-")):
        inner = f"{part}({inner})"
    return inner


def _find_desc_strings(obj):
    if isinstance(obj, str):
        if _DESC_RE.match(obj.strip()):
            yield obj.strip()
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _find_desc_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_desc_strings(v)


def _from_json(d):
    # Caravan
    if isinstance(d, dict) and "quorum" in d and "extendedPublicKeys" in d:
        q = d["quorum"]
        keys = [(k.get("xfp"), k.get("bip32Path", ""), k["xpub"]) for k in d["extendedPublicKeys"]]
        return _build(q["requiredSigners"], d.get("addressType", "P2WSH"), keys)
    # Electrum wallet file: wallet_type "2of3", x1/ x2/ ...
    if isinstance(d, dict) and re.fullmatch(r"\d+of\d+", str(d.get("wallet_type", ""))):
        m, n = map(int, d["wallet_type"].split("of"))
        keys, hint = [], None
        for i in range(1, n + 1):
            ks = d.get(f"x{i}/") or {}
            if not ks.get("xpub"):
                raise ValueError(f"Electrum file is missing x{i}/ xpub")
            hint = hint or parse_extkey(ks["xpub"])[2]
            keys.append((ks.get("root_fingerprint"), ks.get("derivation", ""), ks["xpub"]))
        return _build(m, {"wsh": "p2wsh", "sh-wsh": "p2sh-p2wsh"}.get(hint, "p2sh"), keys)
    # Sparrow / Specter / Nunchuk / Bitcoin Core listdescriptors: any descriptor string
    cands = [s for s in _find_desc_strings(d)]
    if cands:
        # prefer receive/multipath (not the internal /1/* one)
        cands.sort(key=lambda s: ("/1/*" in s and "/0/*" not in s))
        return cands[0]
    raise ValueError("No descriptor or key list found in that JSON export")


def _from_text(t: str):
    lines = [l.strip() for l in t.splitlines() if l.strip() and not l.strip().startswith("#")]
    for l in lines:  # BSMS (Nunchuk), Specter text, or any file with a descriptor line
        if _DESC_RE.match(l):
            return l
    # Coldcard / Keystone / Passport multisig config
    kv, keys, path = {}, [], ""
    for l in lines:
        if ":" not in l:
            continue
        k, v = (x.strip() for x in l.split(":", 1))
        if k.lower() == "derivation":
            path = v
        elif re.fullmatch(r"[0-9A-Fa-f]{8}", k):
            keys.append((k, path, v))
        else:
            kv[k.lower()] = v
    if not keys:
        raise ValueError("Not recognised. Paste the wallet's output descriptor")
    pol = re.match(r"(\d+)\s*(?:of|/)\s*(\d+)", kv.get("policy", ""))
    if not pol:
        raise ValueError("Multisig config has no 'Policy: M of N' line")
    if int(pol.group(2)) != len(keys):
        raise ValueError(f"Policy says {pol.group(2)} keys but {len(keys)} were found")
    fmt = kv.get("format")
    if not fmt:
        hint = parse_extkey(keys[0][2])[2]
        fmt = {"wsh": "p2wsh", "sh-wsh": "p2sh-p2wsh"}.get(hint, "p2sh")
    return _build(pol.group(1), fmt, keys)


def from_input(text: str) -> Descriptor:
    """Accept a descriptor, or a pasted wallet export (JSON or Coldcard/Electrum-style text)."""
    t = text.strip()
    if not t:
        raise ValueError("Empty")
    if t.startswith("{") or t.startswith("["):
        try:
            d = json.loads(t)
        except json.JSONDecodeError:
            d = None
        if d is not None:
            return Descriptor(_from_json(d))
    if _DESC_RE.match(t) and "\n" not in t.strip():
        return Descriptor(t)
    return Descriptor(_from_text(t))


def looks_like_descriptor_or_export(text: str) -> bool:
    t = text.strip()
    return bool(t) and (t[0] in "{[" or "(" in t or "\n" in t or ":" in t)
