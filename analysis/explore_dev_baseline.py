"""Exploratory look at DEVELOPMENT data only. Not the engine; nothing here touches the holdout."""
import glob, json, math, statistics, sys
from decimal import Decimal
root = sys.argv[1]
rows = []
for d in sorted(glob.glob(f"{root}/history/development/*/manifest.json")):
    man = json.load(open(d)); base = d.rsplit("/", 1)[0]
    for key, info in man["cities"].items():
        if info.get("status") == "missing": continue
        mk = json.load(open(f"{base}/{key}/markets.json"))["markets"]
        nbm = json.load(open(f"{base}/{key}/nbm.json"))
        settled = Decimal(mk[0]["expiration_value"])
        qs = info["quotes"]
        if any(qs[m["ticker"]] is None for m in mk): continue
        mids = {m["ticker"]: (Decimal(qs[m["ticker"]]["yes_bid_close_dollars"]) + Decimal(qs[m["ticker"]]["yes_ask_close_dollars"])) / 2 for m in mk}
        fmax = max(h["temperature_c"] for h in nbm["hours"]) * 9 / 5 + 32
        rows.append(dict(date=man["target_date"], city=key, markets=mk, mids=mids, settled=float(settled), fmax=fmax,
                         winner=[m["ticker"] for m in mk if m["result"] == "yes"][0]))

def in_range(m, v):
    k, lo, hi = m["strike_type"], m.get("floor_strike"), m.get("cap_strike")
    return v < hi if k == "less" else v > lo if k == "greater" else lo <= v <= hi

def prob(m, mu, sd):
    cdf = lambda x: 0.5 * (1 + math.erf((x - mu) / (sd * math.sqrt(2))))
    k, lo, hi = m["strike_type"], m.get("floor_strike"), m.get("cap_strike")
    if k == "less": return cdf(hi - 0.5)
    if k == "greater": return 1 - cdf(lo + 0.5)
    return cdf(hi + 0.5) - cdf(lo - 0.5)

n = len(rows)
fav_hits = sum(max(r["mids"], key=r["mids"].get) == r["winner"] for r in rows)
raw_hits = sum(next((m["ticker"] for m in r["markets"] if in_range(m, round(r["fmax"]))), None) == r["winner"] for r in rows)
print(f"city-days with a quote on every range: {n}  ({len({r['date'] for r in rows})} dates)")
print(f"market's favourite range won:      {fav_hits}/{n}")
print(f"raw forecast's range won:          {raw_hits}/{n}")
print("\nforecast error by city (settled high minus raw forecast max, F):")
for c in ("NY", "CHI", "MIA", "DEN", "LAX"):
    e = [r["settled"] - r["fmax"] for r in rows if r["city"] == c]
    print(f"  {c:4s} n={len(e):2d}  mean {statistics.mean(e):+5.2f}  spread(sd) {statistics.pstdev(e):4.2f}")

# Leave-one-date-out: each date's probabilities use bias and spread fitted on the OTHER dates only.
model_b, market_b, wins = [], [], 0
for r in rows:
    others = [o["settled"] - o["fmax"] for o in rows if o["city"] == r["city"] and o["date"] != r["date"]]
    mu, sd = r["fmax"] + statistics.mean(others), max(statistics.pstdev(others), 1.0)
    out = {m["ticker"]: 1.0 if m["ticker"] == r["winner"] else 0.0 for m in r["markets"]}
    mb = statistics.mean((prob(m, mu, sd) - out[m["ticker"]]) ** 2 for m in r["markets"])
    kb = statistics.mean((float(r["mids"][m["ticker"]]) - out[m["ticker"]]) ** 2 for m in r["markets"])
    model_b.append(mb); market_b.append(kb); wins += mb < kb
print(f"\nBrier score (lower is better), leave-one-date-out simple model vs market midpoints:")
print(f"  simple model {statistics.mean(model_b):.4f}   market {statistics.mean(market_b):.4f}   model better on {wins}/{n} city-days")
