import json, math
from datetime import datetime, timezone, timedelta
from pathlib import Path
import quanttrading.oos.report as R

SRC = Path("/workspace/eth_dl/bitstamp_ethusd_1d.csv")
OUT = Path("/workspace/quanttrading/reports/eth_oos")
HO = datetime(2024, 1, 1, tzinfo=timezone.utc)
SLIP = 0.0005
COSTS = [0.003, 0.004, 0.006]

# --- daily bars (UTC) from Bitstamp ETHUSD 1d (Kraken full archive not available locally)
import os
END = os.environ.get("END_DATE")
days = {}
for line in SRC.open():
    p = line.split(",")
    ts = datetime.fromtimestamp(int(p[0]), tz=timezone.utc); d = ts.date()
    if END and str(d) > END: continue
    o, h, l, c = map(float, p[1:5]); days[d] = [ts, o, h, l, c]
D = sorted(days); O = [days[d][1] for d in D]; C = [days[d][4] for d in D]
n = len(D)
class B:  # minimal for fold labels
    def __init__(s, d): s.ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
bars = [B(d) for d in D]
ho = next(i for i, b in enumerate(bars) if b.ts >= HO)
folds = R.build_expanding_folds(n, holdout_frac=(n - ho) / n)
folds = [f if f.kind != "holdout" else R.FoldWindow("holdout", "holdout", ho, ho, n) for f in folds]
f = folds[-2]; folds[-2] = R.FoldWindow(f.fold_id, f.kind, f.train_end, f.test_start, ho)

def target_series(kind, a, b):
    """target weight decided at close of day i-1, executed at open of day i; returns list for days a..b-1"""
    w = []
    if kind.startswith("sma"):
        L = int(kind[3:])
        for i in range(a, b):
            j = i - 1
            if j < L - 1: w.append(0.0); continue
            sma = sum(C[j - L + 1:j + 1]) / L
            w.append(1.0 if C[j] > sma else 0.0)
    return w

def simulate(kind, a, b, cost):
    """window [a,b): start all cash at open of a. Returns equity (at closes), trades list(pnl net), n_trades."""
    per = cost + SLIP
    cash, btc = 1.0, 0.0
    eq = []; trades = []; ntr = 0
    tw = target_series(kind, a, b) if kind.startswith("sma") else None
    nweeks = sum(1 for i in range(a, b) if D[i].weekday() == 0) or 1
    tranche = 1.0 / nweeks
    cur_w = 0.0; entry_val = None; last_val = None
    for k, i in enumerate(range(a, b)):
        px = O[i]; val = cash + btc * px
        new_w = None
        if kind.startswith("sma"):
            new_w = tw[k]
        elif kind == "voltarget":
            if D[i].weekday() == 0 or k == 0:
                j = i - 1
                if j >= 31:
                    rets = [math.log(C[t] / C[t - 1]) for t in range(j - 29, j + 1)]
                    m = sum(rets) / 30
                    vol = math.sqrt(sum((r - m) ** 2 for r in rets) / 29) * math.sqrt(365)
                    tgt = min(1.0, 0.40 / vol) if vol > 0 else 1.0
                    cw = btc * px / val if val else 0
                    if abs(tgt - cw) >= 0.10 or k == 0: new_w = tgt
        elif kind == "dca":
            if D[i].weekday() == 0 and cash > 1e-12:
                amt = min(tranche, cash); cash -= amt; btc += amt * (1 - per) / px; ntr += 1
        elif kind == "bh":
            if k == 0: btc = cash * (1 - per) / px; cash = 0; ntr = 1
        if new_w is not None:
            cw = btc * px / val if val else 0
            if abs(new_w - cw) > 1e-9:
                trade = (new_w - cw) * val
                if trade > 0: cash -= trade; btc += trade * (1 - per) / px
                else: btc += trade / px; cash += -trade * (1 - per)
                ntr += 1
                val2 = cash + btc * px
                if kind.startswith("sma"):
                    if new_w > 0: entry_val = val
                    else: trades.append(val2 / entry_val - 1); entry_val = None
                elif kind == "voltarget":
                    if last_val is not None: trades.append(val / last_val - 1)
                    last_val = val2
        eq.append(cash + btc * C[i])
    if kind.startswith("sma") and entry_val is not None:  # mark open trade at end (net of exit cost)
        trades.append(eq[-1] * (1 - per) / entry_val - 1)
    if kind == "voltarget" and last_val is not None: trades.append(eq[-1] / last_val - 1)
    return eq, trades, ntr


