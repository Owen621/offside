"""ODDS: does the 'beat the bookmakers with their own numbers' edge hold on free historical data?

    python odds.py selftest
    python odds.py backtest                      about 180 small files, a minute or two, then the report
    python odds.py backtest --from 2019          only seasons starting 2019 or later

Python 3.8+, no installs, no key. Data: football-data.co.uk (free CSV files of match results and the odds
many bookmakers offered before kick-off). Files are cached in the folder odds_data.

The idea (Kaunitz, Zhong and Kreiner, 2017)
  Bookmakers are collectively very good at pricing a match. So take the AVERAGE of all their odds as the fair
  chance, and bet only when one bookmaker's odds are clearly more generous than that. No prediction model.

What this tests
  PAPER RULE  exactly as published: chance = 1 / average odds, minus a fixed 5 points. Bet the BEST odds on
          offer when they beat 1 / that chance. Subtracting a fixed amount (instead of scaling) matters:
          bookmakers hide most of their margin in longshots, so scaling makes longshots look like bargains.
  PAPER RULE AT ONE BOOKMAKER  the same test, but only betting Bet365's price. One ordinary account.
  SHARP RULE  fair chance from one sharp bookmaker (Pinnacle) with its margin removed by the power method,
          bets at Bet365 when its price beats fair by a margin.
  NAIVE RULE  the first version of this file (margin removed by simple scaling). Kept to show why it fails.
  BASELINE  betting everything at average odds, which should lose the bookmakers' margin.

Honest limits
  * The paper's authors were profitable with real money and then had their accounts limited to tiny stakes.
    That restriction, not the maths, is the practical ceiling. This file cannot test it.
  * The file's odds are one snapshot before kick-off. A real bet needs that price to still be there.
  * This is betting. It is regulated differently in each country and it can lose money.
"""
import argparse
import csv
import io
import math
import os
import random
import sys
import urllib.request

LEAGUES = ["E0", "E1", "E2", "E3", "SC0", "D1", "D2", "I1", "I2", "SP1", "SP2", "F1", "F2", "N1", "B1", "P1", "T1", "G1"]
TOP = {"E0", "D1", "I1", "SP1", "F1"}
DATA_DIR = "odds_data"
NAMED = ["B365", "BW", "IW", "PS", "WH", "VC", "LB", "SJ", "BF", "1XB", "BMGM", "BV", "CL", "GB", "BS", "SB", "SO", "SY"]


def season_codes(first, last):
    return ["%02d%02d" % (y % 100, (y + 1) % 100) for y in range(first, last + 1)]


def fetch(code, league):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "%s_%s.csv" % (code, league))
    if os.path.exists(path) and os.path.getsize(path) > 200:
        with open(path, "rb") as f:
            return f.read()
    url = "https://www.football-data.co.uk/mmz4281/%s/%s.csv" % (code, league)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=30).read()
    with open(path, "wb") as f:
        f.write(raw)
    return raw


def fnum(x):
    try:
        v = float(x)
        return v if v > 1.0 else None
    except (TypeError, ValueError):
        return None


def parse(raw, code, league):
    """One row per match: result plus average, best, Pinnacle and Bet365 odds for home / draw / away."""
    try: text = raw.decode("utf-8-sig")
    except UnicodeDecodeError: text = raw.decode("latin-1")
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        res = (r.get("FTR") or "").strip()
        if res not in ("H", "D", "A"):
            continue
        def trio(*prefixes):
            for p in prefixes:
                v = [fnum(r.get(p + s)) for s in ("H", "D", "A")]
                if all(v): return v
            return None
        avg, mx = trio("Avg", "BbAv"), trio("Max", "BbMx")
        if not avg or not mx:
            continue
        d = (r.get("Date") or "").strip().split("/")
        date = ""
        if len(d) == 3 and all(x.isdigit() for x in d):
            yr = int(d[2]); yr += 2000 if yr < 100 else 0
            date = "%04d-%02d-%02d" % (yr, int(d[1]), int(d[0]))
        books = [t for t in (trio(b) for b in NAMED) if t]
        named = [max(b[i] for b in books) for i in range(3)] if len(books) >= 3 else None
        out.append({"season": code, "league": league, "y": "HDA".index(res), "avg": avg, "max": mx,
                    "pin": trio("PS", "P"), "b365": trio("B365"), "named": named, "n_books": len(books), "date": date})
    return out


