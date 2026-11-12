#!/usr/bin/env python3
"""minprice: minimal Radix token price service.

Single process, stdlib only. Compatible with the endpoints of
radixdlt/token-pricing-service (consumers/token-price-service):
  POST /price/tokens            {"currency":"USD","tokens":[...],"lsus":[...]}
  POST /price/tokens-legacy     (alias of /price/tokens)
  POST /price/historicalPrice   {"tokens":[...],"timestamp":unix}  header x-api-key
  POST /price/historicalPrice-legacy (alias)
  POST /tokens                  configured tokens with their price
  GET  /healthz

Pricing algorithm = the one from the ledger_current_price DAG:
  USD/XRD from stablecoin anchor pools, ledger price = average over pools,
  and for tokens with a coingecko_id: weight*CG + (1-weight)*ledger, with a
  divergence check and an expiring CoinGecko cache.
"""
import json
import logging
import os
import signal
import sqlite3
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse as urlparse
from urllib import request as urlreq

env = os.environ.get
GATEWAY_URL = env("GATEWAY_URL", "https://mainnet.radixdlt.com").rstrip("/")
COINGECKO_URL = env("COINGECKO_URL", "https://api.coingecko.com/api/v3").rstrip("/")  # "" disables CoinGecko
TOKENS_FILE = env("TOKENS_FILE", "tokens.json")
DB_PATH = env("DB_PATH", "minprice.db")
HOST = env("HOST", "127.0.0.1")
PORT = int(env("PORT", "8000"))
API_KEY = env("API_KEY", "")  # empty = /historicalPrice disabled
INTERVAL_SECS = int(env("INTERVAL_SECS", "60"))
HISTORY_EVERY_SECS = int(env("HISTORY_EVERY_SECS", "300"))
RETENTION_DAYS = int(env("RETENTION_DAYS", "400"))
CG_DEFAULT_WEIGHT = float(env("COINGECKO_DEFAULT_WEIGHT", "0.8"))
CG_DIVERGENCE = float(env("COINGECKO_DIVERGENCE_THRESHOLD", "0.5"))
CG_STALENESS_SECS = int(env("COINGECKO_STALENESS_THRESHOLD_SECS", "300"))
LSU_CACHE_SECS = int(env("LSU_CACHE_SECS", "300"))
HTTP_TIMEOUT = float(env("HTTP_TIMEOUT", "10"))
MAX_ADDRESSES = int(env("MAX_ADDRESSES", "500"))

XRD = "resource_rdx1tknxxxxxxxxxradxrdxxxxxxxxx009923554798xxxxxxxxxradxrd"
PREVIEW_FLAGS = {
    "use_free_credit": True,
    "assume_all_signature_proofs": True,
    "skip_epoch_check": True,
    "disable_auth_checks": False,
}
SIGNER_KEYS = [{
    "key_type": "EcdsaSecp256k1",
    "key_hex": "0388207bf34f38fe057884e1efba2f54d1bfb3daf59894db12b1709315cfc77617",
}]

logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("minprice")


# ---------------------------------------------------------------- HTTP client
def http_json(url, body=None, timeout=HTTP_TIMEOUT):
    data = None if body is None else json.dumps(body).encode()
    req = urlreq.Request(
        url, data=data, method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "minprice/1.0"},
    )
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def gw(path, body):
    return http_json(GATEWAY_URL + path, body)


def get_epoch():
    return gw("/status/gateway-status", {})["ledger_state"]["epoch"]


def preview(manifest, epoch):
    """Run a manifest in preview; return the list of programmatic_json (one per instruction)."""
    data = gw("/transaction/preview", {
        "manifest": manifest,
        "start_epoch_inclusive": epoch,
        "end_epoch_exclusive": epoch + 2,
        "tip_percentage": 0,
        "nonce": 1,
        "signer_public_keys": SIGNER_KEYS,
        "flags": PREVIEW_FLAGS,
    })
    rc = data["receipt"]
    if rc.get("status") not in (None, "Succeeded"):
        raise RuntimeError(f"preview {rc.get('status')}: {str(rc.get('error_message', ''))[:160]}")
    return [o["programmatic_json"] for o in rc["output"]]


