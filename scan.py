"""SCAN: live odds scanner for the consensus rule that held up in odds.py.

    python scan.py selftest
    python scan.py find "England League One"       ask Context.dev for odds-comparison pages of upcoming matches
    python scan.py match URL [URL ...]             read each page now and flag outlier prices
    python scan.py match URL --watch 5             re-read every 5 minutes and report changes (Ctrl+C to stop)
    python scan.py parse saved_page.md             run the parser on a saved page (for debugging)

Needs CONTEXT_DEV_API_KEY (not for selftest or parse). Python 3.8+, no installs.

How it works
  1. Context.dev fetches the page fresh and returns it as text.
  2. The parser finds every line that names a bookmaker and gives three decimal prices (home, draw, away).
     A line only counts if the three prices form a believable market (the implied chances add up to 97-120%).
  3. Consensus = the average price across all bookmakers found.
  4. The rule from the backtest: chance = 1 / average price, minus a fixed 3, 4 or 5 points. A bookmaker's price
     is flagged when it is above 1 / that chance. Prices of 5.0 and over are ignored (they lost in the backtest).

This flags prices; it does not place bets. Bookmakers limit accounts that bet this way, an outlier can be a
stale or mistaken price that is voided, and betting is regulated differently in each country.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

CTX_SCRAPE = "https://api.context.dev/v1/web/scrape"
CTX_ANSWERS = "https://api.context.dev/v1/web/answers"
ALPHAS = (0.05, 0.04, 0.03)
ODDS = re.compile(r"(?<![\d.])(\d{1,2}\.\d{2})(?![\d.])")
MIN_BOOKS = 5


def http(url, body, key, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def scrape(key, url):
    """Fresh page text through Context.dev, or (None, reason)."""
    body = {"url": url, "formats": {"markdown": True}, "maxAgeMs": 0}
    for _ in range(4):
        try:
            res = http(CTX_SCRAPE, body, key)
            md = res.get("markdown") or {}
            if md.get("success"):
                return md.get("data") or "", None
            return None, md.get("message") or md.get("error_code") or "page could not be read"
        except urllib.error.HTTPError as e:
            if e.code == 400 and "maxAgeMs" in body:
                body.pop("maxAgeMs"); continue
            if e.code == 429:
                time.sleep(max(2.0, float(e.headers.get("Retry-After") or 5))); continue
            return None, "HTTP %d %s" % (e.code, e.read().decode()[:160])
        except Exception as e:
            return None, str(e)
    return None, "gave up after retries"


def find_pages(key, competition):
    task = ("Find odds-comparison web pages for upcoming football (soccer) matches in %s in the next three days. "
            "I need pages that show the home / draw / away decimal odds from MANY different bookmakers for ONE "
            "match in a table. Return up to 8 such match pages with the two team names and the kick-off date."
            % competition)
    shape = {"pages": [{"home": "", "away": "", "kickoff": "", "url": ""}]}
    res = http(CTX_ANSWERS, {"mode": "fast", "task": task, "json_format": shape,
                             "timeoutOpts": {"milliseconds": 30000, "behavior": "return-partial"}}, key, timeout=120)
    return [x for x in ((res.get("json_content") or {}).get("pages") or []) if (x.get("url") or "").startswith("http")]


def clean_name(text):
    no_img = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)              # drop images (logos)
    if not re.search(r"[A-Za-z]{2}", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", no_img)):
        no_img = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)      # a logo is the only name: use its alt text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", no_img)         # link -> its label
    text = re.sub(r"[|*_`#>\[\]()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -:.")
    return text


def parse_page(md):
    """{bookmaker: (home, draw, away)} for every believable three-price line on the page."""
    books = {}
    for line in (md or "").splitlines():
        nums = [(m.start(), float(m.group(1))) for m in ODDS.finditer(line)]
        if len(nums) < 3:
            continue
        for k in range(len(nums) - 2):                              # first believable run of three prices
            trio = [nums[k][1], nums[k + 1][1], nums[k + 2][1]]
            if not all(1.01 <= o <= 60 for o in trio):
                continue
            over = sum(1.0 / o for o in trio)
            if not 0.97 <= over <= 1.20:
                continue
            name = clean_name(line[:nums[k][0]])
            name = re.sub(r"\b\d+\b", "", name).strip()
            if len(name) < 2 or len(name) > 40 or not re.search(r"[A-Za-z]", name):
                break
            books.setdefault(name, tuple(trio))
            break
    return books


def analyse(books):
    """Consensus and flagged prices. Returns None if too few bookmakers were found."""
    if len(books) < MIN_BOOKS:
        return None
    n = float(len(books))
    avg = [sum(b[i] for b in books.values()) / n for i in range(3)]
    flags = []
    for i, label in enumerate(("home", "draw", "away")):
        for alpha in ALPHAS:                                         # strictest first: report each price once
            chance = 1.0 / avg[i] - alpha
            if chance <= 0.02:
                continue
            need = 1.0 / chance
            for name, trio in books.items():
                if need < trio[i] < 5.0 and not any(f["book"] == name and f["outcome"] == label for f in flags):
                    flags.append({"book": name, "outcome": label, "price": trio[i], "average": round(avg[i], 3),
                                  "needed": round(need, 3), "points": int(round(100 * alpha)),
                                  "edge_pct": round(100 * (trio[i] * chance - 1), 1)})
    flags.sort(key=lambda f: (-f["points"], -f["edge_pct"]))
    return {"books": len(books), "avg": [round(a, 3) for a in avg],
            "best": [max(b[i] for b in books.values()) for i in range(3)], "flags": flags}


def show(title, res, books):
    print("\n" + title)
    if res is None:
        print("  only %d bookmaker price lines found (need %d). The page may not be an odds table, or may not have"
              " loaded. Try `python scan.py parse` on a saved copy to see what was read." % (len(books), MIN_BOOKS))
        return
    print("  %d bookmakers.  average  home %.2f  draw %.2f  away %.2f   |  best  %.2f  %.2f  %.2f" % (
        res["books"], res["avg"][0], res["avg"][1], res["avg"][2], res["best"][0], res["best"][1], res["best"][2]))
    if not res["flags"]:
        print("  no price beats the rule right now")
    for f in res["flags"]:
        print("  FLAG  %-22s %-5s at %.2f  (average %.2f, rule needs above %.2f at minus %d points, edge %+.1f%%)" % (
            f["book"][:22], f["outcome"], f["price"], f["average"], f["needed"], f["points"], f["edge_pct"]))


def selftest():
    C = []
    page = """