def simulate_basket(a, b, cost, stop=0.60):
    sl = [simulate(k, a, b, cost) for k in ("sma150", "sma200", "sma250")]
    eq = [sum(s[0][i] for s in sl) / 3 for i in range(b - a)]
    trades = [t for s in sl for t in s[1]]; ntr = sum(s[2] for s in sl)
    pk = eq[0]; halted = None
    for i, v in enumerate(eq):
        pk = max(pk, v)
        if 1 - v / pk >= stop: halted = i; break
    if halted is not None:
        frozen = eq[halted] * (1 - cost - SLIP); eq = eq[:halted + 1] + [frozen] * (len(eq) - halted - 1)
    return eq, trades, ntr

_simulate = simulate
def simulate(kind, a, b, cost):
    return simulate_basket(a, b, cost) if kind == "basket" else _simulate(kind, a, b, cost)

def mdd(eq):
    pk, dd = eq[0], 0.0
    for v in eq: pk = max(pk, v); dd = max(dd, 1 - v / pk)
    return dd

def pf(tr):
    g = sum(x for x in tr if x > 0); l = -sum(x for x in tr if x < 0)
    return round(g / l, 3) if l > 0 else (None if g > 0 else 0.0)

STRATS = ["basket", "sma200", "sma150", "sma250", "dca", "voltarget"]
res = {"data": {"source": "Bitstamp ETHUSD daily (UTC); cross-checked vs Kraken XETHZUSD last 721 days: close median diff 0.013%, max 0.15%", "n_days": n,
        "start": str(D[0]), "end": str(D[-1])}, "folds": [f.label(bars) for f in folds], "results": {}}
for cost in COSTS:
    ck = f"{int(cost*1e4)}bps"
    res["results"][ck] = {}
    for s in STRATS + ["bh"]:
        rows = []
        for f in folds:
            eq, tr, ntr = simulate(s, f.test_start, f.test_end, cost)
            beq, _, _ = simulate("bh", f.test_start, f.test_end, cost)
            rows.append(dict(fold=f.fold_id, start=str(D[f.test_start]), end=str(D[f.test_end-1]), trades=ntr,
                ret=round(eq[-1] - 1, 4), bh_ret=round(beq[-1] - 1, 4), maxdd=round(mdd(eq), 4),
                bh_maxdd=round(mdd(beq), 4), pf=None if s in ("dca", "bh") else pf(tr), n_closed=len(tr)))
        h = rows[-1]
        crit = None
        if s != "bh":
            a = h["ret"] >= 0.7 * h["bh_ret"] if h["bh_ret"] > 0 else h["ret"] >= h["bh_ret"]
            b = h["maxdd"] <= h["bh_maxdd"] * (2 / 3) and h["maxdd"] <= 0.35
            c = (h["pf"] is None or h["pf"] > 1.2) and all(r["ret"] >= 0 for r in rows)
            crit = dict(a=a, b=b, c=c, PASS=a and b and c)
        res["results"][ck][s] = dict(folds=rows, criteria=crit)
(OUT / os.environ.get("OUT_NAME","results.json")).write_text(json.dumps(res, indent=1))
print(json.dumps(res["data"]), json.dumps(res["folds"]))
for ck, blk in res["results"].items():
    for s, v in blk.items():
        print(ck, s, v["criteria"])
        for r in v["folds"]: print("   ", r)