def preview_call(component, method, epoch):
    return preview(f'CALL_METHOD\nAddress("{component}")\n"{method}"\n;', epoch)[0]


def _dec(x):
    """Decimal -> manifest literal (no scientific notation, max 18 decimals)."""
    return format(x.quantize(Decimal(1).scaleb(-18)).normalize(), "f")


def _field(obj, name, idx):
    for f in obj.get("fields", []):
        if f.get("field_name") == name:
            return f
    return obj["fields"][idx]


PLAZA_PROBE_QUOTE = Decimal(env("PLAZA_PROBE_QUOTE", "100"))  # probe size in quote-token units (e.g. DFP2)


def plaza_quote_per_base(component, epoch):
    """DefiPlaza PlazaPair: mid price in quote units per unit of base.

    get_state().p0 (B[Q]) is only used to size the probe; the price comes from quote()
    in both directions (bid = selling base, ask = buying base), fee excluded, geometric mean.
    """
    p0 = Decimal(_field(preview_call(component, "get_state", epoch), "p0", 0)["value"])
    if p0 <= 0:
        raise ValueError("p0 <= 0")
    q_in = PLAZA_PROBE_QUOTE
    b_in = q_in / p0
    sell, buy = preview(
        f'CALL_METHOD\nAddress("{component}")\n"quote"\nDecimal("{_dec(b_in)}")\nfalse\n;\n'
        f'CALL_METHOD\nAddress("{component}")\n"quote"\nDecimal("{_dec(q_in)}")\ntrue\n;', epoch)

    def gross(o):  # (output - fee, remainder, fee, allocation, state)
        f = o["fields"]
        return Decimal(f[0]["value"]) + Decimal(f[2]["value"]), Decimal(f[1]["value"])

    q_out, b_rem = gross(sell)
    b_out, q_rem = gross(buy)
    if q_out <= 0 or b_out <= 0 or b_rem >= b_in or q_rem >= q_in:
        raise ValueError("no liquidity on one side")
    bid = q_out / (b_in - b_rem)        # quote received per base sold
    ask = (q_in - q_rem) / b_out        # quote paid per base bought
    return (bid * ask).sqrt()


def pool_raw(pool, epoch):
    """Raw price using the original repo convention: units of `base` per unit of `quote`."""
    if pool["dex"] == "ociswap":
        out = preview_call(pool["component"], "price_sqrt", epoch)
        s = out["fields"][0]["value"] if "fields" in out else out["value"]
        raw = Decimal(s) ** 2
    elif pool["dex"] == "c9":
        out = preview_call(pool["component"], "get_price", epoch)
        raw = Decimal(out["fields"][0]["value"])
    elif pool["dex"] == "defiplaza":
        raw = Decimal(1) / plaza_quote_per_base(pool["component"], epoch)
    else:
        raise ValueError(f"unsupported dex: {pool['dex']}")
    if raw <= 0:
        raise ValueError("price <= 0")
    return raw


