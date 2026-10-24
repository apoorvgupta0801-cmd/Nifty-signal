"""Trading Floor core: data -> analyst bots -> risk filters -> chief decision.
Signals only. Educational tool, not investment advice. Paper trade first."""
import json
import math
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from urllib.parse import quote

import requests

IST = timezone(timedelta(hours=5, minutes=30))
NSE = "https://www.nseindia.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
NSE_HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Accept-Language": "en-US,en;q=0.9",
               "Referer": NSE + "/option-chain"}
CHAIN_URL = {
    "index": NSE + "/api/option-chain-indices?symbol={s}",
    "stock": NSE + "/api/option-chain-equities?symbol={s}",
}
YAHOO_URL = ("https://{h}.finance.yahoo.com/v8/finance/chart/{s}"
             "?interval=5m&range=5d")
BINANCE_URL = ("https://data-api.binance.vision/api/v3/klines"
               "?symbol={s}&interval=5m&limit=300")

WATCHLIST = [
    dict(name="NIFTY", kind="index", chain="NIFTY", yahoo="^NSEI"),
    dict(name="BANKNIFTY", kind="index", chain="BANKNIFTY", yahoo="^NSEBANK"),
    dict(name="FINNIFTY", kind="index", chain="FINNIFTY", yahoo=None),
    dict(name="RELIANCE", kind="stock", chain="RELIANCE", yahoo="RELIANCE.NS"),
    dict(name="HDFCBANK", kind="stock", chain="HDFCBANK", yahoo="HDFCBANK.NS"),
    dict(name="TCS", kind="stock", chain="TCS", yahoo="TCS.NS"),
    dict(name="INFY", kind="stock", chain="INFY", yahoo="INFY.NS"),
    dict(name="ICICIBANK", kind="stock", chain="ICICIBANK",
         yahoo="ICICIBANK.NS"),
    dict(name="SBIN", kind="stock", chain="SBIN", yahoo="SBIN.NS"),
    dict(name="BTC", kind="crypto", binance="BTCUSDT"),
    dict(name="ETH", kind="crypto", binance="ETHUSDT"),
]

MIN_RR = 2.0          # reject setups with reward:risk below this
MIN_TOTAL = 3.0       # combined bot score needed to signal
COOLDOWN_SEC = 30 * 60


# ---------------------------------------------------------------- helpers
def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def rnd(x):
    return round(x, 2) if abs(x) >= 1 else round(x, 6)


def short(e):
    return (str(e) or type(e).__name__)[:90]