def fair(odds):
    """Chances implied by a set of odds, with the bookmaker's margin removed."""
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def fair_power(odds):
    """Remove the margin with the power method: find k so that the chances (1/odds)^k add up to 1.
    Unlike simple scaling, this takes more of the margin out of longshots, where bookmakers put it."""
    inv = [1.0 / o for o in odds]
    lo, hi = 0.5, 3.0
    for _ in range(60):
        k = (lo + hi) / 2
        if sum(x ** k for x in inv) > 1.0: lo = k
        else: hi = k
    return [x ** ((lo + hi) / 2) for x in inv]


def bets_paper(matches, alpha=0.05, book="max"):
    """The published rule: bet when the price beats 1 / (1/average odds - alpha)."""
    out = []
    for m in matches:
        prices = m.get(book)
        if not prices:
            continue
        for i in range(3):
            pr = 1.0 / m["avg"][i] - alpha
            if pr > 0.02 and prices[i] > 1.0 / pr:
                out.append((m, i, prices[i]))
    return out


def bets_sharp(matches, margin):
    out = []
    for m in matches:
        if not m["pin"] or not m["b365"]:
            continue
        pr = fair_power(m["pin"])
        for i in range(3):
            if m["b365"][i] * pr[i] >= 1.0 + margin:
                out.append((m, i, m["b365"][i]))
    return out


def bets_rule1(matches, margin):
    out = []
    for m in matches:
        p = fair(m["avg"])
        for i in range(3):
            if m["max"][i] * p[i] >= 1.0 + margin:
                out.append((m, i, m["max"][i]))
    return out


def bets_rule2(matches, margin):
    out = []
    for m in matches:
        if not m["pin"] or not m["b365"]:
            continue
        p = fair(m["pin"])
        for i in range(3):
            if m["b365"][i] * p[i] >= 1.0 + margin:
                out.append((m, i, m["b365"][i]))
    return out


def stats(bets):
    """Return per unit staked, with a t-statistic."""
    if not bets:
        return None
    r = [(o - 1.0) if m["y"] == i else -1.0 for m, i, o in bets]
    n = len(r); mu = sum(r) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in r) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "roi": mu, "win": sum(1 for x in r if x > 0) / float(n),
            "t": mu / (sd / math.sqrt(n)) if sd > 0 else None, "avg_odds": sum(o for _, _, o in bets) / n}


def line(label, s):
    if not s:
        return "  %-34s no bets" % label
    return "  %-34s %7d bets   return %+6.2f%%   wins %4.1f%%   avg odds %5.2f   t=%s" % (
        label, s["n"], 100 * s["roi"], 100 * s["win"], s["avg_odds"], ("%.2f" % s["t"]) if s["t"] is not None else "n/a")


