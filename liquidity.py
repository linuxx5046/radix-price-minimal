#!/usr/bin/env python3
"""Measure every pool in tokens.json against a Gateway and optionally write a pruned tokens.json.

For each pool it reports: whether the price preview works, XRD/token, and approximate
liquidity (XRD held in the component's vaults plus any native pool_rdx1… referenced in
its state). TVL ≈ 2 × XRD (exact for 50/50 pools; for concentrated-liquidity pools it is
an upper bound, since it includes out-of-range liquidity). For pools without XRD
(e.g. TOKEN/DFP2) the quote side is valued at its own XRD price.

Usage:
  GATEWAY_URL=https://mainnet.radixdlt.com python3 liquidity.py
  GATEWAY_URL=... python3 liquidity.py --min-tvl-xrd 50000 --write tokens.pruned.json
  options: --keep-cg-only  (keep tokens with a coingecko_id even if they end up with no pools)
"""
import argparse
import json
import re
from decimal import Decimal

import minprice as mp

POOL_RE = re.compile(r"pool_rdx1[0-9a-z]{50,}")


def fungibles(addresses):
    """{address: ({resource: Decimal amount}, state_json_str)} using Global aggregation."""
    out = {}
    for i in range(0, len(addresses), 20):
        data = mp.gw("/state/entity/details", {"addresses": addresses[i:i + 20], "aggregation_level": "Global"})
        for it in data["items"]:
            amounts = {r["resource_address"]: Decimal(r["amount"])
                       for r in it.get("fungible_resources", {}).get("items", []) if "amount" in r}
            state = json.dumps(it.get("details", {}).get("state", {}))
            out[it["address"]] = (amounts, state)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokens", default=mp.TOKENS_FILE)
    ap.add_argument("--min-tvl-xrd", type=float, default=None)
    ap.add_argument("--write", default=None)
    ap.add_argument("--keep-cg-only", action="store_true")
    a = ap.parse_args()

    cfg = json.load(open(a.tokens))
    tokens = cfg["tokens"]
    epoch = mp.get_epoch()
    print(f"gateway={mp.GATEWAY_URL} epoch={epoch}\n")

    comps = [p["component"] for t in tokens.values() for p in t["pools"]]
    info = fungibles(comps)
    refs = sorted({r for c in comps if c in info for r in POOL_RE.findall(info[c][1])})
    if refs:
        info.update(fungibles(refs))

    res = mp.Resolver(tokens, epoch)
    addr = {s: t["resource_address"] for s, t in tokens.items()}
    rows = []
    for sym, t in tokens.items():
        for p in t["pools"]:
            c = p["component"]
            err = ""
            if p["dex"] == "defiplaza":
                try:  # PlazaPair.get_tokens() -> (base_address, quote_address)
                    f = mp.preview_call(c, "get_tokens", epoch)["fields"]
                    got = (f[0]["value"], f[1]["value"])
                    want = (addr.get(p["base"], p["base"]), addr.get(p["quote"], p["quote"]))
                    if got != want:
                        err = f"base/quote do not match get_tokens: {got}"
                except Exception as e:
                    err = f"get_tokens failed: {str(e)[:50]}"
            price = res.pool(p)
            if price is None and not err:
                err = "preview failed (see log)"
            sources = [s for s in dict.fromkeys([c] + (POOL_RE.findall(info[c][1]) if c in info else []))
                       if s in info]
            xrd = sum(info[s][0].get(mp.XRD, Decimal(0)) for s in sources)
            other = sum(info[s][0].get(t["resource_address"], Decimal(0)) for s in sources)
            if xrd == 0 and p["quote"] != "XRD" and p["base"] != "XRD":
                # pool without XRD (e.g. TOKEN/DFP2): value the quote side at its XRD price
                q_amt = sum(info[s][0].get(addr.get(p["quote"], ""), Decimal(0)) for s in sources)
                via = res.symbol(p["quote"])
                xrd = q_amt * via if via else Decimal(0)
            rows.append((sym, p, price, float(xrd) * 2, float(other), err))

    print(f"{'token':9} {'dex':8} {'blueprint':14} {'XRD/token':>14} {'TVL~XRD':>14} {'token_amt':>16}  component")
    for sym, p, price, tvl, other, err in sorted(rows, key=lambda r: (r[0].lower(), -r[3])):
        ps = f"{float(price):.8g}" if price is not None else "FAIL"
        print(f"{sym:9} {p['dex']:8} {p.get('blueprint_name', ''):14} {ps:>14} {tvl:>14,.0f} {other:>16,.2f}  "
              f"{p['component']}{'  ' + err if err else ''}")

    if a.min_tvl_xrd is None:
        return
    keep = {}
    for sym, t in tokens.items():
        pools = [p for s, p, price, tvl, _, _ in rows
                 if s == sym and price is not None and tvl >= a.min_tvl_xrd]
        if sym == "XRD" or pools or (a.keep_cg_only and t.get("coingecko_id")):
            keep[sym] = {**t, "pools": pools}
    anchors = [x for x in cfg["usd_anchors"] if keep.get(x, {}).get("pools")]
    dropped = sorted(set(tokens) - set(keep))
    print(f"\nthreshold {a.min_tvl_xrd:,.0f} XRD → keeping {len(keep)} tokens, "
          f"{sum(len(t['pools']) for t in keep.values())} pools")
    print("removed:", ", ".join(dropped) or "-")
    if not anchors:
        print("ERROR: no USD anchor keeps any pool; adjust usd_anchors or the threshold. Not writing.")
        return
    if a.write:
        json.dump({**cfg, "usd_anchors": anchors, "tokens": keep}, open(a.write, "w"), indent=2)
        print(f"written {a.write}")


if __name__ == "__main__":
    main()
