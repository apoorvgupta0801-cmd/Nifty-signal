#!/usr/bin/env python3
"""
Nifty Option Chain Signal Agent
--------------------------------
Polls the NSE option chain, scores it, and prints a BUY CE / BUY PE / NO TRADE
signal with index-level and premium-level stoploss and target.

Run:   pip install requests
       python nifty_agent.py            # refresh every 60s
       python nifty_agent.py --every 30 --symbol NIFTY

Educational tool. Not investment advice. Test on paper before using real money.
"""
import argparse
import time
from collections import deque
from datetime import datetime

import requests

BASE = "https://www.nseindia.com"
URL = BASE + "/api/option-chain-indices?symbol={symbol}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/option-chain",
}
STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50}
MIN_RR = 1.5          # skip trades with reward:risk below this
ATM_WINDOW = 5        # strikes each side of ATM used for OI-change analysis

history = deque(maxlen=30)  # recent (timestamp, spot) for momentum


def new_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(BASE, timeout=10)  # sets cookies NSE requires
    return s


def fetch(session, symbol):
    r = session.get(URL.format(symbol=symbol), timeout=10)
    r.raise_for_status()
    return r.json()


def analyse(raw, symbol):
    rec = raw["records"]
    spot = rec["underlyingValue"]
    expiry = sorted(
        rec["expiryDates"], key=lambda d: datetime.strptime(d, "%d-%b-%Y")
    )[0]
    rows = [d for d in rec["data"] if d["expiryDate"] == expiry]
    step = STEP.get(symbol, 50)
    atm = round(spot / step) * step

    strikes = {}
    for d in rows:
        k = d["strikePrice"]
        ce, pe = d.get("CE", {}), d.get("PE", {})
        strikes[k] = {
            "ce_oi": ce.get("openInterest", 0), "pe_oi": pe.get("openInterest", 0),
            "ce_chg": ce.get("changeinOpenInterest", 0),
            "pe_chg": pe.get("changeinOpenInterest", 0),
            "ce_ltp": ce.get("lastPrice", 0), "pe_ltp": pe.get("lastPrice", 0),
            "ce_iv": ce.get("impliedVolatility", 0),
            "pe_iv": pe.get("impliedVolatility", 0),
        }

    tot_ce = sum(v["ce_oi"] for v in strikes.values())
    tot_pe = sum(v["pe_oi"] for v in strikes.values())
    pcr = tot_pe / tot_ce if tot_ce else 0

    resistance = max(strikes, key=lambda k: strikes[k]["ce_oi"])
    support = max(strikes, key=lambda k: strikes[k]["pe_oi"])

    def pain(k):  # total payout to option buyers if expiry at k
        return sum(
            max(0, k - s) * v["ce_oi"] + max(0, s - k) * v["pe_oi"]
            for s, v in strikes.items()
        )
    max_pain = min(strikes, key=pain)

    near = [k for k in strikes if abs(k - atm) <= ATM_WINDOW * step]
    ce_chg = sum(strikes[k]["ce_chg"] for k in near)
    pe_chg = sum(strikes[k]["pe_chg"] for k in near)

    a = strikes.get(atm, {})
    straddle = a.get("ce_ltp", 0) + a.get("pe_ltp", 0)
    atm_iv = (a.get("ce_iv", 0) + a.get("pe_iv", 0)) / 2

    return dict(
        spot=spot, expiry=expiry, atm=atm, pcr=pcr, support=support,
        resistance=resistance, max_pain=max_pain, ce_chg=ce_chg, pe_chg=pe_chg,
        straddle=straddle, atm_iv=atm_iv, ce_ltp=a.get("ce_ltp", 0),
        pe_ltp=a.get("pe_ltp", 0), step=step,
    )


def momentum():
    if len(history) < 5:
        return 0.0
    return history[-1][1] - history[0][1]


