"""Descriptor -> script vectors from public sources:
- Bitcoin Core src/test/descriptor_tests.cpp (sh(multi) with key origin + checksum, sortedmulti, multipath)
- BIP-380 checksum test vectors, BIP-67 sorted P2SH multisig vectors, BIP-389 multipath semantics.
All keys are public test keys from those files, not real wallets."""
import asyncio, hashlib, json
import pytest
from embit import base58
from embit.hashes import hash160
from app import descriptor as D
from app.descriptor import Descriptor, from_input, checksum, split_checksum
from app.electrum import scan, derive

A = "xpub6ERApfZwUNrhLCkDtcHTcxd75RbzS1ed54G1LkBUHQVHQKqhMkhgbmJbZRkrgZw4koxb5JaHWkY4ALHY2grBGRjaDMzQLcgJvLJuZZvRcEL"
B = "xpub68NZiKmJWnxxS6aaHmn81bvJeTESw724CRDs6HbuccFQN9Ku14VQrADWgqbhhTHBaohPX4CjNLf9fq9MYo6oDaPPLPxSb7gwQN3ih19Zm4Y"
C = "xpub661MyMwAqRbcGDZQUKLqmWodYLcoBQnQH33yYkkF3jjxeLvY8qr2wWGEWkiKFaaQfJCoi3HeEq3Dc5DptfbCyjD38fNhSqtKc1UHaP4ba3t"

# Core: sortedmulti(2, A/*, B/0/0/*) witness/redeem scripts for index 0,1,2
SORTED_SCRIPTS = [
    "5221025d5fc65ebb8d44a5274b53bac21ff8307fec2334a32df05553459f8b1f7fe1b62102fbd47cc8034098f0e6a94c6aeee8528abf0a2153a5d8e46d325b7284c046784652ae",
    "52210264fd4d1f5dea8ded94c61e9641309349b62f27fbffe807291f664e286bfbe6472103f4ece6dfccfa37b211eb3d0af4d0c61dba9ef698622dc17eecdf764beeb005a652ae",
    "5221022ccabda84c30bad578b13c89eb3b9544ce149787e5b538175b1d1ba259cbb83321024d902e1a2fc7a8755ab5b694c575fce742c48d9ff192e63df5193e4c7afe1f9c52ae",
]


def p2wsh(ws_hex):
    return "0020" + hashlib.sha256(bytes.fromhex(ws_hex)).hexdigest()


def p2sh(rs_hex):
    return "a914" + hash160(bytes.fromhex(rs_hex)).hex() + "87"


def as_slip132(x, ver_hex):
    return base58.encode_check(bytes.fromhex(ver_hex) + base58.decode_check(x)[4:])


# ---------- BIP-380 checksum ----------
def test_bip380_checksum_vectors():
    assert checksum("raw(deadbeef)") == "89f8spxm"
    assert split_checksum("raw(deadbeef)#89f8spxm") == ("raw(deadbeef)", True)
    for bad in ["raw(deadbeef)#", "raw(deadbeef)#89f8spxmx", "raw(deadbeef)#89f8spx",
                "raw(deedbeef)#89f8spxm", "raw(deedbeef)##9f8spxm", "raw(Ü)#00000000"]:
        with pytest.raises(ValueError):
            split_checksum(bad)


def test_core_sh_multi_with_origin_and_checksum():
    exp = "a91445a9a622a8b0a1269944be477640eedc447bbd8487"
    for d in [f"sh(multi(2,[00000000/111'/222]{A},{B}/0))#tjg09x5t",
              f"sh(multi(2,[00000000/111h/222]{A},{B}/0))#hgmsckna",
              f"sh(multi(2,[00000000/111'/222]{A},{B}/0))"]:
        assert Descriptor(d).script_pubkey(0, 0).data.hex() == exp
    with pytest.raises(ValueError, match="checksum"):
        Descriptor(f"sh(multi(2,[00000000/111'/222]{A},{B}/0))#tjg09x5q")
    with pytest.raises(ValueError):
        Descriptor(f"sh(multi(2,[00000000/111'/222]{A},{B}/0))#")


# ---------- sortedmulti in each wrapper (BIP-67 ordering) ----------
@pytest.mark.parametrize("wrap,fn", [("wsh({})", p2wsh), ("sh(wsh({}))", lambda s: p2sh(p2wsh(s))), ("sh({})", p2sh)])
def test_core_sortedmulti_wrapped(wrap, fn):
    for keys in (f"{A}/*,{B}/0/0/*", f"{B}/0/0/*,{A}/*"):  # key order must not matter
        d = Descriptor(wrap.format(f"sortedmulti(2,{keys})"))
        for i, ws in enumerate(SORTED_SCRIPTS):
            assert d.script_pubkey(0, i).data.hex() == fn(ws)


