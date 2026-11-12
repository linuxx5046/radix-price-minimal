"""Tests con Gateway y CoinGecko simulados. Ejecutar: python3 -m unittest -v test_minprice"""
import json
import os
import re
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlreq
from urllib.error import HTTPError

XRD = "resource_rdx1tknxxxxxxxxxradxrdxxxxxxxxx009923554798xxxxxxxxxradxrd"
LSU = "resource_rdx1lsu_test"
VALIDATOR = "validator_rdx1test"

# componente -> (método, programmatic_json de salida | None = preview fallido)
PREVIEWS = {
    "component_husdc_c9": ("get_price", {"kind": "Enum", "fields": [{"kind": "Decimal", "value": "0.02"}]}),
    "component_husdc_oci": ("price_sqrt", {"kind": "PreciseDecimal", "value": "0.1414213562373095048801688724"}),
    "component_foo_oci": ("price_sqrt", {"kind": "Enum", "fields": [{"kind": "PreciseDecimal", "value": "10"}]}),
    "component_heth_c9": ("get_price", {"kind": "Enum", "fields": [{"kind": "Decimal", "value": "150000"}]}),
    "component_bar_oci": ("price_sqrt", None),
    "component_dfp2_oci": ("price_sqrt", {"kind": "PreciseDecimal", "value": "0.2"}),  # 0.04 XRD/DFP2
}
# DefiPlaza: componente -> (base_addr, quote_addr, p0 = DFP2 por base, spread)
PLAZA = {
    "component_plaza_xrd": (XRD, "resource_dfp2", Decimal("25"), Decimal("0.01")),
    "component_plaza_zap": ("resource_zap", "resource_dfp2", Decimal("50"), Decimal("0.02")),
}


def plaza_out(comp, method, args):
    base, quote, p0, spread = PLAZA[comp]
    dec = lambda v: {"kind": "Decimal", "value": str(v)}
    if method == "get_state":
        return {"kind": "Tuple", "type_name": "PairState", "fields": [
            dict(dec(p0), field_name="p0"),
            {"kind": "Enum", "field_name": "shortage", "variant_id": "1", "fields": []},
            dict(dec(1), field_name="target_ratio")]}
    if method == "get_tokens":
        return {"kind": "Tuple", "fields": [{"kind": "Reference", "value": base},
                                            {"kind": "Reference", "value": quote}]}
    amount = Decimal(re.search(r'Decimal\("([^"]+)"\)', args).group(1))
    is_quote = "true" in args
    gross = amount / (p0 * (1 + spread)) if is_quote else amount * p0 * (1 - spread)
    fee = gross * Decimal("0.003")
    return {"kind": "Tuple", "fields": [dec(gross - fee), dec(0), dec(fee),
                                        {"kind": "Tuple", "fields": []}, {"kind": "Tuple", "fields": []}]}