class Resolver:
    """XRD prices per pool and per symbol within one cycle (cached, with a recursion guard).

    If neither base nor quote is XRD (e.g. a DefiPlaza TOKEN/DFP2 pair), the base price in
    quote units is converted to XRD using the quote symbol's own XRD price.
    """

    def __init__(self, tokens, epoch):
        self.tokens, self.epoch = tokens, epoch
        self.pools, self.syms, self._busy = {}, {}, set()

    def pool(self, p):
        k = p["component"]
        if k in self.pools:
            return self.pools[k]
        try:
            raw = pool_raw(p, self.epoch)
            if p["base"] == "XRD":
                v = raw                                  # XRD per quote token
            elif p["quote"] == "XRD":
                v = Decimal(1) / raw                     # XRD per base token
            else:
                via = self.symbol(p["quote"])
                if via is None:
                    raise ValueError(f"no XRD price for {p['quote']}")
                v = Decimal(1) / raw * via               # (quote per base) * (XRD per quote)
        except Exception as e:
            log.warning(f"pool {p['dex']} {k} failed: {e}")
            v = None
        self.pools[k] = v
        return v

    def symbol(self, sym):
        if sym == "XRD":
            return Decimal(1)
        if sym in self.syms:
            return self.syms[sym]
        if sym in self._busy or sym not in self.tokens:
            return None
        self._busy.add(sym)
        vals = [v for v in (self.pool(p) for p in self.tokens[sym]["pools"]) if v]
        self._busy.discard(sym)
        self.syms[sym] = sum(vals) / len(vals) if vals else None
        return self.syms[sym]


def xrd_per_token(pool, epoch, tokens=None):
    """XRD per unit of the pool's non-XRD token (pools whose quote is not XRD need `tokens`)."""
    return Resolver(tokens or {}, epoch).pool(pool)


def coingecko_prices(ids):
    if not COINGECKO_URL or not ids:
        return {}
    q = urlparse.urlencode({"ids": ",".join(sorted(ids)), "vs_currencies": "usd"})
    try:
        data = http_json(f"{COINGECKO_URL}/simple/price?{q}")
        return {i: float(data[i]["usd"]) for i in ids if i in data and "usd" in data[i]}
    except Exception as e:
        log.warning(f"CoinGecko failed: {e}")
        return {}


# ---------------------------------------------------------------- config
class TokenConfig:
    def __init__(self, path):
        self.path = path
        self.mtime = None
        self.data = None
        self.reload()

    def reload(self):
        m = os.path.getmtime(self.path)
        if m == self.mtime:
            return False
        with open(self.path) as f:
            d = json.load(f)
        if "XRD" not in d["tokens"]:
            raise ValueError("tokens.json must include XRD")
        for a in d["usd_anchors"]:
            if not d["tokens"].get(a, {}).get("pools"):
                raise ValueError(f"anchor {a} has no pools in tokens.json")
        self.data, self.mtime = d, m
        log.info(f"tokens.json loaded: {len(d['tokens'])} tokens, anchors={d['usd_anchors']}")
        return True

    @property
    def tokens(self):
        return self.data["tokens"]

    @property
    def anchors(self):
        return self.data["usd_anchors"]