def test_unsorted_multi_respects_order():
    d1 = Descriptor(f"wsh(multi(2,{A}/*,{B}/0/0/*))")
    d2 = Descriptor(f"wsh(multi(2,{B}/0/0/*,{A}/*))")
    assert d1.script_pubkey(0, 0).data != d2.script_pubkey(0, 0).data
    assert {d1.script_pubkey(0, 0).data.hex(), d2.script_pubkey(0, 0).data.hex()} >= {p2wsh(SORTED_SCRIPTS[0])}


def test_bip67_vectors():
    v1 = Descriptor("sh(sortedmulti(2,02ff12471208c14bd580709cb2358d98975247d8765f92bc25eab3b2763ed605f8,"
                    "02fe6f0a5a297eb38c391581c4413e084773ea23954d93f7753db7dc0adc188b2f))")
    assert v1.derive(0, 0, 1)[0][1] == "39bgKC7RFbpoCRbtD5KEdkYKtNyhpsNa3Z"
    v2 = Descriptor("sh(sortedmulti(2,02632b12f4ac5b1d1b72b2a3b508c19172de44f6f46bcee50ba33f3f9291e47ed0,"
                    "027735a29bae7780a9755fae7a1c4374c656ac6a69ea9f3697fda61bb99a4f3e77,"
                    "02e2cc6bd5f45edd43bebe7cb9b675f0ce9ed3efe613b177588290ad188d11b404))")
    assert v2.derive(0, 0, 1)[0][1] == "3CKHTjBKxCARLzwABMu9yD85kvtm7WnMfH"


# ---------- multipath (BIP-389), Core CheckMultipath 2-of-3 sh(multi) ----------
MP = f"sh(multi(2,{A}/<1;2;3>/0/*,{B}/0/*,{C}/0/0/<3;4;5>/*))"
MP_EXP = [
    ["a914689cdf7de5836ec04fb971d128cc84858f73e11487", "a9142ea7dbaf0a77ee19f080cdacb3e13560e3cd9cf587", "a9143da854021f58f5e2d3ff6bb4fcd0ced877deb34987"],
    ["a9143dd613d162e89b83369bbf08e5f1977cfdc9b02787", "a91449eef5d3df5c465b20a630c66058fe689082d8e187", "a91492be56babf54ea2109c577f799ba6d73948e8c3287"],
    ["a9140093ca92097bdf557fbb0570bb77e1efd2e7529c87", "a914e4d0419d3d2ce8f921a800796811ff5462bb151887", "a914997bf69841ac444190dc02f5e6031dd6f8feab4587"],
]


def test_core_multipath_2of3():
    d = Descriptor(MP)
    assert d.n_paths == 3 and d.chains() == [0, 1]  # first two paths = receive, change
    for branch, exp in enumerate(MP_EXP):
        assert [d.script_pubkey(branch, i).data.hex() for i in range(3)] == exp
    with pytest.raises(ValueError, match="mismatched"):
        Descriptor(f"sh(multi(2,{A}/<1;2;3>/0/*,{B}/0/*,{C}/0/0/<3;4>/*))")


def test_multipath_equals_separate_chains_and_auto_change():
    mp = Descriptor(f"wsh(sortedmulti(2,[aaaaaaaa/48h/0h/0h/2h]{A}/<0;1>/*,[bbbbbbbb/48h/0h/0h/2h]{B}/<0;1>/*,{C}/<0;1>/*))")
    rcv = Descriptor(f"wsh(sortedmulti(2,{A}/0/*,{B}/0/*,{C}/0/*))")  # Sparrow-style /0/*: change inferred
    assert rcv.summary()["change_chain_inferred"]
    for chain in (0, 1):
        assert mp.derive(chain, 0, 5) == rcv.derive(chain, 0, 5)
    chg = Descriptor(f"wsh(sortedmulti(2,{A}/1/*,{B}/1/*,{C}/1/*))")
    assert mp.derive(1, 0, 3)[0][1] == chg.derive(0, 0, 1)[0][1]
    assert mp.derive(0, 0, 1)[0][1].startswith("bc1q") and len(mp.derive(0, 0, 1)[0][1]) == 62


# ---------- SLIP-132 keys, canonical form, exports ----------
def test_zpub_vpub_style_keys_and_canonical():
    zA, zB, zC = (as_slip132(k, "02aa7ed3") for k in (A, B, C))  # Zpub
    d1 = Descriptor(f"wsh(sortedmulti(2,{A}/<0;1>/*,{B}/<0;1>/*,{C}/<0;1>/*))")
    d2 = Descriptor(f"wsh(sortedmulti(2,{zA}/<0;1>/*,{zB}/<0;1>/*,{zC}/<0;1>/*))")
    assert zA.startswith("Zpub") and d1.derive(0, 0, 3) == d2.derive(0, 0, 3)
    canon = d2.canonical()
    assert canon.startswith("wsh(sortedmulti(2,xpub") and Descriptor(canon).had_checksum
    assert Descriptor(canon).derive(1, 0, 3) == d1.derive(1, 0, 3)
    tA, tB = (as_slip132(k, "02575483") for k in (A, B))  # Vpub (testnet P2WSH)
    t = Descriptor(f"wsh(sortedmulti(1,{tA}/0/*,{tB}/0/*))")
    assert t.net == "test" and t.derive(0, 0, 1)[0][1].startswith("tb1q")