CG = {"radix": {"usd": 0.02}, "ethereum": {"usd": 3000.0}, "usd-coin": {"usd": 1.0}}
FUNGIBLE = {
    "component_husdc_c9": {XRD: "500000"},
    "component_husdc_oci": {},
    "pool_rdx1" + "a" * 54: {XRD: "250000"},
    "component_foo_oci": {XRD: "1000"},
    "component_heth_c9": {XRD: "90000"},
    "component_bar_oci": {},
    "component_dfp2_oci": {XRD: "40000", "resource_dfp2": "1000000"},
    "component_plaza_xrd": {},
    "component_plaza_zap": {},
    "pool_rdx1" + "b" * 54: {"resource_zap": "1000", "resource_dfp2": "10000"},
    "pool_rdx1" + "c" * 54: {"resource_dfp2": "40000"},
}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/cg/simple/price"):
            ids = re.search(r"ids=([^&]+)", self.path).group(1).replace("%2C", ",").split(",")
            return self._send({i: CG[i] for i in ids if i in CG})
        self._send({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/status/gateway-status":
            return self._send({"ledger_state": {"epoch": 1000}})
        if self.path == "/transaction/preview":
            outs = []
            for comp, method, args in re.findall(r'Address\("([^"]+)"\)\n"([^"]+)"\n(.*?);', body["manifest"], re.S):
                if comp in PLAZA:
                    outs.append({"programmatic_json": plaza_out(comp, method, args)})
                    continue
                m, out = PREVIEWS[comp]
                assert m == method, (comp, method)
                if out is None:
                    return self._send({"receipt": {"status": "Failed", "error_message": "boom"}})
                outs.append({"programmatic_json": out})
            return self._send({"receipt": {"status": "Succeeded", "output": outs}})
        if self.path == "/state/entity/details":
            if body["aggregation_level"] == "Global":
                items = []
                for a in body["addresses"]:
                    st = {"pool": "pool_rdx1" + "a" * 54} if a == "component_husdc_oci" else \
                        {"base_pool": "pool_rdx1" + "b" * 54, "quote_pool": "pool_rdx1" + "c" * 54} \
                        if a == "component_plaza_zap" else {}
                    items.append({"address": a, "details": {"state": st}, "fungible_resources": {"items": [
                        {"resource_address": r, "amount": v} for r, v in FUNGIBLE.get(a, {}).items()]}})
                return self._send({"items": items})
            if body["addresses"] == [VALIDATOR]:
                return self._send({"items": [{
                    "address": VALIDATOR,
                    "details": {"state": {"stake_unit_resource_address": LSU,
                                          "stake_xrd_vault": {"entity_address": "internal_vault_stake"}}},
                    "fungible_resources": {"items": [{"resource_address": XRD, "vaults": {"items": [
                        {"vault_address": "internal_vault_other", "amount": "5"},
                        {"vault_address": "internal_vault_stake", "amount": "1100"}]}}]}}]})
            items = []
            for a in body["addresses"]:
                md = [{"key": "validator", "value": {"typed": {"type": "GlobalAddress", "value": VALIDATOR}}}] \
                    if a == LSU else []
                items.append({"address": a, "metadata": {"items": md}, "details": {"total_supply": "1000"}})
            return self._send({"items": items})
        self._send({}, 404)


TOKENS = {
    "usd_anchors": ["hUSDC"],
    "tokens": {
        "XRD": {"resource_address": XRD, "coingecko_id": "radix", "pools": []},
        "hUSDC": {"resource_address": "resource_husdc", "coingecko_id": "usd-coin", "pools": [
            {"dex": "c9", "component": "component_husdc_c9", "base": "hUSDC", "quote": "XRD"},
            {"dex": "ociswap", "component": "component_husdc_oci", "base": "hUSDC", "quote": "XRD"}]},
        "FOO": {"resource_address": "resource_foo", "pools": [
            {"dex": "ociswap", "component": "component_foo_oci", "base": "XRD", "quote": "FOO"}]},
        "hETH": {"resource_address": "resource_heth", "coingecko_id": "ethereum", "pools": [
            {"dex": "c9", "component": "component_heth_c9", "base": "XRD", "quote": "hETH"}]},
        "DFP2": {"resource_address": "resource_dfp2", "pools": [
            {"dex": "ociswap", "component": "component_dfp2_oci", "base": "XRD", "quote": "DFP2"},
            {"dex": "defiplaza", "component": "component_plaza_xrd", "base": "XRD", "quote": "DFP2"}]},
        "ZAP": {"resource_address": "resource_zap", "pools": [
            {"dex": "defiplaza", "component": "component_plaza_zap", "base": "ZAP", "quote": "DFP2"}]},
        "BAR": {"resource_address": "resource_bar", "pools": [
            {"dex": "ociswap", "component": "component_bar_oci", "base": "XRD", "quote": "BAR"}]},
    },
}


class MinpriceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.mock.serve_forever, daemon=True).start()
        mport = cls.mock.server_address[1]
        cls.tmp = tempfile.mkdtemp()
        tf = os.path.join(cls.tmp, "tokens.json")
        with open(tf, "w") as f:
            json.dump(TOKENS, f)
        os.environ.update({
            "GATEWAY_URL": f"http://127.0.0.1:{mport}", "COINGECKO_URL": f"http://127.0.0.1:{mport}/cg",
            "TOKENS_FILE": tf, "DB_PATH": os.path.join(cls.tmp, "t.db"), "API_KEY": "k",
            "HISTORY_EVERY_SECS": "0", "LOG_LEVEL": "WARNING"})
        import minprice
        cls.mp = minprice
        cls.cfg = minprice.TokenConfig(tf)
        cls.store = minprice.Store(os.environ["DB_PATH"])
        cls.pricer = minprice.Pricer(cls.cfg, cls.store)
        cls.pricer.cycle()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), minprice.make_handler(cls.store, cls.pricer, cls.cfg))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    def post(self, path, body, headers=None):
        req = urlreq.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                             headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlreq.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def test_prices(self):
        latest = self.store.latest
        self.assertAlmostEqual(latest[XRD][0], 0.02, places=9)          # 0.8*0.02 + 0.2*0.02
        self.assertEqual(latest[XRD][2], "coingecko_weighted")
        self.assertAlmostEqual(latest["resource_foo"][0], 2.0, places=6)  # 100 XRD * 0.02
        self.assertAlmostEqual(latest["resource_heth"][0], 3000.0, places=4)
        self.assertNotIn("resource_bar", latest)                        # pool fallido -> omitido
        # DFP2: media de ociswap (0.04 XRD) y plaza XRD/DFP2 (1/25 = 0.04 XRD, mid ~ sin spread)
        self.assertAlmostEqual(latest["resource_dfp2"][0], 0.04 * 0.02, delta=1e-7)
        # ZAP vía DefiPlaza: 50 DFP2 * 0.04 XRD/DFP2 * $0.02 = $0.04 (spread 2% -> mid ~ -0.02%)
        self.assertAlmostEqual(latest["resource_zap"][0], 0.04, delta=0.04 * 0.001)

    def test_tokens_endpoint(self):
        code, r = self.post("/price/tokens", {"currency": "USD", "tokens": [XRD, "resource_foo", "nope"],
                                               "lsus": [LSU, "resource_foo"]})
        self.assertEqual(code, 200)
        self.assertEqual({t["resource_address"] for t in r["tokens"]}, {XRD, "resource_foo"})
        self.assertEqual(len(r["lsus"]), 1)
        self.assertAlmostEqual(r["lsus"][0]["xrd_redemption_value"], 1.1)
        self.assertAlmostEqual(r["lsus"][0]["usd_price"], 0.022, places=9)

    def test_currency_and_validation(self):
        self.assertEqual(self.post("/price/tokens", {"currency": "EUR", "tokens": []})[0], 400)
        self.assertEqual(self.post("/price/tokens", {"tokens": []})[0], 422)
        self.assertEqual(self.post("/price/tokens", {"currency": "usd", "tokens": "x"})[0], 422)

    def test_historical(self):
        now = int(time.time())
        self.assertEqual(self.post("/price/historicalPrice", {"tokens": [XRD], "timestamp": now})[0], 401)
        code, r = self.post("/price/historicalPrice", {"tokens": [XRD], "timestamp": now}, {"x-api-key": "k"})
        self.assertEqual(code, 200)
        self.assertAlmostEqual(r["prices"][XRD]["usd_price"], 0.02, places=9)
        code, _ = self.post("/price/historicalPrice", {"tokens": [XRD], "timestamp": now - 10 * 86400},
                            {"x-api-key": "k"})
        self.assertEqual(code, 424)

    def test_token_list_and_health(self):
        code, r = self.post("/tokens", {})
        self.assertEqual(code, 200)
        self.assertEqual({t["symbol"] for t in r}, {"XRD", "hUSDC", "FOO", "hETH", "DFP2", "ZAP"})
        with urlreq.urlopen(self.base + "/healthz") as h:
            self.assertEqual(json.loads(h.read())["Everything"], "Ok")

    def test_divergence_uses_cache_then_ledger(self):
        p = self.mp.Pricer(self.cfg, self.store)
        self.assertEqual(p.resolve_cg("X", "radix", {"radix": 1.0}, 0.02, time.time()), (None, None))
        p.cg_cache["radix"] = (0.021, time.time())
        self.assertEqual(p.resolve_cg("X", "radix", {"radix": 1.0}, 0.02, time.time()),
                         (0.021, "coingecko_cached"))

    def test_restart_loads_latest(self):
        s2 = self.mp.Store(os.environ["DB_PATH"])
        self.assertIn(XRD, s2.latest)


if __name__ == "__main__":
    unittest.main()