# Leyton Orient vs Bolton - 1X2 odds
| Bookmaker | 1 | X | 2 | Payout |
|---|---|---|---|---|
| [bet365](https://x.example/b365) | 2.30 | 3.40 | 3.00 | 94.1% |
| ![logo](a.png) [William Hill](https://x.example/wh) | 2.25 | 3.30 | 3.10 | 93.6% |
| Unibet | 2.35 | 3.45 | 2.95 | 94.5% |
| Betfred | 2.30 | 3.40 | 3.00 | 94.1% |
| 10Bet | 2.28 | 3.35 | 3.05 | 93.8% |
| SlowBook | 2.75 | 3.30 | 2.90 | 97.9% |
Last 5 matches: 2.10 goals per game, 1.40 conceded, 12.50 corners
Kick-off 15.00 on 15.11.2026
"""
    b = parse_page(page)
    C.append(("six bookmakers are read from a table with links and logos", len(b) == 6 and b["bet365"] == (2.30, 3.40, 3.00) and "William Hill" in b))
    C.append(("a digit-led bookmaker name is kept and stats lines are ignored", "10Bet" in b and not any("goals" in k.lower() or "kick" in k.lower() for k in b)))
    r = analyse(b)
    C.append(("consensus is the average across bookmakers", abs(r["avg"][0] - (2.30 + 2.25 + 2.35 + 2.30 + 2.28 + 2.75) / 6) < 1e-3))
    fl = r["flags"]
    C.append(("the slow bookmaker's home price is the only flag", len(fl) == 1 and fl[0]["book"] == "SlowBook" and fl[0]["outcome"] == "home"))
    C.append(("the flag reports how strict a rule it passes", fl[0]["points"] == 5 and fl[0]["price"] > fl[0]["needed"]))
    C.append(("a bookmaker shown only as a logo is named from the logo", parse_page("| ![Betway](b.png) | 2.30 | 3.40 | 3.00 |") == {"Betway": (2.30, 3.40, 3.00)}))
    plain = "Bet365 2.30 3.40 3.00\nUnibet 2.35 3.45 2.95\nBwin 2.30 3.40 3.05\nBetway 2.25 3.50 3.00\nCoral 2.30 3.40 3.00\n"
    C.append(("plain text lines without table bars also work", len(parse_page(plain)) == 5 and analyse(parse_page(plain))["flags"] == []))
    C.append(("too few bookmakers gives no verdict", analyse({"A": (2.0, 3.4, 3.6)}) is None))
    long = {"A": (1.3, 5.5, 10.0), "B": (1.3, 5.5, 10.5), "C": (1.31, 5.4, 10.0), "D": (1.3, 5.6, 9.5), "E": (1.29, 5.5, 14.0)}
    C.append(("generous longshot prices are not flagged", analyse(long)["flags"] == []))
    C.append(("garbage number triples are rejected", parse_page("Table 1.50 1.60 1.70\nScore 12.00 45.00 3.00\n") == {}))
    ok = True
    for name, passed in C:
        print("  %s  %s" % ("PASS" if passed else "FAIL", name)); ok = ok and bool(passed)
    print("\nSELF-TEST %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Live odds scanner")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    p = sub.add_parser("find"); p.add_argument("competition")
    p = sub.add_parser("match"); p.add_argument("urls", nargs="+")
    p.add_argument("--watch", type=int, default=0, help="minutes between re-reads (0 = read once)")
    p = sub.add_parser("parse"); p.add_argument("file")
    a = ap.parse_args()
    if a.cmd == "selftest":
        sys.exit(selftest())
    if a.cmd == "parse":
        with open(a.file, encoding="utf-8") as f:
            books = parse_page(f.read())
        for k, v in books.items(): print("  %-28s %s" % (k, v))
        show(a.file, analyse(books), books); return
    if a.cmd not in ("find", "match"):
        ap.print_help(); return
    key = os.environ.get("CONTEXT_DEV_API_KEY")
    if not key:
        sys.exit("Set CONTEXT_DEV_API_KEY first:  set CONTEXT_DEV_API_KEY=ctxt_secret_...")
    if a.cmd == "find":
        pages = find_pages(key, a.competition)
        if not pages:
            sys.exit("No pages found. Open an odds-comparison site yourself and pass a match page to `match`.")
        for x in pages:
            print("  %s vs %s  %s\n    %s" % (x.get("home"), x.get("away"), x.get("kickoff"), x["url"]))
        print("\nscan them with:\n  python scan.py match " + " ".join('"%s"' % x["url"] for x in pages)); return
    seen, log = {}, []
    while True:
        for url in a.urls:
            t0 = time.time()
            md, why = scrape(key, url)
            took = time.time() - t0
            if md is None:
                print("\n%s\n  could not read the page: %s" % (url, why)); continue
            with open("scan_last_page.md", "w", encoding="utf-8") as f:
                f.write(md)
            books = parse_page(md); res = analyse(books)
            show("%s  (%s, read in %.1fs)" % (url[:90], time.strftime("%H:%M:%S"), took), res, books)
            if res:
                now = {(f["book"], f["outcome"]): f["price"] for f in res["flags"]}
                old = seen.get(url)
                if old is not None:
                    for k in now.keys() - old.keys(): print("  NEW since last read: %s %s at %.2f" % (k[0], k[1], now[k]))
                    for k in old.keys() - now.keys(): print("  GONE since last read: %s %s (was %.2f)" % (k[0], k[1], old[k]))
                seen[url] = now
                log.append({"t": time.time(), "url": url, "result": res})
                with open("scan_log.json", "w", encoding="utf-8") as f:
                    json.dump(log, f, indent=1)
        if not a.watch:
            break
        try:
            time.sleep(a.watch * 60)
        except KeyboardInterrupt:
            break
    print("\nsaved scan_log.json (results) and scan_last_page.md (the last page as read)")


if __name__ == "__main__":
    main()