REF = Descriptor(f"wsh(sortedmulti(2,[aaaaaaaa/48h/0h/0h/2h]{A}/<0;1>/*,[bbbbbbbb/48h/0h/0h/2h]{B}/<0;1>/*,[cccccccc/48h/0h/0h/2h]{C}/<0;1>/*))")


def test_exports_caravan_coldcard_electrum_sparrow():
    keys = [("aaaaaaaa", A), ("bbbbbbbb", B), ("cccccccc", C)]
    caravan = {"name": "vault", "addressType": "P2WSH", "network": "mainnet",
               "quorum": {"requiredSigners": 2, "totalSigners": 3},
               "extendedPublicKeys": [{"name": f"k{i}", "bip32Path": "m/48'/0'/0'/2'", "xpub": x, "xfp": f}
                                      for i, (f, x) in enumerate(keys)]}
    coldcard = "# Coldcard Multisig setup file\nName: vault\nPolicy: 2 of 3\nDerivation: m/48'/0'/0'/2'\nFormat: P2WSH\n\n" + \
               "\n".join(f"{f.upper()}: {x}" for f, x in keys)
    electrum = {"wallet_type": "2of3", **{f"x{i + 1}/": {"type": "bip32", "xpub": as_slip132(x, "02aa7ed3"),
                                                         "root_fingerprint": f, "derivation": "m/48h/0h/0h/2h"}
                                         for i, (f, x) in enumerate(keys)}}
    sparrow = {"label": "vault", "descriptor": REF.canonical()}
    bsms = "BSMS 1.0\n" + REF.canonical() + "\n/0/*,/1/*\n" + REF.derive(0, 0, 1)[0][1]
    for t in (json.dumps(caravan), coldcard, json.dumps(electrum), json.dumps(sparrow), bsms, REF.canonical()):
        d = from_input(t)
        assert d.derive(0, 0, 3) == REF.derive(0, 0, 3) and d.derive(1, 0, 2) == REF.derive(1, 0, 2)
        assert d.summary()["policy"] == "2-of-3 sortedmulti" and d.summary()["fingerprints"] == ["aaaaaaaa", "bbbbbbbb", "cccccccc"]
    nested = from_input(coldcard.replace("Format: P2WSH", "Format: P2SH-P2WSH"))
    assert nested.wrap == "sh-wsh" and nested.derive(0, 0, 1)[0][1].startswith("3")


def test_rejects_private_and_hardened_and_garbage():
    xprv = "xprvA1RpRA33e1JQ7ifknakTFpgNXPmW2YvmhqLQYMmrj4xJXXWYpDPS3xz7iAxn8L39njGVyuoseXzU6rcxFLJ8HFsTjSyQbLYnMpCqE2VbFWc"
    with pytest.raises(ValueError, match="PRIVATE"):
        Descriptor(f"wsh(sortedmulti(1,{xprv}/0/*))")
    with pytest.raises(ValueError):
        Descriptor(f"wsh(sortedmulti(1,{A}/0h/*))")
    with pytest.raises(ValueError):
        Descriptor(f"wsh(sortedmulti(3,{A}/0/*,{B}/0/*))")
    with pytest.raises(ValueError):
        from_input("hello world: nothing here")


def test_scan_multisig_descriptor_via_fake_electrum():
    desc = REF.canonical()
    funded = {derive(desc, 0, 2, 1)[0][2]: 70000, derive(desc, 1, 0, 1)[0][2]: 3000}
    seen = []

    async def handle(r, w):
        while line := await r.readline():
            m = json.loads(line); p = m["params"]
            seen.append(json.dumps(p))
            res = ["fake", "1.4"] if m["method"] == "server.version" else \
                ([{"tx_hash": "00", "height": 1}] if p[0] in funded else []) if m["method"].endswith("get_history") else \
                {"confirmed": funded.get(p[0], 0), "unconfirmed": 0}
            w.write((json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}) + "\n").encode()); await w.drain()

    async def main():
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            return await asyncio.wait_for(scan(desc, "127.0.0.1", srv.sockets[0].getsockname()[1], gap=20), 30)
        finally:
            srv.close()

    r = asyncio.run(main())
    assert round(r["balance"], 8) == 0.00073 and {a["path"] for a in r["used_addresses"]} == {"0/2", "1/0"}
    # only script hashes go to the server, never keys/descriptors
    assert not any("pub" in s or "sortedmulti" in s for s in seen)