def ema(vals, n):
    k = 2.0 / (n + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def atr(c, n=14):
    trs = []
    for i in range(1, len(c["close"])):
        h, l, pc = c["high"][i], c["low"][i], c["close"][i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


def resample(c, n):
    """Group n bars into one (aligned from the end so groups are complete)."""
    L = len(c["close"])
    out = {"high": [], "low": [], "close": [], "ts": []}
    for i in range(L % n, L, n):
        out["high"].append(max(c["high"][i:i + n]))
        out["low"].append(min(c["low"][i:i + n]))
        out["close"].append(c["close"][i + n - 1])
        out["ts"].append(c["ts"][i])
    return out


def is_open(kind, now, demo=False):
    if kind == "crypto" or demo:
        return True
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


# ------------------------------------------------- chart maths / helpers
INDEXES = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": None,
           "MIDCPNIFTY": None}


def make_instrument(text):
    """Turn what the user typed (ITC, SOLUSDT, MIDCPNIFTY) into a watchlist
    entry, or None if it is not a valid symbol."""
    s = (text or "").strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", s):
        return None
    if s.endswith("USDT") and len(s) > 4:
        return dict(name=s[:-4], kind="crypto", binance=s, custom=True)
    if s in INDEXES:
        return dict(name=s, kind="index", chain=s, yahoo=INDEXES[s],
                    custom=True)
    return dict(name=s, kind="stock", chain=s, yahoo=s + ".NS", custom=True)


def fmt_oi(n):
    n = n or 0
    if n >= 1e7:
        return "%.1fCr" % (n / 1e7)
    if n >= 1e5:
        return "%.1fL" % (n / 1e5)
    if n >= 1e3:
        return "%.0fK" % (n / 1e3)
    return "%d" % n


def nice_range(vals, extra=()):
    xs = [v for v in list(vals) + list(extra) if v is not None]
    if not xs:
        return 0.0, 1.0
    lo, hi = min(xs), max(xs)
    pad = (hi - lo) * 0.08 if hi - lo > 1e-9 else (abs(hi) * 0.001 or 1.0)
    return lo - pad, hi + pad


def y_of(v, y0, h, lo, hi):
    return y0 + h * (v - lo) / (hi - lo)


def line_points(vals, x0, y0, w, h, lo, hi):
    """Flat [x1, y1, x2, y2...] list for a Kivy Line, or [] if not drawable."""
    n = len(vals)
    if n < 2 or hi <= lo:
        return []
    pts = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        pts += [x0 + w * i / (n - 1), y_of(v, y0, h, lo, hi)]
    return pts


def oi_bars(rows, x0, y0, w, h):
    """Rectangles (x, y, w, h, 'CE'|'PE') for the open-interest bar chart."""
    n = len(rows)
    if n == 0:
        return []
    mx = max(max(r["ce_oi"], r["pe_oi"]) for r in rows) or 1
    slot = w / n
    bw = slot * 0.4
    out = []
    for i, r in enumerate(rows):
        sx = x0 + i * slot + slot * 0.1
        out.append((sx, y0, bw, h * r["ce_oi"] / mx, "CE"))
        out.append((sx + bw, y0, bw, h * r["pe_oi"] / mx, "PE"))
    return out


def x_for_price(rows, price, x0, w):
    """Horizontal position of a price among evenly spaced strikes."""
    n = len(rows)
    if n < 2:
        return x0 + w / 2.0
    ks = [r["strike"] for r in rows]
    if price <= ks[0]:
        i = 0.0
    elif price >= ks[-1]:
        i = n - 1.0
    else:
        j = max(k for k in range(n) if ks[k] <= price)
        i = j + (price - ks[j]) / float(ks[j + 1] - ks[j])
    return x0 + (i + 0.5) * (w / n)


def visible_names(insts, results, query, flt):
    """Which watchlist names to show, signals first, honouring search+filter."""
    q = (query or "").strip().upper()
    keep = []
    for i, inst in enumerate(insts):
        r = results.get(inst["name"])
        sig = bool(r) and str(r.get("action", "")).startswith(
            ("BUY", "LONG", "SHORT"))
        if q and q not in inst["name"]:
            continue
        if flt == "Index" and inst["kind"] != "index":
            continue
        if flt == "Stocks" and inst["kind"] != "stock":
            continue
        if flt == "Crypto" and inst["kind"] != "crypto":
            continue
        if flt == "Signals" and not sig:
            continue
        keep.append((0 if sig else 1, i, inst["name"]))
    keep.sort()
    return [k[2] for k in keep]


# ------------------------------------------------------------------- data
class Data:
    def __init__(self, demo=False):
        self.demo = demo
        self.nse = None
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": UA})

    def _nse(self):
        if self.nse is None:
            s = requests.Session()
            s.headers.update(NSE_HEADERS)
            s.get(NSE, timeout=10)
            try:
                s.get(NSE + "/option-chain", timeout=10)
            except Exception:
                pass
            self.nse = s
        return self.nse

    def _chain_v3(self, s, inst):
        """NSE's newer endpoints: contract-info for expiries, then v3."""
        sym = inst["chain"]
        typ = "Indices" if inst["kind"] == "index" else "Equity"
        ci = s.get(NSE + "/api/option-chain-contract-info?symbol=" + sym,
                   timeout=10)
        ci.raise_for_status()
        exps = ci.json().get("expiryDates") or []
        today = datetime.now(IST).date()
        exps = sorted(
            (e for e in exps
             if datetime.strptime(e, "%d-%b-%Y").date() >= today),
            key=lambda e: datetime.strptime(e, "%d-%b-%Y"))
        if not exps:
            raise ValueError("no expiry dates")
        expiry = exps[0]
        r = s.get(NSE + "/api/option-chain-v3?type=%s&symbol=%s&expiry=%s"
                  % (typ, sym, expiry), timeout=15)
        r.raise_for_status()
        j = r.json()
        rec = j.get("records") or {}
        if not rec.get("data"):
            raise ValueError("empty v3 data")
        rec["expiryDates"] = [expiry]
        if "underlyingValue" not in rec:
            rec["underlyingValue"] = j.get("underlyingValue") or \
                (j.get("filtered") or {}).get("underlyingValue")
        for d in rec["data"]:
            if "expiryDate" not in d:
                d["expiryDate"] = d.get("expiryDates") or expiry
        return {"records": rec}

    def chain(self, inst):
        if self.demo:
            return demo_chain(inst)
        errs = []
        for attempt in range(2):
            try:
                s = self._nse()
                try:
                    return self._chain_v3(s, inst)
                except Exception as e:
                    errs.append("v3 " + short(e))
                r = s.get(CHAIN_URL[inst["kind"]].format(s=inst["chain"]),
                          timeout=10)
                r.raise_for_status()
                j = r.json()
                if j.get("records", {}).get("data"):
                    return j
                errs.append("legacy empty")
            except Exception as e:
                errs.append("legacy " + short(e))
            self.nse = None  # fresh cookies and retry once
        raise ValueError(" | ".join(errs)[-170:])

    def candles(self, inst):
        if self.demo:
            return demo_candles(inst)
        if inst.get("binance"):
            rows = self.http.get(BINANCE_URL.format(s=inst["binance"]),
                                 timeout=10).json()
            if not isinstance(rows, list):
                raise ValueError("Binance returned an error")
            return dict(ts=[r[0] / 1000.0 for r in rows],
                        high=[float(r[2]) for r in rows],
                        low=[float(r[3]) for r in rows],
                        close=[float(r[4]) for r in rows])
        if inst.get("yahoo"):
            last = None
            for host in ("query1", "query2"):
                try:
                    j = self.http.get(
                        YAHOO_URL.format(h=host, s=quote(inst["yahoo"])),
                        timeout=10).json()
                    res = j["chart"]["result"][0]
                    q = res["indicators"]["quote"][0]
                    rows = [(t, h, l, c) for t, h, l, c in zip(
                        res["timestamp"], q["high"], q["low"], q["close"])
                        if None not in (h, l, c)]
                    if len(rows) < 5:
                        raise ValueError("too little price history")
                    return dict(ts=[r[0] for r in rows],
                                high=[r[1] for r in rows],
                                low=[r[2] for r in rows],
                                close=[r[3] for r in rows])
                except Exception as e:
                    last = e
            raise ValueError("Yahoo: " + short(last))
        return None


# -------------------------------------------------------------- demo data
_DEMO_BASE = {"NIFTY": 24650, "BANKNIFTY": 52800, "FINNIFTY": 23500,
              "RELIANCE": 1450, "HDFCBANK": 1000, "TCS": 3300, "INFY": 1600,
              "ICICIBANK": 1400, "SBIN": 900, "BTC": 95000, "ETH": 3500}
_demo_state = {}


def _demo_spot(name):
    s = _demo_state.setdefault(
        name, dict(px=float(_DEMO_BASE.get(name, 1000)),
                   drift=random.choice([-1, 1])))
    if random.random() < 0.05:
        s["drift"] *= -1
    s["px"] *= 1 + random.gauss(0.0006 * s["drift"], 0.0008)
    return s["px"], s["drift"]


def demo_candles(inst):
    px, drift = _demo_spot(inst["name"])
    closes = [px]
    for _ in range(299):
        closes.append(closes[-1] / (1 + random.gauss(0.0006 * drift, 0.0008)))
    closes.reverse()
    highs = [c * (1 + abs(random.gauss(0, 0.0004))) for c in closes]
    lows = [c * (1 - abs(random.gauss(0, 0.0004))) for c in closes]
    now = time.time()
    ts = [now - (299 - i) * 300 for i in range(300)]
    return dict(ts=ts, high=highs, low=lows, close=closes)


def demo_chain(inst):
    spot, drift = _demo_spot(inst["name"])
    if inst["kind"] == "index":
        step = 100 if inst["name"] == "BANKNIFTY" else 50
    else:
        step = 10 if spot > 1000 else 5
    base = int(spot // step * step)
    exp = (datetime.now(IST) + timedelta(days=3)).strftime("%d-%b-%Y")
    data = []
    for k in range(base - 15 * step, base + 16 * step, step):
        d = k - spot
        tv = max(spot * 0.0001, spot * 0.004 - abs(d) * 0.1)

        def oi(peak, mult):
            return int(1_000_000 * mult * math.exp(
                -((k - peak) / (step * 6)) ** 2) * random.uniform(0.8, 1.2))
        pe_bias = 1.3 if drift > 0 else 0.8
        ce_bias = 0.8 if drift > 0 else 1.3
        data.append({
            "strikePrice": k, "expiryDate": exp,
            "CE": {"openInterest": oi(spot + step * 6, ce_bias),
                   "changeinOpenInterest": int(random.randint(-50_000, 150_000)
                                               * ce_bias),
                   "lastPrice": round(max(spot - k, 0) + tv, 2),
                   "impliedVolatility": round(random.uniform(11, 14), 2)},
            "PE": {"openInterest": oi(spot - step * 6, pe_bias),
                   "changeinOpenInterest": int(random.randint(-50_000, 150_000)
                                               * pe_bias),
                   "lastPrice": round(max(k - spot, 0) + tv, 2),
                   "impliedVolatility": round(random.uniform(11, 14), 2)},
        })
    ts = datetime.now(IST).strftime("%d-%b-%Y %H:%M:%S")
    return {"records": {"underlyingValue": spot, "expiryDates": [exp],
                        "timestamp": ts, "data": data}}


# ------------------------------------------------------------- option bot
def read_chain(raw):
    rec = raw.get("records") if isinstance(raw, dict) else None
    if not rec or not rec.get("data"):
        raise ValueError("NSE returned no option data")
    spot = rec["underlyingValue"]
    today = datetime.now(IST).date()
    exps = sorted(rec["expiryDates"],
                  key=lambda d: datetime.strptime(d, "%d-%b-%Y"))
    future = [e for e in exps
              if datetime.strptime(e, "%d-%b-%Y").date() >= today] or exps
    expiry = future[0]
    strikes = {}
    for d in rec["data"]:
        if d["expiryDate"] != expiry:
            continue
        ce, pe = d.get("CE", {}), d.get("PE", {})
        strikes[d["strikePrice"]] = dict(
            ce_oi=ce.get("openInterest", 0), pe_oi=pe.get("openInterest", 0),
            ce_chg=ce.get("changeinOpenInterest", 0),
            pe_chg=pe.get("changeinOpenInterest", 0),
            ce_ltp=ce.get("lastPrice", 0), pe_ltp=pe.get("lastPrice", 0),
            ce_iv=ce.get("impliedVolatility", 0),
            pe_iv=pe.get("impliedVolatility", 0))
    ks = sorted(strikes)
    if len(ks) < 5:
        raise ValueError("Too few strikes in option chain")
    step = min(b - a for a, b in zip(ks, ks[1:]) if b > a)
    atm = min(ks, key=lambda k: abs(k - spot))
    above = [k for k in ks if k >= atm]
    below = [k for k in ks if k <= atm]
    resistance = max(above, key=lambda k: strikes[k]["ce_oi"])
    support = max(below, key=lambda k: strikes[k]["pe_oi"])
    tot_ce = sum(v["ce_oi"] for v in strikes.values())
    tot_pe = sum(v["pe_oi"] for v in strikes.values())

    def pain(k):
        return sum(max(0, k - s) * v["ce_oi"] + max(0, s - k) * v["pe_oi"]
                   for s, v in strikes.items())
    near = [k for k in ks if abs(k - atm) <= 5 * step]
    a = strikes[atm]
    ts = None
    try:
        ts = datetime.strptime(rec["timestamp"], "%d-%b-%Y %H:%M:%S"
                               ).replace(tzinfo=IST)
    except Exception:
        pass
    rows = [dict(strike=k, ce_oi=strikes[k]["ce_oi"],
                 pe_oi=strikes[k]["pe_oi"], ce_ltp=strikes[k]["ce_ltp"],
                 pe_ltp=strikes[k]["pe_ltp"], ce_chg=strikes[k]["ce_chg"],
                 pe_chg=strikes[k]["pe_chg"])
            for k in ks if abs(k - atm) <= 12 * step]
    return dict(
        rows=rows, spot=spot, expiry=expiry, atm=atm, step=step,
        pcr=(tot_pe / tot_ce) if tot_ce else 0,
        support=support, resistance=resistance,
        max_pain=min(ks, key=pain),
        ce_chg=sum(strikes[k]["ce_chg"] for k in near),
        pe_chg=sum(strikes[k]["pe_chg"] for k in near),
        straddle=a["ce_ltp"] + a["pe_ltp"],
        atm_iv=(a["ce_iv"] + a["pe_iv"]) / 2,
        ce_ltp=a["ce_ltp"], pe_ltp=a["pe_ltp"], ts=ts)


def option_vote(raw, hist):
    m = read_chain(raw)
    spot, step = m["spot"], m["step"]
    hist.append((time.time(), spot))
    score, notes = 0.0, []
    if m["pcr"] >= 1.2:
        score += 1
        notes.append("PCR %.2f high: put writers active" % m["pcr"])
    elif m["pcr"] <= 0.8:
        score -= 1
        notes.append("PCR %.2f low: call writers active" % m["pcr"])
    else:
        notes.append("PCR %.2f neutral" % m["pcr"])
    if m["pe_chg"] > 1.2 * max(m["ce_chg"], 1):
        score += 1
        notes.append("Fresh put OI beats call OI near ATM")
    elif m["ce_chg"] > 1.2 * max(m["pe_chg"], 1):
        score -= 1
        notes.append("Fresh call OI beats put OI near ATM")
    if spot - m["support"] <= 1.5 * step:
        score += 0.5
        notes.append("Spot near put wall %s" % m["support"])
    if m["resistance"] - spot <= 1.5 * step:
        score -= 0.5
        notes.append("Spot near call wall %s" % m["resistance"])
    if spot < m["max_pain"] - 2 * step:
        score += 0.5
        notes.append("Below max pain %s" % m["max_pain"])
    elif spot > m["max_pain"] + 2 * step:
        score -= 0.5
        notes.append("Above max pain %s" % m["max_pain"])
    if len(hist) >= 5:
        mom = hist[-1][1] - hist[0][1]
        if mom > 0.3 * step:
            score += 1
            notes.append("Momentum +%.0f pts" % mom)
        elif mom < -0.3 * step:
            score -= 1
            notes.append("Momentum %.0f pts" % mom)
    return dict(bot="Option chain", score=clamp(score * 0.7, -2, 2),
                notes=notes, m=m)


# ------------------------------------------------------- price-based bots
def trend_vote(c, label, minbars=30):
    if not c or len(c["close"]) < minbars:
        return None
    cl = c["close"]
    px = cl[-1]
    e9, e21 = ema(cl, 9)[-1], ema(cl, 21)[-1]
    r = rsi(cl)
    score, notes = 0.0, []
    gap = (e9 - e21) / e21
    if gap > 0.0005:
        score += 1
        notes.append("EMA9 above EMA21 (uptrend)")
    elif gap < -0.0005:
        score -= 1
        notes.append("EMA9 below EMA21 (downtrend)")
    else:
        notes.append("EMAs flat: no trend")
    score += 0.5 if px > e21 else -0.5
    if r is not None:
        if r > 55:
            score += 0.5
        elif r < 45:
            score -= 0.5
        notes.append("RSI %.0f" % r)
        if r > 75 and score > 0:
            score -= 0.5
            notes.append("RSI stretched high, trimmed")
        if r < 25 and score < 0:
            score += 0.5
            notes.append("RSI stretched low, trimmed")
    return dict(bot=label, score=clamp(score, -2, 2), notes=notes,
                price=px, atr=atr(c), rsi=r)


def breakout_vote(c, n=20):
    if not c or len(c["close"]) < n + 5:
        return None
    px = c["close"][-1]
    hh = max(c["high"][-n - 1:-1])
    ll = min(c["low"][-n - 1:-1])
    a = atr(c) or 0
    if px > hh:
        s, note = 1.5, "Broke above %d-bar high" % n
    elif px < ll:
        s, note = -1.5, "Broke below %d-bar low" % n
    elif a and px >= hh - 0.25 * a:
        s, note = 0.5, "Pressing the %d-bar high" % n
    elif a and px <= ll + 0.25 * a:
        s, note = -0.5, "Pressing the %d-bar low" % n
    else:
        s, note = 0.0, "Inside the recent range"
    return dict(bot="Breakout", score=s, notes=[note])


def regime_check(c, n=30):
    """Efficiency ratio: low means choppy sideways noise."""
    if not c or len(c["close"]) < n + 2:
        return None
    cl = c["close"][-(n + 1):]
    path = sum(abs(b - a) for a, b in zip(cl, cl[1:]))
    er = abs(cl[-1] - cl[0]) / path if path else 0
    choppy = er < 0.20
    return dict(bot="Regime filter", score=0, choppy=choppy,
                notes=["Trend quality %.2f (%s)" % (
                    er, "choppy, avoid" if choppy else "tradable")])


# ---------------------------------------------------------------- journal
class Journal:
    """Paper-trade log: every signal is tracked to a win or loss."""

    def __init__(self, path=None):
        self.path = path
        self.trades = []
        self.last_close = {}
        try:
            if path:
                with open(path) as f:
                    j = json.load(f)
                self.trades = j.get("trades", [])
                self.last_close = j.get("last_close", {})
        except Exception:
            pass

    def save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w") as f:
                json.dump(dict(trades=self.trades[-500:],
                               last_close=self.last_close), f)
        except Exception:
            pass

    def open_for(self, name):
        for t in self.trades:
            if t["name"] == name and t["status"] == "OPEN":
                return t
        return None

    def try_open(self, name, plan, now_ts):
        if self.open_for(name):
            return None
        if now_ts - self.last_close.get(name, 0) < COOLDOWN_SEC:
            return None
        t = dict(name=name, dir=plan["dir"], entry=plan["entry"],
                 sl=plan["sl"], tp=plan["tp"], rr=plan["rr"],
                 action=plan["label"], plan=plan, ts=now_ts, status="OPEN")
        self.trades.append(t)
        self.save()
        return t

    def update(self, name, price, c, now_ts, force_expire=False):
        t = self.open_for(name)
        if not t:
            return None
        d = t["dir"]
        bars = []
        if c and c.get("ts"):
            bars = [(h, l) for h, l, ts in zip(c["high"], c["low"], c["ts"])
                    if ts >= t["ts"]]
        bars.append((price, price))
        hit = None
        for hi, lo in bars:
            sl_hit = lo <= t["sl"] if d > 0 else hi >= t["sl"]
            tp_hit = hi >= t["tp"] if d > 0 else lo <= t["tp"]
            if sl_hit:           # conservative: stoploss first
                hit = "LOSS"
                break
            if tp_hit:
                hit = "WIN"
                break
        if not hit and force_expire:
            hit = "EXPIRED"
        if not hit:
            return None
        exit_px = (t["sl"] if hit == "LOSS" else
                   t["tp"] if hit == "WIN" else price)
        risk = abs(t["entry"] - t["sl"]) or 1
        t.update(status=hit, exit=exit_px, closed=now_ts,
                 r=round((exit_px - t["entry"]) * d / risk, 2))
        self.last_close[name] = now_ts
        self.save()
        return "%s closed: %s (%+.1fR)" % (t["action"], hit, t["r"])

    def stats(self, name=None):
        done = [t for t in self.trades if t["status"] != "OPEN"
                and (name is None or t["name"] == name)]
        wins = sum(1 for t in done if t["r"] > 0)
        tot = sum(t["r"] for t in done)
        return dict(closed=len(done), wins=wins,
                    win_rate=(100.0 * wins / len(done)) if done else 0.0,
                    total_r=round(tot, 2),
                    open=sum(1 for t in self.trades if t["status"] == "OPEN"
                             and (name is None or t["name"] == name)))


# ------------------------------------------------------------------- plan
def make_plan(inst, d, price, opt, atr_v):
    m = opt["m"] if opt else None
    em = m["straddle"] if m else 0
    risk = (1.5 * atr_v if atr_v else (0.4 * em if em else 0.01 * price))
    reach = (3 * atr_v if atr_v else (0.8 * em if em else 2 * risk))
    tp = price + d * reach
    if m:
        wall = m["resistance"] if d > 0 else m["support"]
        if d > 0 and wall > price:
            tp = min(tp, wall)
        if d < 0 and wall < price:
            tp = max(tp, wall)
    sl = price - d * risk
    reward = abs(tp - price)
    plan = dict(dir=d, entry=rnd(price), sl=rnd(sl), tp=rnd(tp),
                rr=round(reward / risk, 2) if risk else 0, risk=rnd(risk))
    if inst["kind"] == "index" and m:
        prem = m["ce_ltp"] if d > 0 else m["pe_ltp"]
        if prem <= 0:
            return None
        plan.update(side="CE" if d > 0 else "PE", strike=m["atm"], prem=prem,
                    prem_sl=rnd(max(prem - 0.5 * risk, 0.3 * prem)),
                    prem_tp=rnd(prem + 0.5 * reward))
        plan["label"] = "BUY %s %s" % (m["atm"], plan["side"])
    else:
        plan["label"] = "LONG" if d > 0 else "SHORT"
    return plan


# -------------------------------------------------------------- the floor
def run(inst, data, hist, journal=None, demo=False):
    now = datetime.now(IST)
    name, kind = inst["name"], inst["kind"]
    out = dict(name=name, kind=kind, updated=now.strftime("%H:%M:%S"),
               ok=True, errors=[], votes=[], notes=[], events=[],
               action="NO TRADE", price=None, plan=None, open=False,
               total=0.0)
    opt = cand = None
    if inst.get("chain"):
        try:
            opt = option_vote(data.chain(inst), hist)
        except Exception as e:
            out["errors"].append("Option chain: " + short(e))
    try:
        cand = data.candles(inst)
    except Exception as e:
        out["errors"].append("Price data: " + short(e))

    t5 = t15 = bo = regime = None
    if cand:
        t5 = trend_vote(cand, "Trend 5m")
        t15 = trend_vote(resample(cand, 3), "Trend 15m", minbars=25)
        bo = breakout_vote(cand)
        regime = regime_check(cand)
    bots = [b for b in (opt, t5, t15, bo) if b]
    if not bots:
        out.update(ok=False, action="NO DATA")
        return out

    price = opt["m"]["spot"] if opt else cand["close"][-1]
    out["price"] = rnd(price)
    atr_v = atr(cand) if cand else None
    if cand and len(cand["close"]) >= 10:
        full = cand["close"]
        out["series"] = dict(close=full[-150:], ema9=ema(full, 9)[-150:],
                             ema21=ema(full, 21)[-150:], live=False)
    elif len(hist) >= 2:
        out["series"] = dict(close=[p for _, p in hist], live=True)
    if opt:
        mm = opt["m"]
        out["chain"] = dict(
            rows=mm["rows"], spot=mm["spot"], atm=mm["atm"],
            support=mm["support"], resistance=mm["resistance"],
            max_pain=mm["max_pain"], pcr=mm["pcr"], atm_iv=mm["atm_iv"],
            expiry=mm["expiry"], step=mm["step"])
    out["votes"] = [dict(bot=b["bot"], score=b["score"], notes=b["notes"])
                    for b in bots + ([regime] if regime else [])]
    open_ = is_open(kind, now, demo)

    # resolve any open paper trade first
    ot = journal.open_for(name) if journal else None
    if ot:
        nse = kind != "crypto" and not demo
        stale_day = nse and datetime.fromtimestamp(
            ot["ts"], IST).date() != now.date()
        closing = nse and now.time() >= dtime(15, 15)
        ev = journal.update(name, price, cand, now.timestamp(),
                            force_expire=stale_day or closing)
        if ev:
            out["events"].append(ev)

    notes = out["notes"]
    total = sum(b["score"] for b in bots)
    out["total"] = total
    d = 1 if total > 0 else -1 if total < 0 else 0
    agree = sum(1 for b in bots if b["score"] * d >= 0.5)
    need = max(2, math.ceil(0.75 * len(bots)))
    oppose = [b["bot"] for b in bots if b["score"] * d <= -1]
    if d == 0 or abs(total) < MIN_TOTAL:
        notes.append("Combined score %+.1f is below +/-%.1f: no edge." % (
            total, MIN_TOTAL))
        d = 0
    elif agree < need:
        notes.append("Only %d of %d bots agree (need %d)." % (
            agree, len(bots), need))
        d = 0
    elif oppose:
        notes.append("Strong opposition from: " + ", ".join(oppose))
        d = 0
    elif regime and regime["choppy"]:
        notes.append("Choppy market: trend trades fail often. Skipping.")
        d = 0
    elif t5 and t5.get("rsi") is not None and (
            (d < 0 and t5["rsi"] < 22) or (d > 0 and t5["rsi"] > 78)):
        notes.append("Move looks exhausted (5m RSI %.0f). Not chasing it."
                     % t5["rsi"])
        d = 0

    plan = None
    if d != 0:
        plan = make_plan(inst, d, price, opt, atr_v)
        if plan is None:
            notes.append("No option premium data for entry.")
        elif plan["rr"] < MIN_RR:
            notes.append("Reward:risk %.1f is below %.1f. Skipping." % (
                plan["rr"], MIN_RR))
            plan = None
    if plan and kind != "crypto" and not demo:
        t = now.time()
        if t < dtime(9, 30) or t >= dtime(14, 45):
            notes.append("No fresh entries before 9:30 or after 2:45 PM IST.")
            plan = None
    if plan and opt and not demo and open_ and opt["m"]["ts"]:
        if (now - opt["m"]["ts"]) > timedelta(minutes=20):
            notes.append("Option data looks stale (holiday or feed lag).")
            plan = None

    ot = journal.open_for(name) if journal else None
    if ot:
        out.update(action=ot["action"], plan=ot["plan"], open=True)
        since = datetime.fromtimestamp(ot["ts"], IST).strftime("%H:%M")
        notes.insert(0, "Signal open since %s IST. Exit at stoploss or "
                        "target." % since)
    elif not open_:
        out["action"] = "MARKET CLOSED"
        notes.insert(0, "Market closed: analysis is informational only.")
    elif plan:
        if journal:
            if journal.try_open(name, plan, now.timestamp()):
                out.update(action=plan["label"], plan=plan, new=True)
            else:
                notes.append("Cooldown after the last trade. Waiting.")
        else:
            out.update(action=plan["label"], plan=plan, new=True)
    return out


if __name__ == "__main__":
    import sys
    demo = "--demo" in sys.argv
    data, journal = Data(demo=demo), Journal()
    hists = {i["name"]: deque(maxlen=30) for i in WATCHLIST}
    for cycle in range(int(sys.argv[-1]) if sys.argv[-1].isdigit() else 3):
        for inst in WATCHLIST:
            r = run(inst, data, hists[inst["name"]], journal, demo)
            print("%-10s %-14s total %+.1f  %s" % (
                r["name"], r["action"], r["total"],
                (r["notes"] or [""])[-1][:60]))
            for e in r["events"]:
                print("   EVENT:", e)
        print("--- stats:", journal.stats())