# ---------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS latest(
  resource_address TEXT PRIMARY KEY,
  usd_price REAL NOT NULL,
  last_updated_at TEXT NOT NULL,
  price_source TEXT
);
CREATE TABLE IF NOT EXISTS history(
  resource_address TEXT NOT NULL,
  ts INTEGER NOT NULL,
  usd_price REAL NOT NULL,
  PRIMARY KEY(resource_address, ts)
) WITHOUT ROWID;
"""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Store:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._wlock = threading.Lock()
        self.conn().executescript(SCHEMA)
        rows = self.conn().execute(
            "SELECT resource_address, usd_price, last_updated_at, price_source FROM latest").fetchall()
        # in-memory cache: /price/tokens requests never touch disk
        self.latest = {r[0]: (r[1], r[2], r[3]) for r in rows}
        last = self.conn().execute("SELECT MAX(ts) FROM history").fetchone()[0]
        self.last_history = last or 0
        self.last_prune = 0

    def conn(self):
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.c = c
        return c

    def save(self, prices, now):
        """prices: {resource_address: (usd_price, source)}"""
        ts_iso = now.isoformat()
        ts = int(now.replace(tzinfo=timezone.utc).timestamp())
        write_hist = ts - self.last_history >= HISTORY_EVERY_SECS
        with self._wlock:
            c = self.conn()
            with c:
                c.executemany(
                    "INSERT INTO latest VALUES(?,?,?,?) ON CONFLICT(resource_address) DO UPDATE SET "
                    "usd_price=excluded.usd_price, last_updated_at=excluded.last_updated_at, "
                    "price_source=excluded.price_source",
                    [(a, p, ts_iso, s) for a, (p, s) in prices.items()])
                if write_hist:
                    c.executemany("INSERT OR REPLACE INTO history VALUES(?,?,?)",
                                  [(a, ts, p) for a, (p, _) in prices.items()])
            if write_hist:
                self.last_history = ts
            if RETENTION_DAYS > 0 and ts - self.last_prune > 86400:
                with c:
                    n = c.execute("DELETE FROM history WHERE ts < ?",
                                  (ts - RETENTION_DAYS * 86400,)).rowcount
                self.last_prune = ts
                if n:
                    log.info(f"history purge: {n} rows")
        new = dict(self.latest)
        for a, (p, s) in prices.items():
            new[a] = (p, ts_iso, s)
        self.latest = new  # atomic swap

    def closest(self, address, timestamp):
        """Same as the original API: closest sample within [day-1, day+1] (UTC)."""
        day = datetime.fromtimestamp(timestamp, timezone.utc).date()
        lo = int(datetime.combine(day - timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
        hi = int(datetime.combine(day + timedelta(days=2), datetime.min.time(), timezone.utc).timestamp())
        c = self.conn()
        before = c.execute(
            "SELECT ts, usd_price FROM history WHERE resource_address=? AND ts<=? AND ts>? "
            "ORDER BY ts DESC LIMIT 1", (address, timestamp, lo)).fetchone()
        after = c.execute(
            "SELECT ts, usd_price FROM history WHERE resource_address=? AND ts>=? AND ts<? "
            "ORDER BY ts ASC LIMIT 1", (address, timestamp, hi)).fetchone()
        cands = [r for r in (before, after) if r]
        if not cands:
            return None
        return min(cands, key=lambda r: abs(r[0] - timestamp))


# ---------------------------------------------------------------- pricing
class Pricer:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.cg_cache = {}  # coingecko_id -> (price, fetched_at_unix)
        self.last_ok = 0.0
        self.last_error = None

    def resolve_cg(self, sym, cg_id, fresh, ledger, now_s):
        if cg_id in fresh:
            p = fresh[cg_id]
            if ledger and ledger > 0 and abs(p - ledger) / ledger > CG_DIVERGENCE:
                log.warning(f"{sym}: CoinGecko {p} diverges >{CG_DIVERGENCE:.0%} from ledger {ledger}, discarded")
            else:
                self.cg_cache[cg_id] = (p, now_s)
                return p, "coingecko_weighted"
        if cg_id in self.cg_cache:
            p, t = self.cg_cache[cg_id]
            if now_s - t <= CG_STALENESS_SECS:
                return p, "coingecko_cached"
        return None, None

    def cycle(self):
        try:
            self.cfg.reload()
        except Exception as e:
            log.error(f"invalid tokens.json, keeping the previous one: {e}")
        tokens, anchors = self.cfg.tokens, self.cfg.anchors
        t0 = time.monotonic()
        epoch = get_epoch()

        res = Resolver(tokens, epoch)

        usd_per_xrd_c = []
        for sym in anchors:
            for p in tokens[sym]["pools"]:
                v = res.pool(p)
                if v:
                    usd_per_xrd_c.append(Decimal(1) / v)  # anchor = 1 USD
        if not usd_per_xrd_c:
            raise RuntimeError("no anchor pool responded; no USD/XRD price")
        usd_per_xrd = statistics.median(usd_per_xrd_c)

        fresh = coingecko_prices({t["coingecko_id"] for t in tokens.values() if t.get("coingecko_id")})
        now = utcnow()
        now_s = time.time()
        out = {}
        for sym, t in tokens.items():
            if sym == "XRD":
                ledger = float(usd_per_xrd)
            else:
                x = res.symbol(sym)
                ledger = float(x * usd_per_xrd) if x else None
            cg_id = t.get("coingecko_id")
            if cg_id:
                w = float(t.get("coingecko_weight", CG_DEFAULT_WEIGHT))
                cg, src = self.resolve_cg(sym, cg_id, fresh, ledger, now_s)
                if cg is not None and ledger is not None:
                    price = w * cg + (1 - w) * ledger
                elif cg is not None:
                    price = cg
                elif ledger is not None:
                    price, src = ledger, "ledger_only"
                else:
                    log.error(f"{sym}: no price, skipped")
                    continue
            else:
                if ledger is None:
                    log.error(f"{sym}: all pools failed, skipped")
                    continue
                price, src = ledger, "ledger_only"
            out[t["resource_address"]] = (price, src)
        self.store.save(out, now)
        self.last_ok = time.time()
        self.last_error = None
        log.info(f"cycle ok: {len(out)}/{len(tokens)} prices, XRD=${float(usd_per_xrd):.6f}, "
                 f"{len(res.pools)} pools, {time.monotonic() - t0:.2f}s")

    def loop(self, stop):
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                self.cycle()
            except Exception as e:
                self.last_error = str(e)
                log.error(f"cycle failed: {e}")
            stop.wait(max(1.0, INTERVAL_SECS - (time.monotonic() - t0)))


# ---------------------------------------------------------------- LSUs
_lsu_cache = {}  # lsu_address -> (xrd_redemption_value, expires_unix)
_lsu_lock = threading.Lock()


def lsu_redemption_values(addresses):
    """Same logic as app/utils/lsus.py, with timeouts and caching."""
    now = time.time()
    out, need = {}, []
    with _lsu_lock:
        for a in addresses:
            c = _lsu_cache.get(a)
            if c and c[1] > now:
                out[a] = c[0]
            else:
                need.append(a)
    for i in range(0, len(need), 20):
        chunk = need[i:i + 20]
        try:
            data = gw("/state/entity/details", {
                "addresses": chunk, "aggregation_level": "Vault", "explicit_metadata": ["validator"]})
            supply, validators = {}, []
            for it in data["items"]:
                v = [m for m in it["metadata"]["items"] if m["key"] == "validator"]
                if not v:
                    continue
                typed = v[0]["value"]["typed"]
                if typed["type"] == "GlobalAddress" and typed["value"].startswith("validator_"):
                    supply[it["address"]] = it["details"]["total_supply"]
                    validators.append(typed["value"])
            if not validators:
                continue
            vdata = gw("/state/entity/details", {"addresses": validators, "aggregation_level": "Vault"})
            for val in vdata["items"]:
                st = val["details"]["state"]
                su = st["stake_unit_resource_address"]
                vault = st["stake_xrd_vault"]["entity_address"]
                if su not in supply:
                    continue
                xrd_res = next(r for r in val["fungible_resources"]["items"] if r["resource_address"] == XRD)
                bal = next(v["amount"] for v in xrd_res["vaults"]["items"] if v["vault_address"] == vault)
                red = float(bal) / float(supply[su])
                out[su] = red
                with _lsu_lock:
                    _lsu_cache[su] = (red, now + LSU_CACHE_SECS)
        except Exception as e:
            log.warning(f"LSU redemption failed for {len(chunk)} addresses: {e}")
    return out


# ---------------------------------------------------------------- HTTP API
class ApiError(Exception):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail


def _addr_list(body, key):
    v = body.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ApiError(422, f"{key} must be a list of strings")
    if len(v) > MAX_ADDRESSES:
        raise ApiError(422, f"too many {key} (max {MAX_ADDRESSES})")
    return list(dict.fromkeys(v))


def make_handler(store, pricer, cfg):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30
        server_version = "minprice"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("%s %s" % (self.address_string(), fmt % args))

        def _send(self, code, obj):
            b = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 256 * 1024:
                raise ApiError(413, "body too large")
            try:
                d = json.loads(self.rfile.read(n) or b"null")
            except ValueError:
                raise ApiError(422, "invalid JSON")
            if not isinstance(d, dict):
                raise ApiError(422, "body must be a JSON object")
            return d

        def do_GET(self):
            if self.path.split("?")[0] != "/healthz":
                return self._send(404, {"detail": "Not Found"})
            age = time.time() - pricer.last_ok if pricer.last_ok else None
            ok = age is not None and age < INTERVAL_SECS * 5
            self._send(200 if ok else 503, {
                "Everything": "Ok" if ok else "Stale",
                "last_cycle_age_secs": None if age is None else round(age),
                "last_error": pricer.last_error,
                "tokens": len(store.latest),
            })

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            try:
                if path in ("/price/tokens", "/price/tokens-legacy"):
                    return self._send(200, self.prices(self._body()))
                if path in ("/price/historicalPrice", "/price/historicalPrice-legacy"):
                    return self._send(200, self.historical(self._body()))
                if path == "/tokens":
                    return self._send(200, self.token_list())
                self._send(404, {"detail": "Not Found"})
            except ApiError as e:
                self._send(e.code, {"detail": e.detail})
            except Exception as e:
                log.exception(f"error in {path}: {e}")
                self._send(500, {"detail": "Internal Server Error"})

        def prices(self, body):
            cur = body.get("currency")
            if not isinstance(cur, str):
                raise ApiError(422, "currency is required")
            if cur.upper() != "USD":
                raise ApiError(400, f"Currency: {cur} not supported. Supported currencies: USD")
            tokens = _addr_list(body, "tokens")
            lsus = _addr_list(body, "lsus")
            latest = store.latest
            res_t = [{"resource_address": a, "usd_price": latest[a][0], "last_updated_at": latest[a][1]}
                     for a in tokens if a in latest]
            res_l = []
            if lsus:
                xrd = latest.get(XRD)
                if xrd:
                    for a, red in lsu_redemption_values(lsus).items():
                        res_l.append({"resource_address": a, "xrd_redemption_value": red,
                                      "usd_price": xrd[0] * red})
            return {"tokens": res_t, "lsus": res_l}

        def historical(self, body):
            if not API_KEY or self.headers.get("x-api-key") != API_KEY:
                raise ApiError(401, "Invalid or missing API Key")
            tokens = _addr_list(body, "tokens")
            ts = body.get("timestamp")
            if not tokens:
                raise ApiError(400, "Token list must be provided.")
            if not isinstance(ts, int) or isinstance(ts, bool):
                raise ApiError(400, "Timestamp must be an integer (unix seconds).")
            found, missing = {}, []
            for a in tokens:
                r = store.closest(a, ts)
                if r:
                    found[a] = {"usd_price": r[1],
                                "last_updated_at": datetime.fromtimestamp(r[0], timezone.utc)
                                .replace(tzinfo=None).isoformat()}
                else:
                    missing.append(a)
            if missing:
                raise ApiError(424, f"Price missing for tokens: {', '.join(missing)}")
            return {"prices": found}

        def token_list(self):
            latest = store.latest
            out = []
            for i, (sym, t) in enumerate(cfg.tokens.items(), 1):
                p = latest.get(t["resource_address"])
                if p:
                    out.append({"id": i, "resource_address": t["resource_address"], "symbol": sym,
                                "name": t.get("name", sym), "price": p[0], "currency": "USD"})
            return out

    return Handler


def main():
    cfg = TokenConfig(TOKENS_FILE)
    store = Store(DB_PATH)
    pricer = Pricer(cfg, store)
    stop = threading.Event()
    threading.Thread(target=pricer.loop, args=(stop,), daemon=True, name="pricer").start()
    srv = ThreadingHTTPServer((HOST, PORT), make_handler(store, pricer, cfg))
    srv.daemon_threads = True

    def shutdown(*_):
        stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info(f"listening on {HOST}:{PORT}, gateway={GATEWAY_URL}, db={DB_PATH}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
