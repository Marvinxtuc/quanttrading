"""BTC ma_ensemble_v1 basket: stop-rule variants (research only). 60 bps + 5 bps slip.
none: no stop. hard: total DD>=60% -> all cash forever. reentry: total DD>=60% -> all cash,
each sleeve re-buys only after a fresh close crossing above its own SMA (close<=SMA then close>SMA); peak resets."""
import json, sys
from datetime import datetime, timezone
from pathlib import Path
import quanttrading.oos.report as R
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/workspace/kraken_dl/XBTUSD_60.csv")
OUT = Path(__file__).parent / (sys.argv[2] if len(sys.argv) > 2 else "results.json")
END = sys.argv[3] if len(sys.argv) > 3 else None
HO = datetime(2024, 1, 1, tzinfo=timezone.utc); PER = 0.006 + 0.0005; STOP = 0.60
days = {}
for line in SRC.open():
    p = line.split(","); ts = datetime.fromtimestamp(int(float(p[0])), tz=timezone.utc); d = ts.date()
    if END and str(d) > END: continue
    o, c = float(p[1]), float(p[4])
    if d not in days: days[d] = [o, c]
    else: days[d][1] = c
D = sorted(days); O = [days[d][0] for d in D]; C = [days[d][1] for d in D]; n = len(D)
class B:
    def __init__(s, d): s.ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
bars = [B(d) for d in D]; ho = next(i for i, b in enumerate(bars) if b.ts >= HO)
folds = R.build_expanding_folds(n, holdout_frac=(n - ho) / n)
folds = [f if f.kind != "holdout" else R.FoldWindow("holdout", "holdout", ho, ho, n) for f in folds]
f = folds[-2]; folds[-2] = R.FoldWindow(f.fold_id, f.kind, f.train_end, f.test_start, ho)
LS = (150, 200, 250)
def sig(L, j): return None if j < L - 1 else C[j] > sum(C[j - L + 1:j + 1]) / L
def sim(mode, a, b):
    cash = [1/3]*3; q = [0.0]*3; eq = []; pk = None; stops = []; halted = False; blocked = [False]*3; ntr = 0
    for i in range(a, b):
        px = O[i]
        for s, L in enumerate(LS):
            up = sig(L, i - 1)
            if blocked[s]:
                if up is False: blocked[s] = "armed"
                if up and blocked[s] == "armed": blocked[s] = False
            want = 0 if (halted or blocked[s] or not up) else 1
            if want and q[s] == 0 and cash[s] > 0: q[s] = cash[s]*(1-PER)/px; cash[s] = 0; ntr += 1
            elif not want and q[s] > 0: cash[s] += q[s]*px*(1-PER); q[s] = 0; ntr += 1
        v = sum(cash[s] + q[s]*C[i] for s in range(3))
        pk = v if pk is None else max(pk, v)
        if mode != "none" and not halted and 1 - v/pk >= STOP:
            stops.append(str(D[i]))
            for s in range(3):  # liquidate at this close (approx.)
                if q[s] > 0: cash[s] += q[s]*C[i]*(1-PER); q[s] = 0; ntr += 1
            v = sum(cash); pk = v
            if mode == "hard": halted = True
            else: blocked = ["armed" if sig(LS[s], i) is False else True for s in range(3)]
        eq.append(v)
    return eq, ntr, stops
def mdd(eq):
    pk, dd = eq[0], 0
    for v in eq: pk = max(pk, v); dd = max(dd, 1 - v/pk)
    return dd
res = {"data": [str(D[0]), str(D[-1]), n], "rows": []}
for f in folds:
    a, b = f.test_start, f.test_end
    bh = [C[i]/O[a]*(1-PER) for i in range(a, b)]
    row = {"fold": f.fold_id, "start": str(D[a]), "end": str(D[b-1]), "bh_ret": round(bh[-1]-1, 4), "bh_mdd": round(mdd(bh), 4)}
    for m in ("none", "hard", "reentry"):
        eq, ntr, st = sim(m, a, b)
        row[m] = {"ret": round(eq[-1]-1, 4), "mdd": round(mdd(eq), 4), "trades": ntr, "stops": st}
    res["rows"].append(row)
OUT.write_text(json.dumps(res, indent=1)); print(json.dumps(res, indent=1))
