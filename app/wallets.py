"""Cold-storage wallet records (1.0.2 schema) + migration from 1.0.0 / 1.0.1 settings.

A stored wallet looks like:
  {id, name, description, input: "xpub"|"descriptor", spec (KEY MATERIAL, never returned by the API),
   script, detected_type: "Multisig"|"Single-sig", policy: "2-of-3"|"single-sig", fingerprints: [...],
   script_type, type_override: None|"Multisig"|"Single-sig",
   script: "auto"|p2wpkh|p2sh-p2wpkh|p2pkh|p2tr (single-sig address type; "auto" = detect from on-chain history),
   detected_scripts: [...] (single-sig types found with history by the last scan, primary first)}
"""
import re, secrets
from . import descriptor, electrum

TYPES = ("Multisig", "Single-sig")
SCHEMA = 3   # 3 (1.0.8): single-sig xpub wallets get address-type auto-detection


def detect(spec: str, script: str = "auto") -> dict:
    """Validate key material and auto-detect the type label. Raises ValueError (generic, no key fragments)."""
    try:
        if re.search(r"[xyztuvYZUV]prv[1-9A-HJ-NP-Za-km-z]{20}", spec or ""):
            raise ValueError("private key")
        if electrum.is_descriptor(spec):
            d = descriptor.from_input(spec)
            d.derive(0, 0, 1)
            s = d.summary()
            return {"input": "descriptor", "spec": d.canonical(), "detected_type": "Multisig" if d.multisig else "Single-sig",
                    "policy": f"{d.m}-of-{len(d.keys)}" if d.multisig else "single-sig", "m": d.m, "n": len(d.keys),
                    "fingerprints": s["fingerprints"], "script_type": s["type"], "network": s["network"],
                    "checksum_verified": s["checksum_verified"], "change_chain_inferred": s["change_chain_inferred"]}
        k, stype, net = electrum.parse_xpub(spec, script)
        if k.is_private:
            raise ValueError("private key")
        return {"input": "xpub", "spec": spec.strip(), "detected_type": "Single-sig", "policy": "single-sig",
                "m": 1, "n": 1, "fingerprints": [], "script_type": stype if script not in ("", "auto", None) else "auto",
                "network": "testnet" if spec.strip()[0] in "tuv" else "mainnet",
                "checksum_verified": False, "change_chain_inferred": False}
    except Exception:
        raise ValueError("Could not read that xpub / descriptor / wallet export. Check it is complete and contains "
                         "only PUBLIC keys (xpub/ypub/zpub or a descriptor), then try again.") from None


def address_type(w: dict) -> str:
    """User setting for a single-sig xpub wallet: "auto" or one of electrum.SCRIPT_TYPES."""
    s = w.get("script") or "auto"
    return s if s in electrum.SCRIPT_TYPES else "auto"


def effective_script(w: dict) -> str:
    """Address type used to show addresses: the setting, else the primary detected type, else the prefix default."""
    s = address_type(w)
    if s != "auto":
        return s
    return (w.get("detected_scripts") or [None])[0] or electrum.default_script(w.get("spec", ""))


def first_receive(w: dict):
    try:
        if w.get("input") == "xpub":
            return electrum.derive(w["spec"], 0, 0, 1, effective_script(w))[0][1]
        if w.get("input") == "descriptor":
            return electrum.derive(w["spec"], 0, 0, 1)[0][1]
    except Exception:
        return None
    return None


def public_view(w: dict) -> dict:
    """Everything safe to show: never the spec. (The first receive address is shown so the user can check it against
    their wallet app; it is only returned to the UI, never logged.)"""
    t = w.get("type_override") or w.get("detected_type") or "Single-sig"
    v = {"id": w["id"], "name": w["name"], "description": w.get("description", ""), "type": t,
         "detected_type": w.get("detected_type"), "type_overridden": bool(w.get("type_override")),
         "policy": w.get("policy"), "input": w.get("input"), "script_type": w.get("script_type"),
         "fingerprints": w.get("fingerprints", []), "network": w.get("network"), "first_receive": first_receive(w)}
    if w.get("input") == "xpub":
        ds = [s for s in (w.get("detected_scripts") or []) if s in electrum.SCRIPT_TYPES]
        v.update(address_type=address_type(w), detected_scripts=ds, effective_script=effective_script(w),
                 script_type=(electrum.SCRIPT_LABELS[effective_script(w)] +
                              (" (auto-detected)" if address_type(w) == "auto" and ds else
                               " (auto: nothing found yet, default)" if address_type(w) == "auto" else " (set manually)")))
    return v


def check_script(s):
    s = (s or "auto").strip()
    if s != "auto" and s not in electrum.SCRIPT_TYPES:
        raise ValueError("Address type must be auto, p2wpkh, p2sh-p2wpkh, p2pkh or p2tr")
    return s


def make(name, description, spec, script="auto", type_override=None, existing=0) -> dict:
    info = detect(spec, script)
    w = {"id": secrets.token_hex(4), "name": clean(name, 60) or f"Wallet {existing + 1}",
         "description": clean(description, 200), "script": script or "auto",
         "type_override": check_type(type_override)}
    w.update(info)
    return w


def clean(s, n):
    return " ".join(str(s or "").split())[:n]


def check_type(t):
    if t in (None, "", "auto"):
        return None
    if t not in TYPES:
        raise ValueError("Type must be Multisig, Single-sig or auto")
    return t


def migrate(s: dict) -> tuple[list, bool]:
    """Return (wallets in schema 2, changed?). Handles 1.0.0 'xpub' setting and 1.0.1 {id,label,spec,script}."""
    out, changed = [], False
    for w in s.get("cold_wallets") or []:
        if w.get("schema") == SCHEMA:
            out.append(w); continue
        if w.get("schema") == 2:
            # 1.0.8: plain-xpub wallets were scanned as legacy p2pkh unless an address type was chosen.
            # Anything without an explicit choice becomes "auto" so its next scan finds the right type.
            changed = True
            w = dict(w, schema=SCHEMA)
            if w.get("input") == "xpub":
                if w.get("script") in (None, "", "auto"):
                    w["script"], w["script_type"], w["detected_scripts"] = "auto", "auto", []
            out.append(w); continue
        changed = True
        nw = {"id": w.get("id") or secrets.token_hex(4), "name": clean(w.get("label") or w.get("name"), 60) or "Cold storage",
              "description": "", "script": w.get("script", "auto"), "type_override": None, "schema": SCHEMA}
        try:
            nw.update(detect(w["spec"], nw["script"]))
        except ValueError:
            nw.update({"input": "unknown", "spec": w.get("spec", ""), "detected_type": "Single-sig", "policy": "invalid",
                       "fingerprints": []})
        out.append(nw)
    if s.get("xpub"):
        changed = True
        nw = {"id": "legacy", "name": "Cold storage", "description": "Migrated from 1.0.0", "type_override": None,
              "script": s.get("xpub_script", "auto"), "schema": SCHEMA}
        try:
            nw.update(detect(s["xpub"], nw["script"]))
            if not any(x.get("spec") == nw["spec"] for x in out):
                out.append(nw)
        except ValueError:
            pass
    for w in out:
        w["schema"] = SCHEMA
    return out, changed
