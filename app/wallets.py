"""Cold-storage wallet records (1.0.2 schema) + migration from 1.0.0 / 1.0.1 settings.

A stored wallet looks like:
  {id, name, description, input: "xpub"|"descriptor", spec (KEY MATERIAL, never returned by the API),
   script, detected_type: "Multisig"|"Single-sig", policy: "2-of-3"|"single-sig", fingerprints: [...],
   script_type, type_override: None|"Multisig"|"Single-sig"}
"""
import re, secrets
from . import descriptor, electrum

TYPES = ("Multisig", "Single-sig")
SCHEMA = 2


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
                "m": 1, "n": 1, "fingerprints": [], "script_type": stype,
                "network": "testnet" if spec.strip()[0] in "tuv" else "mainnet",
                "checksum_verified": False, "change_chain_inferred": False}
    except Exception:
        raise ValueError("Could not read that xpub / descriptor / wallet export. Check it is complete and contains "
                         "only PUBLIC keys (xpub/ypub/zpub or a descriptor), then try again.") from None


def public_view(w: dict) -> dict:
    """Everything safe to show: never the spec."""
    t = w.get("type_override") or w.get("detected_type") or "Single-sig"
    return {"id": w["id"], "name": w["name"], "description": w.get("description", ""), "type": t,
            "detected_type": w.get("detected_type"), "type_overridden": bool(w.get("type_override")),
            "policy": w.get("policy"), "input": w.get("input"), "script_type": w.get("script_type"),
            "fingerprints": w.get("fingerprints", []), "network": w.get("network")}


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