def report(matches):
    seasons = sorted({m["season"] for m in matches}, key=lambda c: (int(c[:2]) + (100 if int(c[:2]) < 50 else 0)))
    half = seasons[len(seasons) // 2]
    older = [m for m in matches if seasons.index(m["season"]) < seasons.index(half)]
    newer = [m for m in matches if seasons.index(m["season"]) >= seasons.index(half)]
    out = ["", "ODDS-CONSENSUS TEST: %d matches, %d leagues, seasons %s to %s" % (
        len(matches), len({m["league"] for m in matches}), seasons[0], seasons[-1]), ""]
    out.append("BASELINE (what the bookmakers keep)")
    out.append(line("every outcome at average odds", stats([(m, i, m["avg"][i]) for m in matches for i in range(3)])))

    def block(title, fn, settings, label):
        out.extend(["", title])
        for s in settings:
            out.append(line("%s, all seasons" % label(s), stats(fn(matches, s))))
            out.append(line("%s, older (to %s)" % (label(s), seasons[seasons.index(half) - 1]), stats(fn(older, s))))
            out.append(line("%s, newer (from %s)" % (label(s), half), stats(fn(newer, s))))
        mid = settings[len(settings) // 2]
        out.append(line("%s, top five leagues" % label(mid), stats(fn([m for m in matches if m["league"] in TOP], mid))))
        out.append(line("%s, other leagues" % label(mid), stats(fn([m for m in matches if m["league"] not in TOP], mid))))
        b = fn(matches, mid)
        for lo, hi, name in ((1.0, 2.0, "odds under 2"), (2.0, 3.0, "odds 2 to 3"), (3.0, 5.0, "odds 3 to 5"), (5.0, 1e9, "odds 5 and over")):
            out.append(line("%s, %s" % (label(mid), name), stats([x for x in b if lo <= x[2] < hi])))
        return mid

    a_lab = lambda a: "minus %.0f points" % (100 * a)
    m_lab = lambda m: "margin %.0f%%" % (100 * m)
    block("PAPER RULE: best odds on offer vs 1/average minus a fixed amount  (needs many bookmaker accounts)",
          lambda ms, a: bets_paper(ms, a, "max"), (0.03, 0.05, 0.07), a_lab)
    nb = [m["n_books"] for m in matches if m.get("named")]
    block("PAPER RULE AT NAMED BOOKMAKERS: best price among the mainstream bookmakers listed in the data, no exchanges"
          "  (about %d accounts)" % (round(sum(nb) / float(len(nb))) if nb else 0),
          lambda ms, a: bets_paper(ms, a, "named"), (0.03, 0.04, 0.05), a_lab)
    block("PAPER RULE AT ONE BOOKMAKER: the same test, betting only Bet365's price  (one ordinary account)",
          lambda ms, a: bets_paper(ms, a, "b365"), (0.02, 0.03, 0.05), a_lab)
    block("SHARP RULE: Bet365 price vs Pinnacle's fair chance (power method)  (one ordinary account)",
          bets_sharp, (0.02, 0.05, 0.08), m_lab)
    out += ["", "NAIVE RULE (the first version: margin removed by simple scaling, which over-rates longshots)"]
    out.append(line("best odds, margin 5%", stats(bets_rule1(matches, 0.05))))
    for a, book, name in ((0.03, "max", "best odds"), (0.05, "max", "best odds"), (0.03, "named", "named bookmakers")):
        out += ["", "PAPER RULE (minus %.0f points, %s), season by season" % (100 * a, name)]
        for s in seasons:
            out.append(line("season " + s, stats(bets_paper([m for m in matches if m["season"] == s], a, book))))
    out += ["", "How to read this: the baseline should lose a few percent. A rule shows an edge if its return is",
            "positive in BOTH the older and newer seasons, with t above about 2, and holds across most single seasons.",
            "The 'best odds' column in this data can include prices from betting exchanges and small bookmakers that",
            "are hard to actually get, so the one-bookmaker rules are the more believable ones.",
            "Remember the practical ceiling: bookmakers limit accounts that win this way."]
    return "\n".join(out)


def selftest():
    C = []
    C.append(("season codes are built correctly", season_codes(2019, 2021) == ["1920", "2021", "2122"] and season_codes(1999, 2000) == ["9900", "0001"]))
    f = fair([2.0, 4.0, 4.0])
    C.append(("margin is removed from odds", abs(sum(f) - 1) < 1e-12 and abs(f[0] - 0.5) < 1e-12))
    raw = ("Div,Date,HomeTeam,AwayTeam,FTR,B365H,B365D,B365A,PSH,PSD,PSA,MaxH,MaxD,MaxA,AvgH,AvgD,AvgA\n"
           "E0,01/08/21,A,B,H,2.10,3.40,3.60,2.05,3.50,3.70,2.20,3.60,3.90,2.04,3.42,3.63\n"
           "E0,02/08/21,C,D,X,2,3,4,2,3,4,2,3,4,2,3,4\n"
           "E0,03/08/21,E,F,A,1.50,4.00,,1.5,4,7,1.6,4.3,7.5,1.5,4.1,7.0\n").encode()
    rows = parse(raw, "2122", "E0")
    C.append(("matches without a result are dropped, missing odds tolerated", len(rows) == 2 and rows[0]["y"] == 0 and rows[1]["b365"] is None))
    old = "Div,Date,HomeTeam,AwayTeam,FTR,BbMxH,BbMxD,BbMxA,BbAvH,BbAvD,BbAvA\nE0,01/08/15,A,B,D,2.2,3.6,3.9,2.04,3.42,3.63\n".encode()
    C.append(("older files with different column names are read", len(parse(old, "1516", "E0")) == 1))
    m = {"y": 0, "avg": [2.0, 4.0, 4.0], "max": [2.3, 4.0, 4.1], "pin": [2.0, 4.0, 4.0], "b365": [2.2, 3.9, 3.9], "season": "2122", "league": "E0",
         "named": [2.25, 4.0, 4.0], "n_books": 5}
    b1, b2 = bets_rule1([m], 0.05), bets_rule2([m], 0.05)
    C.append(("only the clearly generous price is bet", len(b1) == 1 and b1[0][1] == 0 and len(b2) == 1 and b2[0][2] == 2.2))
    C.append(("no bet when nothing beats the margin", bets_rule1([dict(m, max=[2.05, 4.1, 4.1])], 0.05) == []))
    s = stats([(m, 0, 2.3), (dict(m, y=1), 0, 2.3)])
    C.append(("return is profit per unit staked", abs(s["roi"] - 0.15) < 1e-12 and s["win"] == 0.5))
    # simulated world: bookmakers price accurately on average, but each one is a little noisy
    rng = random.Random(4); sim = []
    for k in range(20000):
        a = rng.uniform(0.25, 0.6); d = rng.uniform(0.2, 0.3); p = [a, d, 1 - a - d]
        books = [[1.0 / (pi * 1.05 * math.exp(rng.gauss(0, 0.04))) for pi in p] for _ in range(12)]
        u = rng.random(); y = 0 if u < p[0] else (1 if u < p[0] + p[1] else 2)
        sim.append({"y": y, "avg": [sum(b[i] for b in books) / 12 for i in range(3)], "max": [max(b[i] for b in books) for i in range(3)],
                    "pin": [1.0 / (pi * 1.02) for pi in p], "b365": books[0], "season": "2122" if k % 2 else "2021", "league": "E0",
                    "named": [max(b[i] for b in books[:5]) for i in range(3)], "n_books": 5})
    base = stats([(x, i, x["avg"][i]) for x in sim for i in range(3)])
    r1, r2 = stats(bets_rule1(sim, 0.03)), stats(bets_rule2(sim, 0.03))
    C.append(("in a simulated market, blind betting loses the margin", base["roi"] < -0.02))
    C.append(("and both rules find the noisy generous prices", r1["roi"] > 0.01 and r2["roi"] > 0.01 and r1["t"] > 2))
    fp = fair_power([1.25, 6.0, 12.0]); fs = fair([1.25, 6.0, 12.0])
    C.append(("the power method takes more margin out of longshots than scaling does", abs(sum(fp) - 1) < 1e-9 and fp[2] < fs[2] and fp[0] > fs[0]))
    pb = bets_paper([m], 0.05, "max")
    C.append(("the published rule bets a favourite priced well above the average", len(pb) == 1 and pb[0][1] == 0))
    longshot = dict(m, avg=[1.3, 5.5, 10.0], max=[1.33, 5.9, 12.5])
    C.append(("and ignores a longshot that only looks generous", bets_paper([longshot], 0.05, "max") == [] and len(bets_rule1([longshot], 0.05)) == 1))
    rp = stats(bets_paper(sim, 0.03, "max"))
    C.append(("the published rule is profitable in the simulated market, betting mostly favourites", rp["roi"] > 0.01 and rp["win"] > 0.4 and rp["t"] > 2))
    txt = report(sim)
    full = ("Div,Date,HomeTeam,AwayTeam,FTR,B365H,B365D,B365A,BWH,BWD,BWA,WHH,WHD,WHA,BFEH,BFED,BFEA,MaxH,MaxD,MaxA,AvgH,AvgD,AvgA\n"
            "E0,01/08/24,A,B,H,2.10,3.40,3.60,2.15,3.30,3.50,2.05,3.50,3.70,2.40,3.9,4.2,2.40,3.9,4.2,2.08,3.4,3.6\n").encode()
    fr = parse(full, "2425", "E0")[0]
    C.append(("best named-bookmaker price ignores the exchange and the max column", fr["named"] == [2.15, 3.50, 3.70] and fr["n_books"] == 3))
    C.append(("match dates are read in both short and long year formats", fr["date"] == "2024-08-01" and rows[0]["date"] == "2021-08-01"))
    C.append(("the report splits older and newer seasons", "older (to" in txt and "newer (from" in txt and "season 2021" in txt and "odds 2 to 3" in txt))
    ok = True
    for name, passed in C:
        print("  %s  %s" % ("PASS" if passed else "FAIL", name)); ok = ok and bool(passed)
    print("\nSELF-TEST %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Odds-consensus edge test")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    p = sub.add_parser("backtest")
    p.add_argument("--from", dest="first", type=int, default=2015, help="first season start year")
    p.add_argument("--to", dest="last", type=int, default=2026, help="last season start year")
    a = ap.parse_args()
    if a.cmd == "selftest":
        sys.exit(selftest())
    if a.cmd != "backtest":
        ap.print_help(); return
    matches, missing = [], 0
    for code in season_codes(a.first, a.last):
        got = 0
        for lg in LEAGUES:
            try:
                rows = parse(fetch(code, lg), code, lg)
            except Exception:
                missing += 1; continue
            matches += rows; got += len(rows)
        print("  season %s: %d matches" % (code, got))
    if not matches:
        sys.exit("No data could be downloaded. Check the connection, or send this output back.")
    print("  (%d league files were unavailable and skipped)" % missing)
    text = report(matches); print(text)
    with open("odds_backtest.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print("\nsaved odds_backtest.txt")


if __name__ == "__main__":
    main()