def decide(m):
    score, why = 0.0, []
    spot, step = m["spot"], m["step"]

    # 1. PCR
    if m["pcr"] >= 1.2:
        score += 1; why.append(f"PCR {m['pcr']:.2f} high (put writers active)")
    elif m["pcr"] <= 0.8:
        score -= 1; why.append(f"PCR {m['pcr']:.2f} low (call writers active)")
    else:
        why.append(f"PCR {m['pcr']:.2f} neutral")

    # 2. Fresh OI near ATM
    if m["pe_chg"] > 1.2 * max(m["ce_chg"], 1):
        score += 1; why.append("Fresh put OI > call OI near ATM (bullish)")
    elif m["ce_chg"] > 1.2 * max(m["pe_chg"], 1):
        score -= 1; why.append("Fresh call OI > put OI near ATM (bearish)")

    # 3. Position vs OI walls
    if spot - m["support"] <= 1.5 * step:
        score += 0.5; why.append(f"Spot near put wall {m['support']}")
    if m["resistance"] - spot <= 1.5 * step:
        score -= 0.5; why.append(f"Spot near call wall {m['resistance']}")

    # 4. Max pain gravity (weak signal)
    if spot < m["max_pain"] - 2 * step:
        score += 0.5; why.append(f"Below max pain {m['max_pain']}")
    elif spot > m["max_pain"] + 2 * step:
        score -= 0.5; why.append(f"Above max pain {m['max_pain']}")

    # 5. Short-term momentum from recent polls
    mom = momentum()
    if mom > 0.3 * step:
        score += 1; why.append(f"Momentum +{mom:.0f} pts")
    elif mom < -0.3 * step:
        score -= 1; why.append(f"Momentum {mom:.0f} pts")

    if score >= 2:
        side = "CE"
    elif score <= -2:
        side = "PE"
    else:
        return dict(action="NO TRADE", score=score, why=why)

    # Levels: expected move ~ ATM straddle price
    em = m["straddle"] or 2 * step
    if side == "CE":
        idx_sl = max(m["support"] - step * 0.2, spot - 0.4 * em)
        idx_tp = min(m["resistance"], spot + 0.8 * em)
        prem, k = m["ce_ltp"], m["atm"]
        risk, reward = spot - idx_sl, idx_tp - spot
    else:
        idx_sl = min(m["resistance"] + step * 0.2, spot + 0.4 * em)
        idx_tp = max(m["support"], spot - 0.8 * em)
        prem, k = m["pe_ltp"], m["atm"]
        risk, reward = idx_sl - spot, spot - idx_tp

    rr = reward / risk if risk > 0 else 0
    if rr < MIN_RR:
        why.append(f"Reward:risk {rr:.1f} < {MIN_RR}, skipping")
        return dict(action="NO TRADE", score=score, why=why)

    return dict(
        action=f"BUY {k} {side}", score=score, why=why, prem=prem,
        prem_sl=round(prem * 0.7, 2), prem_tp=round(prem * 1.5, 2),
        idx_sl=round(idx_sl), idx_tp=round(idx_tp), rr=rr,
    )


def show(m, d):
    now = datetime.now().strftime("%H:%M:%S")
    print("\n" + "=" * 56)
    print(f"[{now}] Spot {m['spot']:.1f} | ATM {m['atm']} | Expiry {m['expiry']}")
    print(f"Support {m['support']} | Resistance {m['resistance']} | "
          f"MaxPain {m['max_pain']} | ATM IV {m['atm_iv']:.1f}")
    print(f"Score {d['score']:+.1f}  ->  {d['action']}")
    if d["action"] != "NO TRADE":
        print(f"  Entry premium ~{d['prem']}  SL {d['prem_sl']}  "
              f"Target {d['prem_tp']}")
        print(f"  Index SL {d['idx_sl']}  Index target {d['idx_tp']}  "
              f"R:R {d['rr']:.1f}")
        print("  Exit at SL on whichever hits first; square off before 3:15pm.")
    for w in d["why"]:
        print("   -", w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--every", type=int, default=60, help="seconds between polls")
    args = ap.parse_args()

    session = new_session()
    while True:
        try:
            m = analyse(fetch(session, args.symbol), args.symbol)
            history.append((time.time(), m["spot"]))
            show(m, decide(m))
        except Exception as e:  # NSE often drops sessions; rebuild and retry
            print(f"Fetch failed ({e}); refreshing session...")
            try:
                session = new_session()
            except Exception:
                pass
        time.sleep(args.every)


if __name__ == "__main__":
    main()
