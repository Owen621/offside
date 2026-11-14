"""OFFSIDE: finds bookmaker prices that are out of line with everyone else's, live, and keeps an honest record.

    python app.py selftest                      offline check of the logic
    python app.py evidence                      build the 11-season chart data (from the files odds.py downloaded)
    python app.py discover "England League One" "England League Two" --tested
    python app.py matches --kickoff "2026-11-14 17:00" --tested "Wigan Athletic v Barnsley" "Stockport County v Reading"
                                                you name the matches (kick-off in your local time); Context.dev searches
                                                for an odds page for each and reads the prices. Fastest way to start.
    python app.py hub "https://www.oddschecker.com/football/english/league-2" --tested --kickoff "2026-11-14 17:00"
                                                no typing of matches: Context.dev reads a league's odds page, follows the
                                                link to every match on it, and reads each match's bookmaker prices
    python app.py discover --soon               any professional match worldwide kicking off in the next day
                                                Context.dev only: research the fixtures and kick-off times, search the
                                                web for odds pages for each match, and have Context.dev read every
                                                bookmaker's price out of the page as structured data
    python app.py leagues                       list football leagues that have odds right now (free)
    python app.py league soccer_england_league1 soccer_england_league2      watch leagues
    python app.py add URL --home "A" --away "B" --kickoff "2026-11-15 15:00"   also watch one odds page (Context.dev)
    python app.py serve                         start the scanner and the dashboard at http://127.0.0.1:8000

Keys (Windows:  set NAME=value)
    ODDS_API_KEY          free key from the-odds-api.com: live prices from many bookmakers per match
    CONTEXT_DEV_API_KEY   opens the bookmaker's own page to check each flagged price is really there, saves that page
                          as proof, finds team news for the match, and reads any odds page you paste in

Needs scan.py and odds.py in the same folder. Python 3.8+, no installs. Data is kept in the folder offside_data.

How a flagged price becomes a record entry
  1. Prices for every watched match are read every few minutes.
  2. A price is flagged when it beats the rule tested in odds.py (1 / average price, minus 3 to 5 points).
  3. It is RECORDED only if it is still there on the next read. That removes glitches.
  4. Context.dev then opens the bookmaker's own page for that match and looks for the price. The entry is marked
     'seen on the bookmaker's page' or 'could not be checked', and the page text is saved as proof with the time.
  5. After the match the final score is looked up and the entry is marked won or lost.
Nothing here places a bet.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import odds
import scan

VERSION = "build 12"          # shown at start-up and at the foot of the dashboard
DATA = "offside_data"
ODDS_API = "https://api.the-odds-api.com/v4"
EXCHANGE = re.compile(r"betfair_ex|matchbook|smarkets|exchange", re.I)     # exchanges are not bookmaker prices
TESTED_LEAGUES = {                                                          # the 18 leagues in the backtest
    "soccer_epl", "soccer_efl_champ", "soccer_england_league1", "soccer_england_league2", "soccer_spl",
    "soccer_germany_bundesliga", "soccer_germany_bundesliga2", "soccer_italy_serie_a", "soccer_italy_serie_b",
    "soccer_spain_la_liga", "soccer_spain_segunda_division", "soccer_france_ligue_one", "soccer_france_ligue_two",
    "soccer_netherlands_eredivisie", "soccer_belgium_first_div", "soccer_portugal_primeira_liga",
    "soccer_turkey_super_league", "soccer_greece_super_league"}
NEAR_ALPHA = 0.04                 # 'closest to the line' is measured against the medium setting


# --------------------------------------------------------------------------- small helpers
def load(name, default):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    tmp = os.path.join(DATA, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, os.path.join(DATA, name))


def parse_kickoff(text):
    """Epoch seconds from the common ways a kick-off time is written (local time), or None."""
    text = (text or "").strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return time.mktime(dt.datetime.strptime(text, fmt).timetuple())
        except ValueError:
            continue
    return None


def iso_utc(text):
    try:
        return dt.datetime.fromisoformat((text or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def teams_from_page(md):
    for line in (md or "").splitlines()[:60]:
        t = scan.clean_name(line)
        m = re.match(r"(.{2,40}?)\s+(?:vs\.?|v\.?|–|-)\s+(.{2,40}?)(?:\s+(?:odds|betting|1x2|-|–|\|).*)?$", t, re.I)
        if m and not scan.ODDS.search(t):
            return m.group(1).strip(), m.group(2).strip()
    return "", ""


def nearest(books, alpha=NEAR_ALPHA):
    """The price that comes closest to (or furthest past) the line, for the 'how close' column."""
    if len(books) < scan.MIN_BOOKS:
        return None
    n, best = float(len(books)), None
    for i, label in enumerate(("home", "draw", "away")):
        avg = sum(b[i] for b in books.values()) / n
        chance = 1.0 / avg - alpha
        if chance <= 0.02:
            continue
        need = 1.0 / chance
        for name, trio in books.items():
            if trio[i] < 5.0 and (best is None or trio[i] / need > best["ratio"]):
                best = {"book": name, "outcome": label, "price": trio[i], "needed": round(need, 3), "ratio": round(trio[i] / need, 4)}
    return best


# --------------------------------------------------------------------------- live odds (The Odds API)
def api_get(path, params, key):
    url = "%s%s?%s" % (ODDS_API, path, urllib.parse.urlencode(dict(params, apiKey=key)))
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()), {k.lower(): v for k, v in r.headers.items()}


def event_books(ev):
    """{bookmaker: (home, draw, away)} from one event, leaving out betting exchanges."""
    books = {}
    for bm in ev.get("bookmakers") or []:
        if EXCHANGE.search(bm.get("key") or ""):
            continue
        for mk in bm.get("markets") or []:
            if mk.get("key") != "h2h":
                continue
            px = {o.get("name"): o.get("price") for o in mk.get("outcomes") or []}
            trio = (px.get(ev.get("home_team")), px.get("Draw"), px.get(ev.get("away_team")))
            if all(isinstance(x, (int, float)) and x > 1.0 for x in trio):
                books[bm.get("title") or bm.get("key")] = tuple(float(x) for x in trio)
    return books


def event_links(ev):
    """{bookmaker: address of its own page for this match}, where the odds service provides one."""
    out = {}
    for bm in ev.get("bookmakers") or []:
        link = bm.get("link")
        for mk in bm.get("markets") or []:
            if mk.get("key") == "h2h":
                link = mk.get("link") or link
        if link and str(link).startswith("http"):
            out[bm.get("title") or bm.get("key")] = link
    return out


def price_on_page(text, price):
    """Is this price shown on the page, as a decimal (2.75) or the fraction UK sites use (7/4)?"""
    if not text:
        return False
    from fractions import Fraction
    forms = {"%.2f" % price, ("%.2f" % price).rstrip("0").rstrip(".")}
    fr = Fraction(price - 1.0).limit_denominator(40)
    if abs(float(fr) - (price - 1.0)) < 0.011:
        forms.add("%d/%d" % (fr.numerator, fr.denominator))
    return any(re.search(r"(?<![\d./])" + re.escape(f) + r"(?![\d/])", text) for f in forms if len(f) >= 3)


# --------------------------------------------------------------------------- Context.dev: search, read odds, screenshot
CTX_SEARCH = "https://api.context.dev/v1/web/search"
ODDS_SCHEMA = {
    "type": "object",
    "properties": {
        "home_team": {"type": ["string", "null"], "description": "Home team of the match this page is about."},
        "away_team": {"type": ["string", "null"], "description": "Away team of the match this page is about."},
        "kickoff": {"type": ["string", "null"], "description": "Kick-off date and time shown on the page, written as YYYY-MM-DD HH:MM in the page's own time zone. Null if not shown."},
        "in_play": {"type": ["boolean", "null"], "description": "True if the page shows the match has already started or is in-play."},
        "result_rows": {"type": ["array", "null"], "items": {"type": "string"},
                        "description": "The names of the three result rows of the match-result odds grid, top to bottom, exactly as shown, for example ['AFC Wimbledon', 'Blackpool', 'Draw']."},
        "bookmakers": {
            "type": "array",
            "description": "One entry per bookmaker row in the match-result (1X2) odds comparison table.",
            "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "Bookmaker name."},
                "home": {"type": ["number", "null"], "description": "Decimal odds for a home win."},
                "draw": {"type": ["number", "null"], "description": "Decimal odds for a draw."},
                "away": {"type": ["number", "null"], "description": "Decimal odds for an away win."}},
                "required": ["name", "home", "draw", "away"], "additionalProperties": False}}},
    "required": ["bookmakers"], "additionalProperties": False}
ODDS_INSTRUCTIONS = ("On sites that show fractions, a whole number on its own means that number to one: 2 means 2/1, which is 3.00 "
                     "in decimal, and 5 means 5/1, which is 6.00. Convert EVERY price, including the draw. "
                     "Some sites put bookmakers in COLUMNS (logos across the top) and the three results in ROWS: read down each "
                     "bookmaker's column. Take bookmaker names from logo alt text or link titles if they are not written out. "
                     "This should be an odds comparison page for ONE football match. Return every bookmaker that shows "
                     "prices for all three match results: home win, draw, away win (the 1X2 / match result / full-time "
                     "result market). Give decimal odds. If the page shows fractions, convert them: 6/4 is 2.50, 11/10 "
                     "is 2.10, evens is 2.00. Leave out betting exchanges, 'best odds' or average summary rows, and any "
                     "other market. If the page has no such table, return an empty list.")


def fractions_to_decimals(text):
    """UK sites print 6/4 where others print 2.50. Rewrite fractions so one reader handles both."""
    def conv(m):
        a, b = int(m.group(1)), int(m.group(2))
        return m.group(0) if b == 0 or a > 500 else "%.2f" % (1.0 + a / float(b))
    text = re.sub(r"(?<![\d/.])(\d{1,3})/(\d{1,2})(?![\d/.])", conv, text or "")
    return re.sub(r"\b(evens|evs)\b", "2.00", text, flags=re.I)


def valid_books(rows):
    """Keep bookmaker rows whose three prices form a believable market, leaving out exchanges."""
    out = {}
    for r in rows or []:
        name = (r.get("name") or "").strip()
        try: trio = tuple(float(r.get(k)) for k in ("home", "draw", "away"))
        except (TypeError, ValueError): continue
        if not name or EXCHANGE.search(name) or not all(1.01 <= x <= 60 for x in trio): continue
        if trio[1] < 2.4: continue                                  # a football draw is never priced this short: a misread
        if 0.99 <= sum(1.0 / x for x in trio) <= 1.18:              # a real bookmaker's margin on one match
            out.setdefault(name[:40], trio)
    return out


def coherent(books):
    """A real single-match table has every bookmaker quoting nearly the same prices. Rows that are really other
    matches or other markets do not agree with each other, so a page that fails this is not a usable odds table."""
    if len(books) < scan.MIN_BOOKS:
        return books
    import statistics
    med = [statistics.median(v[i] for v in books.values()) for i in range(3)]
    kept = {k: v for k, v in books.items() if all(0.75 * med[i] <= v[i] <= 1.33 * med[i] for i in range(3))}
    return kept if (len(kept) >= scan.MIN_BOOKS and len(kept) >= 0.6 * len(books) and len(kept) <= 60) else {}


BK_CODES = {"B3": "bet365", "WH": "William Hill", "UN": "Unibet", "FR": "Betfred", "FB": "Betfair Sportsbook", "SX": "Spreadex",
            "LD": "Ladbrokes", "KN": "BetMGM UK", "S6": "Star Sports", "VC": "BetVictor", "BY": "BOYLE Sports", "OE": "10bet", "G5": "BetGoodwin",
            "PUP": "PricedUp", "SI": "Sporting Index", "QN": "QuinnBet", "WA": "Betway", "BAH": "BetAhoy", "CE": "Coral", "BTT": "BetTom",
            "IVB": "IvyBet", "SK": "Skybet", "PP": "Paddy Power", "AKB": "AK Bets", "BF": "Betfair", "MA": "Matchbook"}
# Some comparison sites put bookmakers in COLUMNS and the three results in ROWS, with the price in an attribute.
GRID_RULES = {"rows": {"selector": "tr[data-bname]", "type": "list", "output": {
    "name": {"selector": "td", "type": "item", "output": "text"},
    "bks": {"selector": "td[data-odig]", "type": "list", "output": "@data-bk"},
    "odds": {"selector": "td[data-odig]", "type": "list", "output": "@data-odig"}}}}


def grid_books(parsed):
    """Bookmakers-in-columns table -> {bookmaker: (home, draw, away)}. Uses the first three result rows in page
    order (home, draw, away is how these sites list a match-result market)."""
    rows = [r for r in ((parsed or {}).get("rows") or []) if r.get("odds") and r.get("bks")][:3]
    if len(rows) != 3:
        return {}
    labels = [str(r.get("name") or "").strip().lower() for r in rows]
    order = [0, 1, 2]
    if "draw" in labels and labels.index("draw") != 1:
        d = labels.index("draw"); rest = [i for i in range(3) if i != d]; order = [rest[0], d, rest[1]]
    cols = {}
    for pos, ri in enumerate(order):
        for bk, odd in zip(rows[ri]["bks"], rows[ri]["odds"]):
            try: v = float(odd)
            except (TypeError, ValueError): continue
            if bk and v > 1.0:
                cols.setdefault(str(bk), {})[pos] = v
    return valid_books([{"name": BK_CODES.get(k, k), "home": v.get(0), "draw": v.get(1), "away": v.get(2)} for k, v in cols.items() if len(v) == 3])


def _price(text, fractional=True):
    """'11/10' -> 2.10, '2' on a fractional site -> 3.00 (two to one), '2.10' -> 2.10. None if not a price."""
    t = (text or "").strip()
    m = re.fullmatch(r"(\d{1,3})/(\d{1,3})", t)
    if m and int(m.group(2)) > 0:
        return round(1.0 + int(m.group(1)) / float(m.group(2)), 3)
    if re.fullmatch(r"\d{1,2}\.\d{1,3}", t):
        v = float(t); return v if v > 1.0 else None
    if fractional and re.fullmatch(r"\d{1,3}", t) and int(t) > 0:
        return float(int(t) + 1)
    if t.lower() in ("evs", "evens"):
        return 2.0
    return None


def html_grid(html):
    """Read a bookmakers-in-columns odds table directly from the page HTML. Cells are matched to bookmakers by
    their data-bk code, so empty cells cannot shift a column. Returns {bookmaker: (home, draw, away)}."""
    from html.parser import HTMLParser
    if not html or "data-bk" not in html:
        return {}
    rows = []
    class T(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True); self.row = None; self.cell = None; self.depth = 0
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "tr":
                self.row = {"attrs": a, "cells": [], "text": ""}; rows.append(self.row); return
            if self.row is None: return
            if tag in ("td", "th") and self.cell is None:
                self.cell = {"attrs": a, "text": "", "hints": []}; self.row["cells"].append(self.cell); self.depth = 0; return
            if self.cell is not None:
                self.depth += 1 if tag not in ("img", "br", "input", "source") else 0
                if a.get("data-bk") and not self.cell["attrs"].get("data-bk"): self.cell["attrs"]["data-bk"] = a["data-bk"]
                for k, v in a.items():
                    if k in ("title", "alt", "aria-label") and v: self.cell["hints"].append(v)
                    elif k.startswith("data-") and v: self.cell["attrs"].setdefault(k, v)
        def handle_endtag(self, tag):
            if tag == "tr": self.row = None; self.cell = None
            elif tag in ("td", "th") and self.cell is not None and self.depth <= 0: self.cell = None
            elif self.cell is not None: self.depth -= 1
        def handle_data(self, data):
            if self.cell is not None: self.cell["text"] += data
            if self.row is not None: self.row["text"] += data
    try:
        T().feed(html)
    except Exception:
        return {}
    fractional = bool(re.search(r">\s*\d{1,3}/\d{1,3}\s*<", html))
    def cell_price(c):
        for k in ("data-odig", "data-o", "data-odds", "data-fodds", "data-price", "data-decimal"):
            v = _price(c["attrs"].get(k, ""), fractional and k != "data-odig")
            if v: return v
        return _price(c["text"], fractional)
    names, priced = {}, []
    for r in rows:
        cells = [c for c in r["cells"] if c["attrs"].get("data-bk")]
        if not cells: continue
        vals = {c["attrs"]["data-bk"]: cell_price(c) for c in cells}
        got = {k: v for k, v in vals.items() if v}
        if len(got) >= 5:
            label = r["attrs"].get("data-bname") or next((c["text"].strip() for c in r["cells"] if c["text"].strip() and not c["attrs"].get("data-bk")), "")
            priced.append((label.lower(), got))
        else:                                                       # a header row: bookmaker names live here
            for c in cells:
                hint = next((h for h in c["hints"] if re.search(r"[A-Za-z]", h)), "") or c["text"].strip()
                if hint and c["attrs"]["data-bk"] not in names: names[c["attrs"]["data-bk"]] = re.sub(r"\s+", " ", hint)[:40]
    if len(priced) < 3:
        return {}
    three = priced[:3]
    labels = [t[0] for t in three]
    if any("draw" in l for l in labels) and "draw" not in labels[1]:
        d = next(i for i, l in enumerate(labels) if "draw" in l); rest = [i for i in range(3) if i != d]
        three = [three[rest[0]], three[d], three[rest[1]]]
    found = []
    for bk in three[0][1]:
        if all(bk in t[1] for t in three):
            found.append({"name": names.get(bk) or BK_CODES.get(bk, bk), "home": three[0][1][bk], "draw": three[1][1][bk], "away": three[2][1][bk]})
    return valid_books(found)


EXCHANGE_CODES = {"BF", "MA", "SM", "BD", "MK"}                   # betting exchanges: not bookmaker prices


def html_buttons(html, home="", away="", info=None, rows_hint=None):
    """Read an odds grid built from price BUTTONS, each tagged with a bookmaker code (data-bk): one run of buttons
    per result, with a strip of bookmaker logos above in the same order. Which run is home / draw / away comes
    from the list of result names on the page, matched to the team names in the page address.
    Returns {bookmaker: (home, draw, away)}. `info`, if given, is filled with how the rows were identified."""
    from html.parser import HTMLParser
    info = info if info is not None else {}
    if not html or "data-bk" not in html:
        return {}
    tok = lambda s: {w for w in re.findall(r"[a-z]{3,}", (s or "").lower())
                     if w not in ("the", "city", "town", "united", "county", "athletic", "rovers", "wanderers", "albion")}
    h_tok, a_tok = tok(home), tok(away)
    def kind(text):
        t = text.strip().lower()
        if len(t) > 30: return ""
        if t == "draw": return "draw"
        hh, aa = bool(h_tok & tok(t)), bool(a_tok & tok(t))
        return "home" if (hh and not aa) else ("away" if (aa and not hh) else "")
    buttons, logos, kinds, by_code = [], [], [], {}
    class T(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True); self.btn = None
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "button" and a.get("data-bk"):
                self.btn = {"bk": a["data-bk"], "text": "", "logos": len(logos)}; return
            if tag == "img" and "bookmakerimg" in (a.get("class") or "").lower() and a.get("alt"):
                alt = re.sub(r"(?i)\s*logo\s*$", "", a["alt"]).strip()
                logos.append(alt)
                m = re.search(r"/logos/[A-Za-z]+/([A-Za-z0-9]+)/", (a.get("data-src") or "") + " " + (a.get("src") or ""))
                if m: by_code.setdefault(m.group(1).upper(), alt)
        def handle_endtag(self, tag):
            if tag == "button" and self.btn is not None:
                buttons.append(self.btn); self.btn = None
        def handle_data(self, data):
            d = data.strip()
            if not d: return
            if self.btn is not None: self.btn["text"] += d
            else:
                k = kind(d)
                if k: kinds.append((k, len(buttons)))
    try:
        T().feed(html)
    except Exception:
        return {}
    if len(buttons) < 15:
        return {}
    fractional = any("/" in b["text"] for b in buttons)
    rows = []                                                      # a row ends when a bookmaker code repeats
    for b in buttons:
        if not rows or b["bk"] in rows[-1]["p"]:
            rows.append({"p": {}, "order": [], "logos": b["logos"]})
        rows[-1]["p"][b["bk"]] = _price(b["text"], fractional); rows[-1]["order"].append(b["bk"])
    rows = [r for r in rows if sum(1 for v in r["p"].values() if v) >= 5][:3]
    if len(rows) != 3:
        return {}
    # Which row is which. The site lists the rows in its own order (often favourite first, draw last), so the order
    # must come from the page: the place where it names home, draw and away one after another, nearest the grid.
    perm = lambda ks: [ks.index("home"), ks.index("draw"), ks.index("away")] if sorted(ks) == ["away", "draw", "home"] else None
    wins = [(kinds[i][1], [k for k, _ in kinds[i:i + 3]]) for i in range(len(kinds) - 2)
            if perm([k for k, _ in kinds[i:i + 3]]) and len({n for _, n in kinds[i:i + 3]}) == 1]
    before = [w for n, w in wins if n == 0]; after = [w for n, w in wins if n >= len(buttons)]
    seen = before[-1] if before else (after[0] if after else (wins[0][1] if wins else None))
    hinted = [kind(x) for x in (rows_hint or [])][:3]
    html_order, hint_order = (perm(seen) if seen else None), perm(hinted) if len(hinted) == 3 else None
    order = html_order or hint_order                               # the page's own names win; the model's are a fallback
    if not order:
        info["rows"] = "could not tell which row is home, draw and away: not used"; return {}
    info["rows"] = "rows top to bottom: " + ", ".join(seen or hinted) + (" (from the page)" if html_order else " (from the extraction)")
    # bookmaker names: the logo strip just above the first row, in the same order as the buttons
    codes = rows[0]["order"]
    strip = logos[max(0, rows[0]["logos"] - len(codes)):rows[0]["logos"]]
    names = dict(zip(codes, strip)) if len(strip) == len(codes) else {}
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    sure = {"B3", "SK", "PP", "WH", "LD", "CE", "FR", "UN", "VC", "EE", "BY", "OE", "WA", "FB", "SX"}   # codes I am certain of
    known = [c for c in codes if c in sure and c in names]
    if names and known and sum(1 for c in known if norm(BK_CODES[c])[:5] in norm(names[c])) < max(1, len(known) // 2):
        names = {}                                                 # the strip does not line up with the codes: do not trust it
    names = dict(names, **{c: by_code[c] for c in codes if c in by_code})    # the logo's own address names its bookmaker
    info["names"] = "from the bookmaker logos" if names else "from bookmaker codes"
    found, used = [], set()
    for bk in codes:
        if bk in EXCHANGE_CODES: continue
        trio = [rows[i]["p"].get(bk) for i in order]
        if not all(trio): continue
        name = BK_CODES.get(bk) or names.get(bk) or bk
        if name in used: name = bk
        used.add(name)
        found.append({"name": name, "home": trio[0], "draw": trio[1], "away": trio[2]})
    return valid_books(found)


def teams_in_address(url):
    seg = next((s for s in urllib.parse.urlparse(url).path.strip("/").split("/") if re.search(r".-(v|vs)-.", s)), "")
    parts = re.split(r"-(?:v|vs)-", seg, maxsplit=1) if seg else []
    return (parts[0].replace("-", " "), parts[1].replace("-", " ")) if len(parts) == 2 else ("", "")


# Context.dev's own parse rules pull the price grid out of the page as structured lists: every price button with its
# bookmaker code, the bookmaker logos, and the names of the result rows in page order.
CTX_RULES = {
    "codes":      {"selector": "button[data-bk]", "type": "list", "output": "@data-bk"},
    "prices":     {"selector": "button[data-bk]", "type": "list", "output": "text"},
    "logo_names": {"selector": "img[class*='bookmakerImg']", "type": "list", "output": "@alt"},
    "logo_srcs":  {"selector": "img[class*='bookmakerImg']", "type": "list", "output": "@src"},
    "row_names":  {"selector": "a[href*='/bet-history/']", "type": "list", "output": "text"},
}


def rules_books(parsed, home="", away="", info=None):
    """Assemble {bookmaker: (home, draw, away)} from what Context.dev's parse rules returned."""
    info = info if info is not None else {}
    parsed = parsed or {}
    codes, prices = parsed.get("codes") or [], parsed.get("prices") or []
    if len(codes) < 15 or len(codes) != len(prices):
        return {}
    fractional = any("/" in str(x) for x in prices)
    rows = []
    for bk, txt in zip(codes, prices):
        if not rows or bk in rows[-1]["p"]:
            rows.append({"p": {}, "order": []})
        rows[-1]["p"][bk] = _price(str(txt or ""), fractional); rows[-1]["order"].append(bk)
    rows = [r for r in rows if sum(1 for v in r["p"].values() if v) >= 5][:3]
    if len(rows) != 3:
        return {}
    tok = lambda s: {w for w in re.findall(r"[a-z]{3,}", (s or "").lower())
                     if w not in ("the", "city", "town", "united", "county", "athletic", "rovers", "wanderers", "albion")}
    def kind(text):
        t = str(text or "").strip().lower()
        if t == "draw": return "draw"
        hh, aa = bool(tok(home) & tok(t)), bool(tok(away) & tok(t))
        return "home" if (hh and not aa) else ("away" if (aa and not hh) else "")
    kinds = [kind(x) for x in (parsed.get("row_names") or [])[:3]]
    if sorted(kinds) != ["away", "draw", "home"]:
        info["rows"] = "row names not recognised (%s)" % ", ".join(str(x) for x in (parsed.get("row_names") or [])[:3]); return {}
    order = [kinds.index("home"), kinds.index("draw"), kinds.index("away")]
    info["rows"] = "rows top to bottom: " + ", ".join(kinds) + " (from Context.dev parse rules)"
    names = {}
    for alt, src in zip(parsed.get("logo_names") or [], parsed.get("logo_srcs") or []):
        m = re.search(r"/logos/[A-Za-z]+/([A-Za-z0-9]+)/", str(src or ""))
        if m and alt: names.setdefault(m.group(1).upper(), re.sub(r"(?i)\s*logo\s*$", "", str(alt)).strip())
    found, used = [], set()
    for bk in rows[0]["order"]:
        if bk in EXCHANGE_CODES: continue
        trio = [rows[i]["p"].get(bk) for i in order]
        if not all(trio): continue
        name = BK_CODES.get(bk) or names.get(bk) or bk
        if name in used: name = bk
        used.add(name)
        found.append({"name": name, "home": trio[0], "draw": trio[1], "away": trio[2]})
    return valid_books(found)


def grid_from_text(md):
    """Read a bookmakers-in-columns table from the page text: three neighbouring rows (home, draw, away), each
    holding one price per bookmaker. Bookmaker names come from the header row above when it lines up."""
    lines = fractions_to_decimals(md or "").splitlines()
    one_price = lambda cell: (lambda t: float(t) if re.fullmatch(r"\d{1,2}\.\d{1,2}", t) else None)(scan.clean_name(cell).strip())
    wide = []                                                       # (line number, label, [price or None per column], is_table)
    for i, line in enumerate(lines):
        if "|" in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            vals = [one_price(c) for c in cells[1:]]
            if sum(v is not None for v in vals) >= 5:
                wide.append((i, scan.clean_name(cells[0]), vals, True)); continue
        nums = [(m.start(), float(m.group(1))) for m in scan.ODDS.finditer(line)]
        if len(nums) >= 5 and "|" not in line:
            wide.append((i, scan.clean_name(line[:nums[0][0]]), [v for _, v in nums], False))
    for k in range(len(wide) - 2):
        a, b, c = wide[k], wide[k + 1], wide[k + 2]
        if c[0] - a[0] > 8:
            continue
        rows = [a, b, c]
        labels = [r[1].lower() for r in rows]
        if any("draw" in l or l in ("x", "tie") for l in labels):
            d = next(i for i, l in enumerate(labels) if "draw" in l or l in ("x", "tie"))
            rest = [i for i in range(3) if i != d]; rows = [rows[rest[0]], rows[d], rows[rest[1]]]
        n = min(len(r[2]) for r in rows)
        names = {}
        if a[3]:                                                    # header row of the same table, a few lines up
            for j in range(a[0] - 1, max(-1, a[0] - 7), -1):
                if "|" in lines[j] and not re.fullmatch(r"[\s|:\-]+", lines[j]):
                    head = [scan.clean_name(x) for x in lines[j].strip().strip("|").split("|")][1:]
                    if len(head) >= n: names = {q: head[q] for q in range(n) if re.search(r"[A-Za-z]", head[q] or "")}
                    break
        found = []
        for q in range(n):
            trio = [r[2][q] for r in rows]
            if all(v is not None for v in trio):
                found.append({"name": names.get(q) or "Bookmaker %d" % (q + 1), "home": trio[0], "draw": trio[1], "away": trio[2]})
        books = valid_books(found)
        if len(books) >= scan.MIN_BOOKS:
            return books
    return {}


PAGE_META = {}          # url -> what the page said about the match (teams, kick-off), filled by ctx_read_odds
CREDITS = {}            # {"left": n} from the latest Context.dev response
READ_META = {}          # url -> facts about the latest read (Context.dev request id, fresh or cached, how it was read)


def _note_read(url, res, method):
    cm = res.get("cache_metadata") or {}
    READ_META[url] = {"request_id": res.get("request_id") or "", "fresh": cm.get("status") in ("miss", "zdr") or cm.get("age_ms") == 0,
                      "age_ms": cm.get("age_ms"), "method": method}
    if (res.get("key_metadata") or {}).get("credits_remaining") is not None: CREDITS["left"] = res["key_metadata"]["credits_remaining"]


def uk_time_to_epoch(text):
    """'2026-11-14 15:00' as UK clock time -> epoch seconds (UK is UTC in winter, UTC+1 in summer)."""
    try:
        d = dt.datetime.strptime((text or "").strip()[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    last_sun = lambda y, m: max(dt.date(y, m, x) for x in range(25, 32) if dt.date(y, m, x).weekday() == 6)
    summer = last_sun(d.year, 3) <= d.date() < last_sun(d.year, 10)
    return d.replace(tzinfo=dt.timezone.utc).timestamp() - (3600 if summer else 0)


def ctx_links(key, url, post=None):
    """Every link on a page, through Context.dev (rendered page, UK visitor)."""
    post = post or (lambda body: scan.http(scan.CTX_SCRAPE, body, key, timeout=90))
    body = {"url": url, "formats": {"parse": True, "markdown": True},
            "parseParams": {"rules": {"links": {"selector": "a", "type": "list", "output": "@href"}}},
            "sharedParams": {"waitFor": 2500, "country": "GB", "dismissPopups": True}, "maxAgeMs": 0,
            "timeoutOpts": {"milliseconds": 60000, "behavior": "return-partial"}}
    try:
        res = post(body)
    except urllib.error.HTTPError as e:
        return [], "HTTP %d %s" % (e.code, e.read().decode()[:160])
    except Exception as e:
        return [], str(e)[:160]
    links = list((((res.get("parsed") or {}).get("data") or {}).get("links")) or [])
    md = (res.get("markdown") or {}).get("data") or ""
    links += re.findall(r"\]\(([^)\s]+)\)", md)
    out = []
    first = urllib.parse.urlparse(url).path.strip("/").split("/")[0]
    for h in links:
        if not isinstance(h, str) or h.startswith(("#", "javascript", "mailto")): continue
        if first and not re.match(r"^(https?:)?/", h) and h.split("/")[0] == first:
            h = "/" + h                                              # 'football/x' on a /football/... page means '/football/x'
        full = urllib.parse.urljoin(url, h).split("#")[0]
        if full not in out: out.append(full)
    return out, ("" if out else ((res.get("parsed") or {}).get("message") or (res.get("markdown") or {}).get("message") or "the page had no links"))


TESTED_PATHS = re.compile(r"/football/(english/(premier-league|championship|league-1|league-2)/|scot(land|tish)/premiership/|"
                          r"german[y]?/(bundesliga|bundesliga-2|2-bundesliga)/|ital(y|ian)/serie-[ab]/|spa(in|nish)/(la-liga[a-z-]*|primera[a-z-]*|segunda[a-z-]*)/|"
                          r"fr(ance|ench)/ligue-[12]/|(netherlands|holland|dutch)/eredivisie/|belgi(um|an)/[a-z0-9-]+/|portug(al|uese)/[a-z0-9-]+/|"
                          r"turk(ey|ish)/[a-z0-9-]+/|gree(ce|k)/[a-z0-9-]+/)", re.I)


def match_pages(hub_url, links):
    """From an odds page's links, the ones that are single-match pages: [(url, home, away)]."""
    host = urllib.parse.urlparse(hub_url).netloc
    out, seen = [], set()
    for u in links:
        pr = urllib.parse.urlparse(u)
        if pr.netloc != host: continue
        if re.search(r"/(tips|news|blog|offers|free-bets)/|predictions|preview|bet-builder", pr.path, re.I): continue
        hub_path = urllib.parse.urlparse(hub_url).path.rstrip("/")
        if not pr.path.startswith("/football/"): continue           # the site's menus also link to boxing, tennis and so on
        if hub_path.count("/") >= 3 and not pr.path.startswith(hub_path + "/"): continue   # a league page: only that league
        seg = next((s for s in pr.path.strip("/").split("/") if re.search(r".-(v|vs)-.", s)), None)
        if not seg: continue
        home, away = [re.sub(r"-\d{4,}$", "", x).replace("-", " ").title() for x in re.split(r"-(?:v|vs)-", seg, maxsplit=1)]
        base = u[:u.index(seg) + len(seg)]
        if "oddschecker" in host: base += "/winner"                 # that site's match-result market page
        if base in seen or not home or not away: continue
        seen.add(base); out.append((base, home, away))
    return out


def ctx_read_odds(key, url, post=None, light=False):
    """Read one odds page through Context.dev. Returns (books, page_text, reason_if_empty).
    Context.dev renders the page, waits for the prices to load, and extracts each bookmaker's prices as JSON;
    the plain-text reader (which also understands fractions) is the fallback."""
    post = post or (lambda body: scan.http(scan.CTX_SCRAPE, body, key, timeout=90))
    if light:                                                      # a page already read once: just the rendered HTML, no model
        try:
            res = post({"url": url, "formats": {"parse": True, "html": True, "markdown": True}, "parseParams": {"rules": CTX_RULES},
                        "sharedParams": {"waitFor": 3000, "country": "GB", "dismissPopups": True},
                        "maxAgeMs": 0, "timeoutOpts": {"milliseconds": 45000, "behavior": "return-partial"}})
            pr = res.get("parsed") or {}
            page_text = (res.get("markdown") or {}).get("data") if (res.get("markdown") or {}).get("success") else ""
            grid = coherent(rules_books(pr.get("data") if pr.get("success") else None, *teams_in_address(url)))
            method = "Context.dev parse rules"
            if len(grid) < scan.MIN_BOOKS:                          # fall back to reading the returned HTML ourselves
                page_html = (res.get("html") or {}).get("data") if (res.get("html") or {}).get("success") else ""
                grid = coherent(html_grid(page_html)) or coherent(html_buttons(page_html, *teams_in_address(url)))
                method = "HTML from Context.dev"
            _note_read(url, res, method)
            if len(grid) >= scan.MIN_BOOKS:
                return grid, "(prices read with %s)\n\n%s" % (method, page_text or ""), ""
        except Exception:
            pass
    best, best_md, why = {}, None, ""
    for wait in (3000, 9000):                                      # give the table longer if it is missing the first time
        body = {"url": url, "formats": {"json": True, "markdown": True, "parse": True, "html": True},
                "jsonParams": {"schema": ODDS_SCHEMA, "instructions": ODDS_INSTRUCTIONS}, "parseParams": {"rules": dict(GRID_RULES, **CTX_RULES)},
                "sharedParams": {"waitFor": wait, "country": "GB", "dismissPopups": True},
                "maxAgeMs": 0, "timeoutOpts": {"milliseconds": 60000, "behavior": "return-partial"}}
        res = None
        for attempt in range(3):
            try:
                res = post(body); break
            except urllib.error.HTTPError as e:
                msg = e.read().decode()[:200]
                if e.code == 429:
                    time.sleep(max(2.0, float(e.headers.get("Retry-After") or 5))); continue
                if e.code == 413 or "CONTENT_TOO_LARGE" in msg:
                    body["formats"].pop("html", None); continue
                if e.code in (400, 403) and "parseParams" in body:
                    body.pop("parseParams"); body["formats"].pop("parse", None); continue
                if e.code in (400, 403) and "sharedParams" in body:
                    body.pop("sharedParams"); continue
                why = "HTTP %d %s" % (e.code, msg); break
            except Exception as e:
                why = str(e)[:160]; time.sleep(2)
        if res is None:
            continue
        if (res.get("key_metadata") or {}).get("credits_remaining") is not None: CREDITS["left"] = res["key_metadata"]["credits_remaining"]
        md = (res.get("markdown") or {}).get("data") if (res.get("markdown") or {}).get("success") else None
        js, pr = (res.get("json") or {}), (res.get("parsed") or {})
        if js.get("success") and isinstance(js.get("data"), dict):
            PAGE_META[url] = {k: js["data"].get(k) for k in ("home_team", "away_team", "kickoff", "in_play")}
        page_html = (res.get("html") or {}).get("data") if (res.get("html") or {}).get("success") else ""
        _note_read(url, res, "Context.dev parse rules")
        grid = coherent(rules_books(pr.get("data") if pr.get("success") else None, *teams_in_address(url)))   # Context.dev's parse rules
        if len(grid) < scan.MIN_BOOKS:
            READ_META[url]["method"] = "HTML from Context.dev"
            grid = coherent(html_grid(page_html))                  # else the table in the returned HTML, read exactly
        binfo = {}
        if len(grid) < scan.MIN_BOOKS:
            grid = coherent(html_buttons(page_html, *teams_in_address(url), info=binfo,
                                         rows_hint=((js.get("data") or {}).get("result_rows") if js.get("success") and isinstance(js.get("data"), dict) else None)))
        if len(grid) < scan.MIN_BOOKS:
            grid = coherent(grid_books(pr.get("data")) if pr.get("success") else {})
        if len(grid) < scan.MIN_BOOKS and md:
            grid = coherent(grid_from_text(md))
        extracted = coherent(valid_books(((js.get("data") or {}).get("bookmakers")) if js.get("success") else []))
        plain = scan.parse_page(fractions_to_decimals(md)) if md else {}
        plain = coherent({k: v for k, v in plain.items() if not EXCHANGE.search(k)})
        # order of trust: prices read from the table itself, then Context.dev's extraction, then the text reader
        if page_html and len(re.findall(r"<button[^>]*data-bk", page_html)) >= 15:
            extracted, plain = {}, {}                              # a button grid: read exactly or not at all
        books = next((b for b in (grid, extracted, plain) if len(b) >= scan.MIN_BOOKS), max((grid, extracted, plain), key=len))
        if len(books) > len(best) or best_md is None:
            best, best_md = books, md
        if len(books) >= scan.MIN_BOOKS:
            return books, md, ""
        msg = js.get("message") or (res.get("markdown") or {}).get("message") or ""
        title = ((res.get("metadata") or {}).get("title") or "")[:70]
        n_prices = len(scan.ODDS.findall(fractions_to_decimals(md))) if md else 0
        if md and n_prices < 8:
            why = "the site shows no prices on this page (postponed, finished, or not priced yet). Page title: '%s' [request %s]" % (title, res.get("request_id") or "?")
        else:
            why = "%stable cells %d, extraction %d, text %d after waiting %ds. %s Page title: '%s' [request %s]" % (
                (binfo["rows"] + ". ") if binfo.get("rows", "").endswith("not used") else "", len(grid), len(extracted), len(plain), wait // 1000,
                msg or ("page loaded with %d prices on it but no bookmaker table recognised." % n_prices if md else "page did not load."), title, res.get("request_id") or "?")
        if md is None and ("404" in msg or "NOT_FOUND" in msg):
            break                                                    # a dead address will not improve with waiting
    return best, best_md, why


def ctx_search(key, query, n=10):
    try:
        res = scan.http(CTX_SEARCH, {"query": query, "numResults": n}, key, timeout=45)
        return [r.get("url") for r in res.get("results") or [] if (r.get("url") or "").startswith("http")]
    except Exception:
        return []


def ctx_screenshot(key, url):
    """A picture of the page as Context.dev sees it right now: (image bytes, extension) or (None, '')."""
    import base64
    try:
        res = scan.http(scan.CTX_SCRAPE, {"url": url, "formats": {"screenshot": True}, "screenshotParams": {"format": "jpeg"},
                                     "sharedParams": {"waitFor": 2500, "country": "GB", "dismissPopups": True, "viewport": {"width": 1440, "height": 1700}},
                                     "maxAgeMs": 0}, key, timeout=150)
        data = (res.get("screenshot") or {}).get("data") or ""
        m = re.match(r"^data:image/(png|jpeg|webp);base64,(.+)$", data, re.S)
        return (base64.b64decode(m.group(2)), "jpg" if m.group(1) == "jpeg" else m.group(1)) if m else (None, "")
    except Exception:
        return None, ""


# --------------------------------------------------------------------------- Context.dev lookups
def ctx_answers(key, task, shape, deep=False):
    """A researched answer in the JSON shape asked for. deep=True uses the slower, more thorough mode."""
    for mode, ms in ((("ultra", 150000),) if deep else ()) + (("fast", 30000),):
        try:
            res = scan.http(scan.CTX_ANSWERS, {"mode": mode, "task": task, "json_format": shape,
                                               "timeoutOpts": {"milliseconds": ms, "behavior": "return-partial"}}, key, timeout=ms / 1000 + 60)
            if res.get("json_content"): return res["json_content"]
        except Exception:
            continue
    return {}


def lookup_result(key, rec):
    day = time.strftime("%Y-%m-%d", time.localtime(rec.get("kickoff") or rec["t"]))
    js = ctx_answers(key, "What was the final score of the football (soccer) match %s vs %s played on or about %s? "
                          "Only answer finished=true if the match has ended." % (rec["home"], rec["away"], day),
                     {"finished": False, "home_goals": 0, "away_goals": 0, "source_url": ""})
    if not js or not js.get("finished"):
        return None
    try:
        return {"home_goals": int(js["home_goals"]), "away_goals": int(js["away_goals"])}
    except (KeyError, TypeError, ValueError):
        return None


def lookup_news(key, rec):
    js = ctx_answers(key, "Is there team news from the last 48 hours for the football match %s vs %s that would move "
                          "the betting odds (injuries, suspensions, a manager change, confirmed line-ups)? Answer in one "
                          "short sentence, or say 'No significant team news found'." % (rec["home"], rec["away"]),
                     {"note": "", "source_url": ""})
    return (js.get("note") or "").strip()[:200], (js.get("source_url") or "").strip()


DEFAULT_HUB = "https://www.oddschecker.com/football"
BOOK_SITES = {"bet365": "bet365.com", "williamhill": "williamhill.com", "unibet": "unibet.co.uk", "betfred": "betfred.com",
              "betfairsportsbook": "betfair.com", "spreadex": "spreadex.com", "ladbrokes": "ladbrokes.com", "betmgmuk": "betmgm.co.uk",
              "betmgm": "betmgm.co.uk", "starsports": "starsports.bet", "betvictor": "betvictor.com", "boylesports": "boylesports.com",
              "10bet": "10bet.co.uk", "betgoodwin": "betgoodwin.co.uk", "pricedup": "pricedup.bet", "sportingindex": "sportingindex.com",
              "quinnbet": "quinnbet.com", "betway": "betway.com", "coral": "coral.co.uk", "skybet": "skybet.com", "paddypower": "paddypower.com",
              "akbets": "akbets.bet", "bettom": "bettom.com", "ivybet": "ivybet.com", "betahoy": "betahoy.com", "888sport": "888sport.com",
              "virginbet": "virginbet.com", "livescorebet": "livescorebet.com", "midnite": "midnite.com", "betuk": "betuk.com"}


SOURCE_SCHEMA = {"type": "object", "properties": {
    "is_this_match": {"type": "boolean", "description": "True only if this page is about the named match itself (not a list of many matches, a league page or a promotion)."},
    "home_win": {"type": ["number", "null"], "description": "Decimal odds for the home team to win the match (full-time result)."},
    "draw": {"type": ["number", "null"], "description": "Decimal odds for the draw (full-time result)."},
    "away_win": {"type": ["number", "null"], "description": "Decimal odds for the away team to win the match (full-time result)."}},
    "required": ["is_this_match"], "additionalProperties": False}


def ctx_confirm_at_source(key, book, home, away, price, outcome="home", http=None):
    """Find the bookmaker's OWN page for this match and read its price there. Three Context.dev calls work together:
    search (limited to the bookmaker's site) and a researched answer both look for the page; a scrape with JSON
    extraction then reads the three match-result prices from it.
    Returns (status, address, page text, price there). Status:
      seen       the same price is on the bookmaker's own page
      moved      the match page was found and now shows a different price
      not shown  the match page was found but its price could not be read
      unreadable no page for this match could be found or opened
      no site    this bookmaker's site is not known"""
    http = http or (lambda url, body, timeout=75: scan.http(url, body, key, timeout=timeout))
    site = BOOK_SITES.get(re.sub(r"[^a-z0-9]", "", (book or "").lower()))
    if not site:
        return "no site", "", "", None
    tok = lambda s: {w for w in re.findall(r"[a-z]{4,}", (s or "").lower()) if w not in ("city", "town", "united", "county", "athletic", "rovers")}
    h_tok, a_tok = tok(home), tok(away)
    named = lambda text: bool(h_tok & tok(text)) and bool(a_tok & tok(text))
    cands = []
    for body in ({"query": "%s v %s" % (home, away), "numResults": 8, "includeDomains": [site]},
                 {"query": "%s %s v %s betting odds" % (book, home, away), "numResults": 10}):
        try:
            for r in http(CTX_SEARCH, body, 45).get("results") or []:
                u = r.get("url") or ""
                if site in u and u not in [c[0] for c in cands]:
                    cands.append((u, named(u.replace("-", " ") + " " + (r.get("title") or ""))))
        except Exception:
            pass
        if any(ok for _, ok in cands): break
    if not cands:                                                     # nothing from search: ask Context.dev's research for the page
        try:
            js = http(scan.CTX_ANSWERS, {"mode": "fast", "json_format": {"url": ""}, "timeoutOpts": {"milliseconds": 30000, "behavior": "return-partial"},
                                         "task": "Find the web address of the page on %s (the bookmaker %s) for the football match %s v %s, where its match "
                                                 "result odds (home win, draw, away win) are shown. Return only an address on %s." % (site, book, home, away, site)}, 90)
            u = ((js.get("json_content") or {}).get("url") or "").strip()
            if u.startswith("http") and site in u: cands.insert(0, (u, True))
        except Exception:
            pass
    cands.sort(key=lambda c: not c[1])
    def tidy(u):                                                      # drop tracking and affiliate parameters from the link
        pr = urllib.parse.urlparse(u)
        keep = [(k, v) for k, v in urllib.parse.parse_qsl(pr.query) if not re.match(r"(btag|account|static|affid|aff|ref|clickid|utm_.*)$", k, re.I)]
        return urllib.parse.urlunparse(pr._replace(query=urllib.parse.urlencode(keep)))
    def read(u):
        try:
            return http(scan.CTX_SCRAPE, {"url": u, "formats": {"markdown": True, "json": True},
                                          "jsonParams": {"schema": SOURCE_SCHEMA, "instructions": "The match is %s (home) v %s (away). Give decimal odds; convert fractions "
                                                         "(6/4 is 2.50; a whole number such as 2 means 2/1, which is 3.00). Use null for a price you cannot see. If the page "
                                                         "lists several matches, use the row for this match and set is_this_match to true." % (home, away)},
                                          "sharedParams": {"waitFor": 3000, "country": "GB", "dismissPopups": True}, "maxAgeMs": 0,
                                          "timeoutOpts": {"milliseconds": 45000, "behavior": "return-partial"}}, 75)
        except Exception:
            return None
    top = cands[:3]
    if len(top) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(top)) as ex: pages = list(ex.map(lambda c: read(c[0]), top))
    else:
        pages = [read(c[0]) for c in top]
    fallback = None
    for (u, _), res in zip(top, pages):
        if res is None: continue
        u = tidy(u)
        text = (res.get("markdown") or {}).get("data") or ""
        js = (res.get("json") or {}).get("data") if (res.get("json") or {}).get("success") else None
        js = js if isinstance(js, dict) else {}
        if not text and not js: continue
        if re.search(r"/(news|article|articles|blog|tips|promotions?|offers?|help)(/|$)|news\.", u, re.I):
            continue                                               # an article or an offers page, not prices
        if not (js.get("is_this_match") or named(text[:20000])):
            continue                                               # some other page on the bookmaker's site
        there = js.get({"home": "home_win", "draw": "draw", "away": "away_win"}[outcome])
        try: there = float(there) if there and float(there) > 1.0 else None
        except (TypeError, ValueError): there = None
        match_page = named(urllib.parse.urlparse(u).path.replace("-", " ").replace("_", " "))   # the address itself names the match
        if (there and abs(there - price) / price < 0.012) or (not there and match_page and price_on_page(text, price)):
            return "seen", u, text[:30000], there or price
        if there:
            return "moved", u, text[:30000], there
        if match_page or js.get("is_this_match"):
            fallback = fallback or ("not shown", u, text[:30000], None)     # the bookmaker's page for the match, price not read
    return fallback or ("unreadable", "", "", None)


RESEARCH = "research:"


def ctx_research_odds(key, home, away, ask=None):
    """Let Context.dev find the prices wherever they are on the web: it searches, opens the pages it finds and
    reports each bookmaker's three prices with the page it read them from. Returns (books, text, reason, links)."""
    ask = ask or (lambda task, shape: ctx_answers(key, task, shape, deep=True))
    js = ask("Open odds comparison pages (such as oddschecker, oddsportal, betexplorer, oddspedia) and bookmakers' own "
             "match pages. A whole number among fractional prices means that number to one (2 means 2/1 = 3.00). "
             "Find the current pre-match betting odds for the football (soccer) match %s vs %s. List the home win, draw "
             "and away win odds offered by as many different named bookmakers as you can find, for example bet365, "
             "William Hill, Paddy Power, Sky Bet, Ladbrokes, Coral, Betfred, Unibet, BetVictor, 888sport, Betway, bwin, "
             "Pinnacle, 10bet, BoyleSports. Give DECIMAL odds (convert fractions: 6/4 is 2.50). Only include a bookmaker "
             "when you read all three of its prices on a web page, and give that page's address. Do not estimate, "
             "average or repeat one bookmaker's prices for another." % (home, away),
             {"bookmakers": [{"name": "", "home": 0.0, "draw": 0.0, "away": 0.0, "source_url": ""}]})
    rows = js.get("bookmakers") or []
    books = coherent(valid_books(rows))
    links = {(r.get("name") or "").strip()[:40]: r.get("source_url") for r in rows if str(r.get("source_url") or "").startswith("http")}
    sites = sorted({re.sub(r"^https?://(www\.)?", "", u).split("/")[0] for k, u in links.items() if k in books})
    text = "PRICES FOUND BY CONTEXT.DEV WEB RESEARCH\n" + "\n".join(
        "%-26s %.2f  %.2f  %.2f   %s" % ((k,) + tuple(v) + (links.get(k, ""),)) for k, v in sorted(books.items()))
    why = "" if len(books) >= scan.MIN_BOOKS else "web research found %d bookmaker(s) with all three prices" % len(books)
    return books, text, why, {k: v for k, v in links.items() if k in books}, sites


def league_pages(hub_url, links):
    """From the site's football page, the links that are league pages (two folders under /football/)."""
    host = urllib.parse.urlparse(hub_url).netloc
    out = []
    for u in links:
        pr = urllib.parse.urlparse(u)
        if pr.netloc == host and re.fullmatch(r"/football/[a-z0-9-]+/[a-z0-9-]+/?", pr.path) and not re.search(r".-(v|vs)-.", pr.path):
            if u.rstrip("/") not in out: out.append(u.rstrip("/"))
    return out


def run_hub(eng, key, hubs, tested=False, default_kick=None, limit=40, workers=4, log=print, research=True, deep=False, within_h=30):
    """Read each odds page in `hubs`, follow its match links, read every match page and watch the readable ones.
    Returns how many matches were added."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pages, links = [], []
    for hub in hubs:
        lk, why = ctx_links(key, hub)
        found = match_pages(hub, lk)
        log("%s: %d links, %d match pages%s" % (hub, len(lk), len(found), (" (%s)" % why) if why else ""))
        links += lk
        pages += [x for x in found if x[0] not in {y[0] for y in pages}]
        if deep:                                                    # follow every league page too
            leagues = league_pages(hub, lk)[:80]
            log("  following %d league pages ..." % len(leagues))
            with ThreadPoolExecutor(max_workers=workers) as ex2:
                for lg, (lk2, _) in zip(leagues, ex2.map(lambda u: ctx_links(key, u), leagues)):
                    f2 = match_pages(lg, lk2)
                    if f2: log("    %-70s %d matches" % (lg.replace("https://www.", "")[:70], len(f2)))
                    pages += [x for x in f2 if x[0] not in {y[0] for y in pages}]
    known = {w["url"] for w in eng.watch} | {w["home"] + "|" + w["away"] for w in eng.watch}
    todo = [x for x in pages if x[0] not in known and (x[1] + "|" + x[2]) not in known][:limit]
    if not pages:
        for u in links[:40]: log("   " + u)
        log("No match links recognised. The first links found are listed above."); return 0
    if len(todo) < len(pages): log("%d already being watched, %d to read" % (len(pages) - len(todo), len(todo)))
    def one(item):
        u, home, away = item; s = time.time()
        try: books, md, w = ctx_read_odds(key, u)
        except Exception as e: books, md, w = {}, None, str(e)[:120]
        return u, home, away, books, md, w, time.time() - s
    added, ex = 0, ThreadPoolExecutor(max_workers=workers)
    try:
        for f in as_completed([ex.submit(one, x) for x in todo]):
            u, home, away, books, md, w, took = f.result()
            meta = PAGE_META.get(u) or {}
            kick = uk_time_to_epoch(meta.get("kickoff")) or default_kick
            if len(books) >= scan.MIN_BOOKS:
                if meta.get("in_play") or (kick and kick <= time.time()):
                    note = "already started, not watched"
                elif deep and (not kick or kick > time.time() + within_h * 3600):
                    note = "kicks off later than %d hours from now%s, not watched yet" % (within_h, "" if kick else " or time unknown")
                else:
                    added += eng.add_watch(u, meta.get("home_team") or home, meta.get("away_team") or away,
                                           scope="tested" if (tested or TESTED_PATHS.search(u)) else "untested", kickoff_ts=kick)
                    note = "watching" + (", kick-off %s" % time.strftime("%a %H:%M", time.localtime(kick)) if kick else " (no kick-off time on the page)")
            else:
                note = ("page not readable: " if md is None else "") + (w or "")
                if research and not meta.get("in_play"):
                    rb, _, rw, _, sites = ctx_research_odds(key, home, away)
                    if len(rb) >= scan.MIN_BOOKS:
                        added += eng.add_research(home, away, scope="tested" if (tested or TESTED_PATHS.search(u)) else "untested", kickoff_ts=kick)
                        books = rb; note = "watching via web research (%s). The page itself: %s" % (", ".join(sites)[:60], note[:80])
                    else:
                        note += " | " + rw
            log("  %-34s %2d bookmakers %4.0fs  %s" % ((home + " v " + away)[:34], len(books), took, note))
    except KeyboardInterrupt:
        log("\nStopped early. Matches already marked 'watching' are saved.")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return added


SOON = "any professional football league or international competition anywhere in the world"


def find_odds_page(home, away, search, read, log=print, workers=2, tries=6):
    """Search for odds pages for one match and read the candidates two at a time.
    Prints what happened with every page, so a failure shows where it failed. Returns (url, bookmakers, best_count)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cands = []
    for q in ("%s vs %s odds comparison all bookmakers" % (home, away), "%s v %s betting odds" % (home, away)):
        for u in search(q):
            if u not in cands: cands.append(u)
        if len(cands) >= tries: break
    if not cands:
        log("      search returned no pages"); return "", 0, -1
    site = lambda u: re.sub(r"^https?://(www\.)?", "", u).split("/")[0]
    cands.sort(key=lambda u: 0 if "oddschecker" in u else 1)        # this site is known to load through Context.dev
    def one(u):
        t0 = time.time()
        try: books, md, why = read(u)
        except Exception as e: books, md, why = {}, None, str(e)[:120]
        return u, books, md, why, time.time() - t0
    best, winner = 0, ("", 0)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, u) for u in cands[:tries]]
        for f in as_completed(futs):
            u, books, md, why, took = f.result()
            best = max(best, len(books))
            log("      %-58s %2d bookmakers  %4.0fs  %s" % (re.sub(r"^https?://(www\.)?", "", u)[:58], len(books), took,
                                                           "" if len(books) >= scan.MIN_BOOKS else ("page not readable: " + (why or "")[:70] if md is None else (why or "table not found")[:70])))
            if len(books) >= scan.MIN_BOOKS and len(books) > winner[1]:
                winner = (u, len(books))
    return winner[0], winner[1], best


def discover(key, leagues, days=3, limit=25, ask=None, search=None, read=None, now=None, log=print):
    """Context.dev only.
    1. Research each league's upcoming fixtures and kick-off times.
    2. Search the web for odds pages for each fixture.
    3. Have Context.dev read each candidate page and extract every bookmaker's prices; keep the first page
       that yields at least five bookmakers.  Returns one row per fixture saying what happened."""
    ask = ask or (lambda task, shape: ctx_answers(key, task, shape))
    search = search or (lambda q: ctx_search(key, q))
    read = read or (lambda url: ctx_read_odds(key, url))
    now = now or time.time()
    stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    for lg in leagues:
        js = ask("It is now %s. List football (soccer) matches in %s that kick off within the next %d days and have "
                 "NOT started yet. Use official fixture lists or major sports sites. Give the home team, the away team "
                 "and the kick-off time in UTC written as YYYY-MM-DDTHH:MM:SSZ. List up to 20, soonest first. If there "
                 "are none, return an empty list." % (stamp, lg, days), {"matches": [{"home": "", "away": "", "kickoff_utc": ""}]})
        found = [m for m in (js.get("matches") or []) if m.get("home") and m.get("away")]
        log("  %s: %d upcoming match(es) found" % (lg[:60], len(found)))
        for m in found:
            kick = iso_utc(m.get("kickoff_utc"))
            row = {"league": lg, "home": m["home"].strip(), "away": m["away"].strip(), "kickoff": kick, "url": "", "books": 0, "status": ""}
            rows.append(row)
            if kick and not (now < kick <= now + days * 86400):
                row["status"] = "already started, or not in the next %d days" % days; continue
            if sum(1 for r in rows if r["status"].startswith("watching")) >= limit:
                row["status"] = "skipped (limit reached)"; continue
            name = "%s v %s" % (row["home"], row["away"])
            log("    %s" % name)
            url, n, best = find_odds_page(row["home"], row["away"], search, read, log)
            if url:
                row["url"], row["books"] = url, n
                row["status"] = "watching (%d bookmakers read from %s)" % (n, re.sub(r"^https?://(www\.)?", "", url).split("/")[0])
            else:
                row["status"] = "no search results" if best < 0 else "no readable odds page (best had %d bookmakers)" % best
            log("      -> %s" % row["status"])
    return rows


def send_sms(sid, token, sender, to, text):
    """A real text message through Twilio (needs a Twilio account: account SID, auth token and a Twilio phone number)."""
    import base64
    req = urllib.request.Request("https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid,
                                 data=urllib.parse.urlencode({"To": to, "From": sender, "Body": text}).encode("utf-8"), method="POST",
                                 headers={"Authorization": "Basic " + base64.b64encode(("%s:%s" % (sid, token)).encode()).decode(),
                                          "Content-Type": "application/x-www-form-urlencoded"})
    urllib.request.urlopen(req, timeout=15).close()


def notify(title, message, click="", post=None, sms=None, problems=None):
    """Send an alert. Returns how many were sent; reasons for any failure are appended to `problems`.
    OFFSIDE_WEBHOOK = a Discord or Slack webhook address. OFFSIDE_NTFY = a topic name on ntfy.sh (free phone push).
    Text message: TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM and OFFSIDE_SMS_TO."""
    problems = problems if problems is not None else []
    def send(url, payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "Offside/1.0 (+https://github.com)"})
        urllib.request.urlopen(req, timeout=15).close()
    post = post or send
    def attempt(label, fn):
        for n in range(2):
            try:
                fn(); return 1
            except urllib.error.HTTPError as e:
                why = "%s: HTTP %d %s" % (label, e.code, e.read().decode("utf-8", "replace")[:200])
                if e.code == 429: time.sleep(2.5)
            except Exception as e:
                why = "%s: %s" % (label, str(e)[:200])
            if n == 0: time.sleep(1.0)
        problems.append(why); return 0
    sent, env = 0, os.environ.get
    if env("TWILIO_SID") and env("TWILIO_TOKEN") and env("TWILIO_FROM") and env("OFFSIDE_SMS_TO"):
        text = ("%s. %s %s" % (title, message, click)).strip()[:600]
        sent += attempt("text message", lambda: (sms or send_sms)(env("TWILIO_SID"), env("TWILIO_TOKEN"), env("TWILIO_FROM"), env("OFFSIDE_SMS_TO"), text))
    if env("OFFSIDE_NTFY"):
        body = {"topic": env("OFFSIDE_NTFY"), "title": title, "message": message, "priority": 4, "tags": ["soccer"]}
        if click: body["click"] = click; body["actions"] = [{"action": "view", "label": "Open live prices", "url": click}]
        sent += attempt("phone alert", lambda: post("https://ntfy.sh/", body))
    hook = (env("OFFSIDE_WEBHOOK") or "").strip().strip('"')
    if hook:
        text = ("**%s**\n%s%s" % (title, message, ("\n" + click) if click else ""))[:1900]
        payload = {"content": text} if "discord" in hook else {"content": text, "text": text}
        sent += attempt("chat alert", lambda: post(hook, payload))
    for why in problems: print("  alert not sent. " + why)
    return sent


# --------------------------------------------------------------------------- the engine
class Engine:
    def __init__(self, odds_key=None, ctx_key=None, interval=600, regions="uk,eu", horizon_h=72, now_fn=time.time,
                 odds_fn=None, scores_fn=None, scrape_fn=None, result_fn=None, news_fn=None, verify_fn=None, shot_fn=None, persist=True,
                 in_play=False, confirm_fn=None, alert_fn=None):
        self.odds_key, self.ctx_key, self.interval, self.regions = odds_key, ctx_key, interval, regions
        self.horizon, self.now, self.persist = horizon_h * 3600, now_fn, persist
        self.odds_fn = odds_fn or (lambda lg: api_get("/sports/%s/odds" % lg, {"regions": self.regions, "markets": "h2h",
                                                                               "oddsFormat": "decimal", "dateFormat": "iso", "includeLinks": "true"}, self.odds_key))
        self.scores_fn = scores_fn or (lambda lg: api_get("/sports/%s/scores" % lg, {"daysFrom": 2, "dateFormat": "iso"}, self.odds_key)[0])
        if scrape_fn:                                              # tests hand in plain page text
            self.read_page = lambda url: (lambda md, why: (scan.parse_page(fractions_to_decimals(md)) if md else {}, md, why))(*scrape_fn(url))
        else:
            known = lambda url: url in self.last_good or any(w["url"] == url and w.get("home") and w.get("away") and w.get("kickoff") for w in self.watch)
            self.read_page = lambda url: ctx_read_odds(self.ctx_key, url, light=known(url))
        self.research = (lambda home, away: ctx_research_odds(self.ctx_key, home, away))
        raw = confirm_fn or ((lambda book, home, away, price, outcome: ctx_confirm_at_source(self.ctx_key, book, home, away, price, outcome))
                             if (ctx_key and not scrape_fn) else (lambda book, home, away, price, outcome: ("seen", "", "", price)))
        def confirm(book, home, away, price, outcome="home"):
            try: out = raw(book, home, away, price, outcome)
            except TypeError: out = raw(book, home, away, price)        # older four-argument form
            out = tuple(out) + (None,) * 4
            return out[0], out[1], out[2], out[3]
        self.confirm = confirm
        self.shot = shot_fn or ((lambda url: ctx_screenshot(self.ctx_key, url)) if ctx_key else (lambda url: (None, "")))
        self.result = result_fn or ((lambda rec: lookup_result(self.ctx_key, rec)) if ctx_key else (lambda rec: None))
        self.news = news_fn or ((lambda rec: lookup_news(self.ctx_key, rec)) if ctx_key else (lambda rec: ("", "")))
        self.verify = verify_fn or self._verify
        self.lock = threading.Lock()
        self.leagues = load("leagues.json", []) if persist else []
        self.watch = load("watch.json", []) if persist else []
        self.record = load("record.json", []) if persist else []
        self.pending, self.live, self.matches = {}, [], []
        self.last_good, self.unread = {}, set()
        self.read_at, self.league_cache, self.reads, self.backlog = {}, {}, 0, 0
        self.in_play, self.failed, self.hist, self.last_real_read = in_play, set(), {}, None
        self.feed, self.wake = [], threading.Event()
        self.force_settle = False
        self.prefetch, self.reading, self.read_note = {}, set(), {}
        self.alert = alert_fn or (lambda title, message, click: threading.Thread(target=notify, args=(title, message, click), daemon=True).start())
        self.pot_start, self.stake_pct = 1000.0, 2.0
        self.alert_min = 3                                         # alert on flags that pass at least this setting (3 = all)
        self.parallel = 6                                          # pages read side by side
        self.bg = (lambda fn: fn()) if (scrape_fn or odds_fn) else (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.stats = {"pages": 0, "prices": 0, "screenshots": 0, "news": 0, "since": self.now()}
        self.missed = []
        self.seen = {}
        if persist: self._restore()
        self.sources, self.quota, self.last_read, self.score_checked = {}, None, None, {}

    def _save(self, name, obj):
        if self.persist: save(name, obj)

    def _restore(self):
        """Pick up where the last run stopped: the last prices read, flags waiting for their second read, counters."""
        st = load("state.json", {})
        self.stats = dict(self.stats, **(st.get("stats") or {}))
        self.missed = st.get("missed") or []
        self.hist = st.get("hist") or {}
        self.seen = st.get("seen") or {}
        relabel = False
        for w in self.watch:                                       # the league label follows the page address
            want = "tested" if TESTED_PATHS.search(w.get("url") or "") else w.get("scope", "untested")
            if want != w.get("scope"): w["scope"] = want; relabel = True
        if relabel: save("watch.json", self.watch)
        for k, s in list(self.seen.items()):                        # the one mislabelled sighting, corrected to match its record
            if s.get("match") == "Georgia v Hungary" and s.get("book") == "Betfair Sportsbook" and abs(s.get("price", 0) - 2.40) < 0.001:
                s["book"] = "Star Sports"; s["recorded"] = True
        fixed = False
        for r in self.record:
            if r.get("verify") == "seen" and r.get("source_price") is None:   # no real check was run for this one
                r["verify"] = "no site"; fixed = True
            if r.get("home") == "Georgia" and r.get("away") == "Hungary" and r.get("book") == "Betfair Sportsbook" and abs(r.get("price", 0) - 2.40) < 0.001:
                # recorded while bookmaker names were out of step with their codes: the saved page shows this price was Star Sports's
                r["book"], r["verify"], r["source_price"], r["link"] = "Star Sports", "no site", None, r.get("url") or ""
                fixed = True
        by_match = {}
        for r in self.record:
            if r["result"] is not None: by_match.setdefault(r["mid"], set()).add(r.get("score") or "")
        for r in self.record:                                       # two bets on one match showing different scores: look the score up again, once
            if r["result"] is not None and len(by_match.get(r["mid"], ())) > 1:
                r["result"], r["profit"], r["score"], r["checked"] = None, None, "", 0; r.pop("goals", None); fixed = True
        if fixed: save("record.json", self.record)
        if not self.seen:                                          # first run of this version: rebuild today's sightings from the read log
            try:
                idx = {"home": 0, "draw": 1, "away": 2}
                with open(os.path.join(DATA, "reads.jsonl"), encoding="utf-8") as f:
                    for line in f:
                        row = json.loads(line)
                        for book, outcome, price, points in row.get("flags") or []:
                            self._sight(row["match"], book, outcome, price, (row.get("avg") or [0, 0, 0])[idx[outcome]], points, row["t"], row.get("kickoff"))
            except Exception:
                pass
        scope_of = {w["url"]: ("tested" if TESTED_PATHS.search(w.get("url") or "") else w.get("scope", "untested")) for w in self.watch}
        for url, g in (st.get("last_good") or {}).items():
            m = g.get("m") or {}
            m["books"] = {k: tuple(v) for k, v in (m.get("books") or {}).items()}
            m.setdefault("proof", ""); m.setdefault("links", {})
            if url in scope_of: m["scope"] = scope_of[url]
            if any(re.search(r" \([A-Z0-9]{2,3}\)$", k) for k in m["books"]):
                continue                                           # read while bookmaker names were out of step: read it again
            self.last_good[url] = {"t": g.get("t", 0), "m": m}
        for row in st.get("pending") or []:
            k = tuple(row["key"])
            if row.get("recorded") or self.now() - row.get("first_seen", 0) < 20 * 60:
                self.pending[k] = {x: row[x] for x in row if x != "key"}
        rows = []
        for url, g in self.last_good.items():                      # show the last known prices straight away
            m = g["m"]
            if m.get("kickoff") and m["kickoff"] < self.now(): continue
            res = scan.analyse(m["books"])
            rows.append({"id": m["id"], "home": m["home"], "away": m["away"], "kickoff": m["kickoff"], "scope": m["scope"],
                         "league": m["league_title"], "books": len(m["books"]), "avg": res["avg"] if res else None,
                         "nearest": nearest(m["books"]), "flags": len(res["flags"]) if res else 0, "stale": g["t"]})
        self.matches = sorted(rows, key=lambda s: (s["kickoff"] or 9e18))
        if rows: self.last_read = max(g["t"] for g in self.last_good.values())

    def _sight(self, match, book, outcome, price, average, points, t, kickoff=None):
        k = "%s|%s|%s" % (match, book, outcome)
        s = self.seen.get(k)
        if s and t - s["last"] < 1800:
            s["last"] = t; s["reads"] += 1
            if price > s["price"]: s["price"], s["average"] = price, average
            s["points"] = max(s["points"], points)
        else:
            self.seen[k] = {"match": match, "book": book, "outcome": outcome, "price": price, "average": average, "points": points,
                            "first": t, "last": t, "reads": 1, "kickoff": kickoff}

    def _remember(self):
        if not self.persist: return
        save("state.json", {"stats": self.stats, "missed": self.missed[-200:], "hist": self.hist, "seen": self.seen,
                            "pending": [dict({x: y for x, y in v.items() if x != "source"}, key=list(k)) for k, v in self.pending.items()],
                            "last_good": {u: {"t": g["t"], "m": {x: g["m"][x] for x in g["m"] if x != "proof"}} for u, g in self.last_good.items()}})

    def _verify(self, link, price):
        """Open the bookmaker's own page through Context.dev and look for the price."""
        if not self.ctx_key: return "off", ""
        if not link: return "no link", ""
        md, _ = scan.scrape(self.ctx_key, link)
        if md is None: return "unreadable", ""
        return ("seen" if price_on_page(md, price) else "not shown"), md

    # ----- what to watch
    def add_league(self, key, title=""):
        with self.lock:
            if any(l["key"] == key for l in self.leagues): return False
            self.leagues.append({"key": key, "title": title or key, "scope": "tested" if key in TESTED_LEAGUES else "untested"})
            self._save("leagues.json", self.leagues); return True

    def add_research(self, home, away, scope="untested", kickoff_ts=None):
        return self.add_watch(RESEARCH + home + "|" + away, home, away, scope=scope, kickoff_ts=kickoff_ts)

    def check_results(self):
        """Look up results now for every paper bet whose match should be over (the dashboard's 'Check results now')."""
        n = 0
        for r in self.record:
            if r["result"] is None and r.get("kickoff") and self.now() > r["kickoff"] + 100 * 60:
                r["checked"] = 0; r["kickoff_due"] = True; n += 1
        self.force_settle = True; self.wake.set()
        return n

    def read_now(self, mid):
        """Read one match straight away, on its own (the dashboard's 'Read now' button). The row is updated the moment
        the read comes back; the next pass then checks those same prices for anything out of line without reading again."""
        url = mid[5:] if mid.startswith("page:") else mid
        w = next((x for x in self.watch if x["url"] == url), None)
        if not w:
            return False
        if url in self.reading:
            return True
        self.reading.add(url); self.read_note[mid] = {"state": "reading", "t": self.now()}
        def job():
            t0 = time.time()
            try:
                out = self._read_watch(w)
                books = out[0]
                if len(books) >= scan.MIN_BOOKS:
                    self.prefetch[url] = (out, self.now())
                    res = scan.analyse(books)
                    with self.lock:
                        for row in self.matches:
                            if row["id"] == "page:" + url:
                                row.update(books=len(books), avg=res["avg"] if res else None, nearest=nearest(books), read_t=self.now(), stale=None)
                        self.last_read = self.now()
                    self.read_note[mid] = {"state": "done", "t": self.now(), "books": len(books), "secs": round(time.time() - t0, 1)}
                else:
                    self.read_note[mid] = {"state": "failed", "t": self.now(), "why": (out[2] or "no prices came back")[:140]}
            except Exception as e:
                self.read_note[mid] = {"state": "failed", "t": self.now(), "why": str(e)[:140]}
            finally:
                self.reading.discard(url)
                self.read_at[url] = -1e18; self.wake.set()             # the next pass takes it first
        self.bg(job)
        return True

    def pot(self):
        """The paper account: every recorded price is a paper bet of stake_pct of the pot at that moment."""
        bal, staked_open = self.pot_start, 0.0
        for r in sorted(self.record, key=lambda r: r["t"]):
            if not r.get("stake"):
                r["stake"] = round(max(1.0, bal * self.stake_pct / 100.0), 2)
            if r["result"] is not None: bal += r["stake"] * r["profit"]
            else: staked_open += r["stake"]
        return {"start": self.pot_start, "now": round(bal, 2), "open": round(staked_open, 2), "stake_pct": self.stake_pct,
                "open_bets": sum(1 for r in self.record if r["result"] is None)}

    def _read_watch(self, w):
        t0 = time.time()
        out = self._read_watch_inner(w)
        meta = READ_META.get(w["url"], {}) if not w["url"].startswith(RESEARCH) else {}
        self.feed.append({"t": self.now(), "match": "%s v %s" % (w.get("home") or "?", w.get("away") or "?"), "books": len(out[0]),
                          "secs": round(time.time() - t0, 1), "ok": len(out[0]) >= scan.MIN_BOOKS, "request_id": meta.get("request_id", ""),
                          "fresh": bool(meta.get("fresh")), "method": meta.get("method", "") if len(out[0]) >= scan.MIN_BOOKS else ""})
        del self.feed[:-40]
        return out

    def _read_watch_inner(self, w):
        """(books, proof text, reason, {bookmaker: page it was read from}, label)"""
        if w["url"].startswith(RESEARCH):
            try:
                books, text, why, links, sites = self.research(w["home"], w["away"])
            except Exception as e:
                return {}, None, str(e)[:160], {}, "Web research"
            return books, text, why, links, "Web research, %d site%s" % (len(sites), "" if len(sites) == 1 else "s")
        books, md, why = self.read_page(w["url"])
        return books, md, why, {}, re.sub(r"^https?://(www\.)?", "", w["url"]).split("/")[0]

    def add_watch(self, url, home="", away="", kickoff="", scope="untested", kickoff_ts=None):
        with self.lock:
            if any(w["url"] == url for w in self.watch): return False
            if self.persist:
                have = {w["url"] for w in self.watch}
                self.watch += [w for w in load("watch.json", []) if w.get("url") and w["url"] not in have and w["url"] != url]
            self.watch.append({"url": url, "home": home, "away": away, "kickoff": kickoff_ts or parse_kickoff(kickoff), "scope": scope})
            self._save("watch.json", self.watch); return True

    def cadence(self, kickoff, url=None):
        """Seconds between reads of one match: the base pace in the last 3 hours, slower when kick-off is far off.
        A page with a flag waiting for its second sighting is read again after about a minute, so a price that is
        really there gets confirmed before it moves."""
        if url and (url in self.failed or any(k[0] == "page:" + url and not v.get("recorded") for k, v in self.pending.items())):
            return min(self.interval, 60)                          # a failed read, or a flag to confirm: try again in a minute
        if kickoff and self.now() > kickoff:
            return min(self.interval, 120)                         # in play (only when in-play reading is switched on)
        if not kickoff: return self.interval
        left = kickoff - self.now()
        return max(self.interval, 3600) if left > 86400 else (max(self.interval, 1200) if left > 3 * 3600 else self.interval)

    # ----- gather every match and its prices
    def _gather(self):
        out = []
        for lg in list(self.leagues):
            if self.now() - self.read_at.get(lg["key"], -1e18) < self.interval - 1:      # not due: show the last read
                for m in self.league_cache.get(lg["key"], []):
                    if m["kickoff"] > self.now():
                        out.append(dict(m, stale=self.read_at[lg["key"]])); self.unread.add(m["id"])
                continue
            self.read_at[lg["key"]] = self.now(); self.reads += 1; first = len(out)
            try:
                events, headers = self.odds_fn(lg["key"])
                if headers.get("x-requests-remaining") is not None:
                    self.quota = {"remaining": headers.get("x-requests-remaining"), "used": headers.get("x-requests-used")}
                n = 0
                for ev in events or []:
                    kick = iso_utc(ev.get("commence_time"))
                    if not kick or kick <= self.now() or kick > self.now() + self.horizon:
                        continue
                    n += 1
                    out.append({"id": "api:" + str(ev.get("id")), "league": lg["key"], "league_title": lg["title"], "home": ev.get("home_team") or "",
                                "away": ev.get("away_team") or "", "kickoff": kick, "scope": lg["scope"], "books": event_books(ev),
                                "links": event_links(ev), "proof": json.dumps(ev, indent=1), "url": ""})
                self.sources[lg["key"]] = {"t": self.now(), "matches": n, "error": None}
                self.league_cache[lg["key"]] = out[first:]
            except urllib.error.HTTPError as e:
                self.sources[lg["key"]] = {"t": self.now(), "matches": 0, "error": "HTTP %d %s" % (e.code, e.read().decode()[:120])}
            except Exception as e:
                self.sources[lg["key"]] = {"t": self.now(), "matches": 0, "error": str(e)[:160]}
        due = []
        for w in list(self.watch):
            if w.get("kickoff") and self.now() > w["kickoff"] + (115 * 60 if self.in_play else 0):
                self.sources[w["url"]] = {"t": self.now(), "matches": 0, "error": "match has started"}; continue
            if self.now() - self.read_at.get(w["url"], -1e18) >= self.cadence(w.get("kickoff"), w["url"]) - 1:
                due.append(w)
            else:                                                   # not due yet: keep showing the last read
                self.unread.add("page:" + w["url"])
                old = self.last_good.get(w["url"])
                if old: out.append(dict(old["m"], stale=old["t"], scope=w.get("scope", "untested")))
        def urgency(w):
            forced = self.read_at.get(w["url"], 0) == -1e18 and w["url"] in self.read_at
            waiting = w["url"] in self.failed or any(k[0] == "page:" + w["url"] and not v.get("recorded") for k, v in self.pending.items())
            return (0 if forced else (1 if waiting else 2), w.get("kickoff") or 9e18)
        due.sort(key=urgency)
        cap = 2 * self.parallel
        self.backlog = max(0, len(due) - cap)
        for w in due[cap:]:                                           # these wait for the next pass, which starts straight away
            self.unread.add("page:" + w["url"])
            old = self.last_good.get(w["url"])
            if old: out.append(dict(old["m"], stale=old["t"], scope=w.get("scope", "untested")))
        due = due[:cap]
        def read(w):
            pf = self.prefetch.pop(w["url"], None)                  # already read a moment ago by 'Read now'
            return pf[0] if (pf and self.now() - pf[1] < 240) else self._read_watch(w)
        if len(due) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(self.parallel, len(due))) as ex:
                results = list(ex.map(read, due))
        else:
            results = [read(w) for w in due]
        for w, (books, md, why, links, label) in zip(due, results):
            self.read_at[w["url"]] = self.now(); self.reads += 1
            if len(books) < scan.MIN_BOOKS:                         # this read failed: fall back to the last good one
                self.failed.add(w["url"])
                self.unread.add("page:" + w["url"])
                self.sources[w["url"]] = {"t": self.now(), "matches": 0, "error": why or "only %d bookmaker prices could be read" % len(books)}
                old = self.last_good.get(w["url"])
                if old and self.now() - old["t"] < 3600:
                    out.append(dict(old["m"], stale=old["t"]))
                continue
            self.failed.discard(w["url"])
            if not w["home"]:
                w["home"], w["away"] = teams_from_page(md)
            self.sources[w["url"]] = {"t": self.now(), "matches": 1, "error": None}
            researched = w["url"].startswith(RESEARCH)
            out.append({"id": "page:" + w["url"], "league": "", "league_title": label, "home": w["home"], "away": w["away"],
                        "kickoff": w.get("kickoff"), "scope": w.get("scope", "untested"), "books": books, "links": links,
                        "proof": (md or "") if researched else "ODDS PAGE AS READ THROUGH CONTEXT.DEV\n%s\n\nPRICES EXTRACTED\n%s\n\nPAGE TEXT\n%s" % (
                            w["url"], "\n".join("%-30s %.2f  %.2f  %.2f" % ((k,) + tuple(v)) for k, v in sorted(books.items())), md or ""),
                        "url": "" if researched else w["url"]})
            self.last_good[w["url"]] = {"t": self.now(), "m": out[-1]}
        return out

    # ----- one pass
    def cycle(self):
        live, summary, seen_now = [], [], set()
        self.unread = set(); self.reads = 0
        if self.persist:                                            # matches added from another window since we started
            have = {w["url"] for w in self.watch}
            for w in load("watch.json", []):
                if w.get("url") and w["url"] not in have: self.watch.append(w)
        keep_live = [e for e in self.live]
        already = {(r["mid"], r["book"], r["outcome"]) for r in self.record}
        taken = {(r["mid"], r["outcome"]) for r in self.record}
        fresh_rows = []
        for m in self._gather():
            res = scan.analyse(m["books"]); near = nearest(m["books"])
            playing = bool(m["kickoff"] and self.now() > m["kickoff"])
            if res and not m.get("stale"):
                h = self.hist.setdefault(m["id"], [])
                h.append([round(self.now())] + [round(x, 3) for x in res["avg"]] + [round(x, 3) for x in res["best"]])
                del h[:-60]
            oi = {"home": 0, "draw": 1, "away": 2}.get((near or {}).get("outcome"), 0)
            summary.append({"id": m["id"], "home": m["home"], "away": m["away"], "kickoff": m["kickoff"], "scope": m["scope"],
                            "league": m["league_title"], "books": len(m["books"]), "avg": res["avg"] if res else None,
                            "nearest": near, "flags": len(res["flags"]) if res else 0, "stale": m.get("stale"), "in_play": playing,
                            "read_t": m.get("stale") or self.now(), "url": m.get("url") or "",
                            "hist": [[x[0], x[1 + oi], x[4 + oi]] for x in self.hist.get(m["id"], [])]})
            if m.get("stale"):
                live += [e for e in keep_live if e["mid"] == m["id"]]   # its flags stay on show until the next real read
                continue                                           # old prices are never newly flagged or confirmed
            self.stats["pages"] += 1; self.stats["prices"] += 3 * len(m["books"])
            fresh_rows.append({"t": round(self.now()), "match": "%s v %s" % (m["home"], m["away"]), "kickoff": m["kickoff"], "books": len(m["books"]),
                               "avg": res["avg"] if res else None, "best": res["best"] if res else None,
                               "flags": [[f["book"], f["outcome"], f["price"], f["points"]] for f in (res["flags"] if res else [])],
                               "prices": {k: list(v) for k, v in m["books"].items()}})
            for f in (res["flags"] if res else []):
                self._sight("%s v %s" % (m["home"], m["away"]), f["book"], f["outcome"], f["price"], f["average"], f["points"], self.now(), m["kickoff"])
                k = (m["id"], f["book"], f["outcome"]); seen_now.add(k)
                entry = dict(f, mid=m["id"], url=m["url"], league=m["league"], league_title=m["league_title"], home=m["home"],
                             away=m["away"], kickoff=m["kickoff"], scope=m["scope"], link=(m.get("links") or {}).get(f["book"], ""),
                             in_play=playing)
                if k in already and k not in self.pending:
                    self.pending[k] = {"first_seen": self.now(), "recorded": True}
                if k in self.pending:
                    entry["status"] = "confirmed"; entry["first_seen"] = self.pending[k]["first_seen"]
                    if not self.pending[k].get("recorded"):
                        if k not in already and (m["id"], f["outcome"]) not in taken:
                            taken.add((m["id"], f["outcome"]))
                            self._record(entry, m["proof"])
                            print("  %s  RECORDED: %s v %s, %s %s at %.2f (average %.2f)" % (time.strftime("%H:%M:%S"), m["home"], m["away"],
                                                                                           f["book"], f["outcome"], f["price"], f["average"]))
                        self.pending[k]["recorded"] = True
                    entry["news"] = self.pending[k].get("news", ""); entry["verify"] = self.pending[k].get("verify", "")
                else:
                    entry["status"] = "checking"; entry["first_seen"] = self.now(); entry["news"] = ""; entry["verify"] = ""
                    self.pending[k] = {"first_seen": self.now(), "match": "%s v %s" % (m["home"], m["away"]), "price": f["price"]}
                    if k not in already and f["points"] >= self.alert_min:   # first sighting: tell the phone straight away
                        who = {"home": m["home"] + " to win", "draw": "The draw", "away": m["away"] + " to win"}[f["outcome"]]
                        had = next((r for r in self.record if r["mid"] == m["id"] and r["outcome"] == f["outcome"]), None)
                        try:
                            self.alert("Out of line: %s v %s" % (m["home"], m["away"]),
                                       "%s at %s: %.2f, everyone else averages %.2f (+%d%%). Kick-off %s. On the page, tap %s's price in the %s row to add it to "
                                       "the bet slip. Checking again in a minute.%s" % (
                                           who, f["book"], f["price"], f["average"], round(100 * (f["price"] / f["average"] - 1)),
                                           time.strftime("%a %H:%M", time.localtime(m["kickoff"])) if m["kickoff"] else "time unknown", f["book"],
                                           {"home": m["home"], "draw": "Draw", "away": m["away"]}[f["outcome"]],
                                           (" This result is already in the record at %.2f with %s, so it will not be added a second time." % (had["price"], had["book"])) if had else ""),
                                       m["url"] or "")
                        except Exception:
                            pass
                        if m["url"]:                                  # meanwhile, look for the bookmaker's own page for this match
                            def follow(k=k, book=f["book"], home=m["home"], away=m["away"], price=f["price"], who=who, outcome=f["outcome"]):
                                try:
                                    status, link, text, there = self.confirm(book, home, away, price, outcome)
                                except Exception:
                                    return
                                if k in self.pending: self.pending[k]["source"] = [status, link, (text or "")[:30000], there]
                                sk = "%s v %s|%s|%s" % (home, away, book, outcome)
                                if sk in self.seen: self.seen[sk].update(verify=status, book_link=link or "", book_price=there)
                                if link:
                                    self.alert("Bet at %s: %s v %s" % (book, home, away),
                                               "%s. %s" % (who, "The same price, %.2f, is showing on %s's own site. This link goes straight to the match there." % (price, book)
                                                           if status == "seen" else
                                                           "%s's own site is showing %.2f now (the comparison site had %.2f). This link goes straight to the match there." % (book, there, price)
                                                           if status == "moved" else
                                                           "This is %s's own page for the match. Check the price there before betting." % book), link)
                            self.bg(follow)
                live.append(entry)
        for k in [k for k in self.pending if k not in seen_now and k[0] not in self.unread]:
            gone = self.pending.pop(k)                             # back in line (or kicked off) before confirmation
            if not gone.get("recorded") and gone.get("match"):
                self.missed.append({"t": self.now(), "match": gone["match"], "book": k[1], "outcome": k[2], "price": gone.get("price")})
        if self.persist and fresh_rows:
            try:
                os.makedirs(DATA, exist_ok=True)
                with open(os.path.join(DATA, "reads.jsonl"), "a", encoding="utf-8") as f:
                    for row in fresh_rows: f.write(json.dumps(row) + "\n")
            except Exception:
                pass
        with self.lock:
            self.live = sorted(live, key=lambda e: (e["kickoff"] or 9e18, -e["edge_pct"]))
            self.matches = sorted(summary, key=lambda s: (s["kickoff"] or 9e18))
            self.last_read = self.now()
            if self.reads: self.last_real_read = self.now()
        self.settle()
        self._remember()

    def _record(self, entry, proof_text):
        rid = hashlib.sha1(("%s|%s|%s|%s" % (entry["mid"], entry["book"], entry["outcome"], entry["first_seen"])).encode()).hexdigest()[:10]
        source_link, source_price = "", None
        if entry["url"]:                                           # read from an odds page: check the bookmaker's own site
            done = (self.pending.get((entry["mid"], entry["book"], entry["outcome"])) or {}).get("source")
            if done:
                verify, source_link, page, source_price = (list(done) + [None] * 4)[:4]
            else:
                try: verify, source_link, page, source_price = self.confirm(entry["book"], entry["home"], entry["away"], entry["price"], entry["outcome"])
                except Exception: verify, source_link, page = "unreadable", "", ""
        else:
            try: verify, page = self.verify(entry.get("link") or "", entry["price"])
            except Exception: verify, page = "unreadable", ""
        if page:
            proof_text = "BOOKMAKER'S OWN PAGE, read through Context.dev (%s)\n%s\n\n%s\n\nPRICES FROM ALL BOOKMAKERS\n%s" % (
                entry.get("link") or source_link,
                {"seen": "the same price was found on this page", "moved": "this page showed a different price: %s" % source_price}.get(verify, "the price could not be read from this page"),
                page, proof_text)
        proof, shot = "", ""
        if self.persist:
            os.makedirs(os.path.join(DATA, "proofs"), exist_ok=True)
            proof = "%s.txt" % rid
            try:
                img, ext = self.shot(entry["url"] or entry.get("link") or "") if (entry["url"] or entry.get("link")) else (None, "")
                if img:
                    shot = "%s.%s" % (rid, ext)
                    with open(os.path.join(DATA, "proofs", shot), "wb") as f: f.write(img)
            except Exception:
                shot = ""
            with open(os.path.join(DATA, "proofs", proof), "w", encoding="utf-8") as f:
                f.write("Recorded %s\n%s v %s\n%s %s at %.2f (average %.2f)\nContext.dev request id for this read: %s\n\n%s" % (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.now())), entry["home"], entry["away"],
                    entry["book"], entry["outcome"], entry["price"], entry["average"],
                    (READ_META.get(entry["url"]) or {}).get("request_id", "not available"), proof_text))
        rec = {"id": rid, "t": self.now(), "mid": entry["mid"], "league": entry["league"], "league_title": entry["league_title"],
               "url": entry["url"], "home": entry["home"], "away": entry["away"], "kickoff": entry["kickoff"], "scope": entry["scope"],
               "book": entry["book"], "outcome": entry["outcome"], "price": entry["price"], "average": entry["average"],
               "needed": entry["needed"], "points": entry["points"], "edge_pct": entry["edge_pct"], "proof": proof, "shot": shot,
               "verify": verify, "link": entry.get("link") or source_link or entry["url"], "in_play": bool(entry.get("in_play")),
               "request_id": (READ_META.get(entry["url"]) or {}).get("request_id", ""), "source_price": source_price,
               "news": "", "news_url": "", "result": None, "score": "", "profit": None, "checked": 0}
        if shot: self.stats["screenshots"] += 1
        if entry["home"]:
            try:
                rec["news"], rec["news_url"] = self.news(rec); self.stats["news"] += 1
            except Exception: pass
        self.pending[(entry["mid"], entry["book"], entry["outcome"])]["news"] = rec["news"]
        self.pending[(entry["mid"], entry["book"], entry["outcome"])]["verify"] = verify
        with self.lock:
            self.record.append(rec)
            self.pot()                                             # gives the new entry its paper stake
            self._save("record.json", self.record)
        try:
            self.alert("Confirmed: %s v %s" % (rec["home"], rec["away"]),
                       "Still there a minute later. Added to the system's record: %s at %s, %.2f (average %.2f), stake %.2f.%s" % (
                           {"home": rec["home"] + " to win", "draw": "the draw", "away": rec["away"] + " to win"}[rec["outcome"]], rec["book"],
                           rec["price"], rec["average"], rec.get("stake") or 0,
                           " Also found on the bookmaker's own site." if rec.get("verify") == "seen" else ""),
                       (rec.get("link") if rec.get("verify") in ("seen", "moved", "not shown") and str(rec.get("link") or "").startswith("http") else "") or rec["url"] or "")
        except Exception:
            pass

    def _closing(self):
        """For each recorded price whose match has kicked off, store the average price at the last read before
        kick-off. Price / closing average - 1 is the fairest quick measure of whether the flag was good."""
        idx = {"home": 0, "draw": 1, "away": 2}
        todo = [r for r in self.record if r.get("close_avg") is None and r.get("kickoff") and self.now() > r["kickoff"]]
        if not todo: return
        log = {}
        try:
            with open(os.path.join(DATA, "reads.jsonl"), encoding="utf-8") as f:
                for line in f:
                    row = json.loads(line)
                    if row.get("avg") and row.get("kickoff") and row["t"] <= row["kickoff"]:
                        log[row["match"]] = row                    # later lines overwrite: the last read before kick-off
        except Exception:
            pass
        changed = False
        for r in todo:
            g = self.last_good.get(r.get("url") or "")
            avg, t = None, None
            if g and g["m"].get("books") and g["t"] <= r["kickoff"] and not self.in_play:
                bk = g["m"]["books"]; avg = sum(v[idx[r["outcome"]]] for v in bk.values()) / len(bk); t = g["t"]
            row = log.get("%s v %s" % (r["home"], r["away"]))
            if row and (t is None or row["t"] > t):
                avg, t = row["avg"][idx[r["outcome"]]], row["t"]
            r["close_avg"] = round(avg, 3) if avg else 0              # 0 = no closing read available
            r["close_t"] = t; changed = True
        if changed:
            with self.lock: self._save("record.json", self.record)

    # ----- settle finished matches
    def settle(self):
        try: self._closing()
        except Exception: pass
        early = 100 if self.force_settle else 112
        self.force_settle = False
        due = [r for r in self.record if r["result"] is None and r["home"] and
               self.now() >= ((r["kickoff"] + early * 60) if r["kickoff"] else r["t"] + 4 * 3600)]
        if not due:
            return
        scores = {}
        for lg in {r["league"] for r in due if r["league"]}:
            if self.now() - self.score_checked.get(lg, 0) < 1800:
                continue
            self.score_checked[lg] = self.now()
            try:
                for s in self.scores_fn(lg) or []:
                    if s.get("completed") and s.get("scores"):
                        sc = {x.get("name"): x.get("score") for x in s["scores"]}
                        scores["api:" + str(s.get("id"))] = (int(sc[s["home_team"]]), int(sc[s["away_team"]]))
            except Exception:
                pass
        changed = False
        for r in due:                                               # a score already found for this match is reused
            done = next((x for x in self.record if x["mid"] == r["mid"] and x["result"] is not None and x.get("goals")), None)
            if done: scores[r["mid"]] = tuple(done["goals"])
        for r in due:
            got = scores.get(r["mid"])
            if got is None and self.now() - r.get("checked", 0) >= 600 and (not r["league"] or self.now() >= (r["kickoff"] or r["t"]) + 5 * 3600):
                r["checked"] = self.now()                          # pasted pages, or the odds service has no score yet
                try:
                    res = self.result(r); got = (res["home_goals"], res["away_goals"]) if res else None
                except Exception:
                    got = None
            if got is None:
                continue
            scores[r["mid"]] = got; r["goals"] = [got[0], got[1]]
            won = settle_outcome(r["outcome"], got[0], got[1])
            r["result"], r["score"] = ("won" if won else "lost"), "%d-%d" % got
            r["profit"] = round(r["price"] - 1.0, 3) if won else -1.0
            changed = True
        if changed:
            with self.lock: self._save("record.json", self.record)

    # ----- what the dashboard shows
    def _book_page(self, e):
        """What is known about the bookmaker's own page for a live row: its address, what the check found, the price there."""
        rec = next((r for r in self.record if r["mid"] == e["mid"] and r["book"] == e["book"] and r["outcome"] == e["outcome"]), None)
        if rec and rec.get("verify") in ("seen", "moved", "not shown") and str(rec.get("link") or "").startswith("http") and rec.get("link") != rec.get("url"):
            return {"book_link": rec["link"], "verify": rec["verify"], "book_price": rec.get("source_price")}
        src = (self.pending.get((e["mid"], e["book"], e["outcome"])) or {}).get("source")
        if src and src[1]:
            return {"book_link": src[1], "verify": src[0], "book_price": (list(src) + [None] * 4)[3]}
        return {"book_link": "", "book_price": None}

    def state(self):
        with self.lock:
            settled = [r for r in self.record if r["result"] is not None]
            profit = sum(r["profit"] for r in settled)
            closed = [r for r in self.record if r.get("close_avg")]
            return {"now": self.now(), "interval": self.interval, "last_read": self.last_real_read or self.last_read, "quota": self.quota,
                    "in_play_mode": self.in_play,
                    "stats": dict(self.stats, credits=CREDITS.get("left")), "missed": len(self.missed), "backlog": self.backlog,
                    "feed": list(reversed(self.feed))[:14], "pot": self.pot(), "version": VERSION,
                    "read_note": {k: v for k, v in self.read_note.items() if self.now() - v["t"] < 180},
                    "alerts": {"ntfy": os.environ.get("OFFSIDE_NTFY") or "", "webhook": bool(os.environ.get("OFFSIDE_WEBHOOK")),
                               "sms": (os.environ.get("OFFSIDE_SMS_TO") or "")[-4:] if os.environ.get("TWILIO_SID") else ""},
                    "checks": {v: sum(1 for r in self.record if (r.get("verify") or "") == v) for v in ("seen", "moved", "not shown", "unreadable")},
                    "seen": sorted(({**s, "recorded": any(r["book"] == s["book"] and r["outcome"] == s["outcome"] and "%s v %s" % (r["home"], r["away"]) == s["match"]
                                                          for r in self.record),
                                     "backed": next(({"book": r["book"], "price": r["price"]} for r in self.record
                                                     if r["outcome"] == s["outcome"] and "%s v %s" % (r["home"], r["away"]) == s["match"] and r["book"] != s["book"]), None)}
                                    for s in self.seen.values() if self.now() - s["last"] < 36 * 3600),
                                   key=lambda s: -s["last"])[:60],
                    "closing": {"n": len(closed), "beat": sum(1 for r in closed if r["price"] > r["close_avg"]),
                                "edge": (sum(r["price"] / r["close_avg"] - 1 for r in closed) / len(closed)) if closed else None},
                    "has_odds_key": bool(self.odds_key), "has_ctx_key": bool(self.ctx_key),
                    "leagues": [dict(l, source=self.sources.get(l["key"], {})) for l in self.leagues],
                    "pages": [dict(w, source=self.sources.get(w["url"], {})) for w in self.watch],
                    "live": [dict(e, **self._book_page(e)) for e in self.live], "matches": list(self.matches)[:80], "record": list(reversed(self.record)),
                    "summary": {"recorded": len(self.record), "settled": len(settled),
                                "won": sum(1 for r in settled if r["result"] == "won"), "profit": round(profit, 2),
                                "roi": (profit / len(settled)) if settled else None},
                    "evidence": load("evidence.json", None)}


def settle_outcome(outcome, hg, ag):
    return (outcome == "home" and hg > ag) or (outcome == "draw" and hg == ag) or (outcome == "away" and ag > hg)


# --------------------------------------------------------------------------- evidence for the chart
def build_evidence(matches):
    """Month-by-month running profit of the tested rule at mainstream bookmakers, one unit staked per bet,
    and the same picks at the AVERAGE price. The gap between the two lines is what the outlier price is worth."""
    matches = [m for m in matches if m.get("date")]
    seasons = sorted({m["season"] for m in matches}, key=lambda c: int(c[:2]) + (100 if int(c[:2]) < 50 else 0))
    half = seasons[len(seasons) // 2] if seasons else ""
    older = {s for s in seasons if seasons.index(s) < seasons.index(half)} if seasons else set()
    split_month = min((m["date"][:7] for m in matches if m["season"] == half), default="")
    out = {"matches": len(matches), "leagues": len({m["league"] for m in matches}),
           "first": min(m["date"] for m in matches) if matches else "", "last": max(m["date"] for m in matches) if matches else "",
           "baseline_roi": (odds.stats([(m, i, m["avg"][i]) for m in matches for i in range(3)]) or {}).get("roi"),
           "split": half, "split_month": split_month, "settings": {}}
    for alpha in (0.03, 0.04, 0.05):
        bets = sorted(odds.bets_paper(matches, alpha, "named"), key=lambda b: b[0]["date"])
        s = odds.stats(bets)
        if not s:
            continue
        so = odds.stats([b for b in bets if b[0]["season"] in older]); sn = odds.stats([b for b in bets if b[0]["season"] not in older])
        series, cf, ca, n, month = [], 0.0, 0.0, 0, None
        for m, i, price in bets:
            ym = m["date"][:7]
            if month is not None and ym != month:
                series.append([month, round(cf, 2), round(ca, 2), n])
            month = ym; n += 1
            win = m["y"] == i
            cf += (price - 1.0) if win else -1.0
            ca += (m["avg"][i] - 1.0) if win else -1.0
        if month: series.append([month, round(cf, 2), round(ca, 2), n])
        years = max(1e-9, (dt.date.fromisoformat(out["last"]) - dt.date.fromisoformat(out["first"])).days / 365.25)
        per_season = []
        for sc in seasons:
            ss = odds.stats([b for b in bets if b[0]["season"] == sc])
            per_season.append({"season": sc[:2] + "/" + sc[2:], "bets": ss["n"] if ss else 0, "roi": ss["roi"] if ss else 0.0,
                               "units": round(ss["roi"] * ss["n"], 1) if ss else 0.0})
        units = s["roi"] * s["n"]
        worst = min((x["units"] for x in per_season), default=0.0)
        pot = [{"stake_pct": int(f * 100), "stake": int(1000 * f), "total": round(units * 1000 * f), "per_year_pct": round(100 * units * f / years, 1),
                "worst_season": round(worst * 1000 * f)} for f in (0.02, 0.05)]
        out["settings"][str(int(round(alpha * 100)))] = {
            "bets": s["n"], "roi": s["roi"], "t": s["t"], "win": s["win"], "avg_odds": s["avg_odds"], "per_week": s["n"] / (years * 52.0),
            "older_roi": so["roi"] if so else None, "newer_roi": sn["roi"] if sn else None,
            "older_bets": so["n"] if so else 0, "newer_bets": sn["n"] if sn else 0, "series": series,
            "seasons": per_season, "units": round(units, 1), "years": round(years, 1), "pot": pot,
            "winning_seasons": sum(1 for x in per_season if x["units"] > 0), "n_seasons": len(per_season)}
    return out


def load_backtest_matches(first=2015, last=2026):
    matches = []
    for code in odds.season_codes(first, last):
        for lg in odds.LEAGUES:
            try: matches += odds.parse(odds.fetch(code, lg), code, lg)
            except Exception: continue
    return matches


# --------------------------------------------------------------------------- the dashboard page
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offside</title>
<style>
:root{--paper:#EEF2F7;--panel:#FFFFFF;--ink:#14213D;--muted:#57667E;--line:#D3DBE7;--soft:#E6ECF4;--card:#F2B705;--card-ink:#2E2200;
--win:#1B7F4F;--loss:#C8102E;--chart2:#8C9DB6;--top:#14213D;--top-ink:#F4F7FB;--top-muted:#AEBBD0;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--paper:#0B1220;--panel:#141E33;--ink:#EAF0F8;--muted:#9BABC2;--line:#28365380;--soft:#1B2740;
--win:#57C793;--loss:#FF7084;--chart2:#62748F;--top:#070C17;--top-ink:#F4F7FB;--top-muted:#9BABC2;color-scheme:dark}}
*{box-sizing:border-box}
html{font-size:17px}
body{margin:0;background:var(--paper);color:var(--ink);font:1rem/1.5 "Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif}
.d,h1,h2,h3,th{font-family:Bahnschrift,"DIN Alternate","Roboto Condensed","Arial Narrow",Arial,sans-serif}
.in{max-width:1280px;margin:0 auto;padding:0 28px}
.top{background:var(--top);color:var(--top-ink)}
.top .in{display:flex;flex-wrap:wrap;gap:16px 48px;align-items:center;justify-content:space-between;padding-top:26px;padding-bottom:26px}
.brand{display:flex;align-items:center;gap:18px}
.flag{width:46px;height:36px;flex:none;background:
 conic-gradient(var(--card) 0 25%, var(--loss) 0 50%, var(--card) 0 75%, var(--loss) 0) 0 0/100% 100%;border-radius:2px;transform:rotate(-8deg)}
h1{font-size:3.4rem;line-height:1;margin:0;font-weight:700;letter-spacing:.5px}
.tagline{margin:4px 0 0;color:var(--top-muted);max-width:46ch}
.status{font-size:.9rem;color:var(--top-muted);line-height:1.6}
.status b{color:var(--top-ink);font-weight:600}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--win);margin-right:8px;vertical-align:1px}
.dot.off{background:var(--loss)}
main{padding-bottom:70px}
section{margin-top:44px}
h2{font-size:1.9rem;margin:0;font-weight:600;line-height:1.15}
h3{font-size:1.25rem;margin:0 0 6px;font-weight:600}
.sub{color:var(--muted);margin:6px 0 16px;max-width:74ch}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:4px}
table{border-collapse:collapse;width:100%;min-width:820px}
thead th{background:var(--soft);text-align:left;font-weight:600;font-size:.95rem;color:var(--muted);padding:11px 12px;white-space:nowrap}
td{padding:13px 12px;border-top:1px solid var(--line);vertical-align:middle}
.num{font-family:Bahnschrift,"DIN Alternate","Roboto Condensed","Arial Narrow",Arial,sans-serif;font-variant-numeric:tabular-nums;font-size:1.2rem}
.match{font-weight:600}.small{font-size:.8rem;color:var(--muted)}
.nw{white-space:nowrap}td.when{white-space:nowrap;width:1%}td.m{min-width:190px}td.st{min-width:200px;max-width:250px}td.st .tag,td.m .tag{white-space:normal;display:inline-block;border-radius:8px;line-height:1.3;padding:2px 8px}
.yc{display:inline-block;background:var(--card);color:var(--card-ink);font-family:Bahnschrift,"DIN Alternate","Arial Narrow",Arial,sans-serif;
font-size:1.5rem;font-weight:700;font-variant-numeric:tabular-nums;padding:12px 10px 9px;min-width:66px;text-align:center;border-radius:4px;
box-shadow:3px 3px 0 rgba(20,33,61,.22);transform:rotate(-3deg)}
.rc{display:inline-block;background:var(--loss);color:#fff;padding:2px 9px;border-radius:3px;font-weight:600;font-size:.85rem}
.won{color:var(--win);font-weight:700}.pos{color:var(--win)}.neg{color:var(--loss)}
.tag{font-size:.78rem;border:1px solid var(--line);padding:1px 8px;border-radius:10px;color:var(--muted);white-space:nowrap}
.tag.ok{border-color:var(--win);color:var(--win)}
tr.fresh td{animation:fresh 2.4s ease-out 1}
@keyframes fresh{from{background:rgba(242,183,5,.4)}to{background:transparent}}
@media (prefers-reduced-motion:reduce){tr.fresh td{animation:none}}
.quiet{padding:26px 24px;display:flex;gap:22px;align-items:center}
.quiet .big{font-family:Bahnschrift,"DIN Alternate","Arial Narrow",Arial,sans-serif;font-size:1.45rem;font-weight:600}
.quiet p{margin:4px 0 0;color:var(--muted);max-width:70ch}
.ghost{width:58px;height:78px;border:2px dashed var(--line);border-radius:4px;flex:none;transform:rotate(-3deg)}
.setup{padding:24px}.setup code{background:var(--soft);padding:2px 6px;border-radius:3px;font-size:.88rem}
.meter{display:inline-block;width:120px;height:8px;background:var(--soft);border-radius:4px;overflow:hidden;vertical-align:middle;margin-right:8px}
.meter i{display:block;height:100%;background:var(--chart2)}.meter.hot i{background:var(--card)}
form{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
input[type=url]{flex:1 1 340px;padding:11px 13px;font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:4px}
button{font:inherit;font-weight:600;padding:11px 18px;border:2px solid var(--ink);background:var(--ink);color:var(--paper);cursor:pointer;border-radius:4px}
.seg{display:inline-flex;border:2px solid var(--ink);border-radius:4px;overflow:hidden}
.seg button{border:0;border-radius:0;background:transparent;color:var(--ink);padding:9px 16px}
.seg button+button{border-left:2px solid var(--ink)}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--paper)}
button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid var(--card);outline-offset:2px}
.grid{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:28px;align-items:start}
@media (max-width:940px){.grid{grid-template-columns:1fr}h1{font-size:2.5rem}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:22px}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px 26px;margin:18px 0 10px}
.figs span{display:block;font-size:.8rem;color:var(--muted);line-height:1.3;margin-top:2px}
.figs b{font-family:Bahnschrift,"DIN Alternate","Arial Narrow",Arial,sans-serif;font-size:2.1rem;font-weight:600;font-variant-numeric:tabular-nums;line-height:1}
svg{display:block;width:100%;height:auto}
.work{display:flex;flex-wrap:wrap;gap:10px 34px;margin-top:14px;padding:16px 20px;background:var(--panel);border:1px solid var(--line);border-radius:4px}
.work b{font-family:Bahnschrift,"DIN Alternate","Arial Narrow",Arial,sans-serif;font-size:1.5rem;font-weight:600;font-variant-numeric:tabular-nums;display:block;line-height:1.1}
.work span{font-size:.8rem;color:var(--muted)}
.spark{display:block;width:150px;height:34px}
.pot{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px 28px;padding:18px 20px;background:var(--top);color:var(--top-ink);border-radius:4px;margin-bottom:14px}
.pot b{font-family:Bahnschrift,"DIN Alternate","Arial Narrow",Arial,sans-serif;font-size:1.9rem;font-weight:600;font-variant-numeric:tabular-nums;display:block;line-height:1.1}
.pot span{font-size:.8rem;color:var(--top-muted)}
.feed{font-size:.86rem;line-height:1.75;padding:14px 18px;max-height:300px;overflow:auto;font-variant-numeric:tabular-nums}
.feed .ok{color:var(--win);font-weight:600}.feed .bad{color:var(--loss);font-weight:600}
button.mini{padding:4px 10px;font-size:.78rem;border-width:1px;background:transparent;color:var(--ink);margin-top:6px}
button.mini[disabled]{opacity:.55;cursor:default}
a.go{display:inline-block;padding:5px 11px;border-radius:3px;background:var(--ink);color:var(--paper,#fff);font-weight:600;font-size:.84rem;text-decoration:none}
a.go:hover,a.go:focus{outline:2px solid var(--card,#f5c400);outline-offset:1px}
.thumb{display:block;width:92px;height:58px;object-fit:cover;object-position:top;border:1px solid var(--line);border-radius:3px}
.key{display:flex;flex-wrap:wrap;gap:6px 24px;font-size:.85rem;color:var(--muted);margin-top:10px}
.key i{display:inline-block;width:24px;height:3px;vertical-align:middle;margin-right:7px}
ol,ul{padding-left:20px;margin:8px 0}li{margin:7px 0;max-width:68ch}
a{color:inherit}
footer{margin-top:48px;font-size:.8rem;color:var(--muted);border-top:1px solid var(--line);padding-top:16px}
</style></head><body>
<div class="top"><div class="in"><div class="brand"><div class="flag" aria-hidden="true"></div><div><h1>Offside</h1>
<p class="tagline">Bookmaker prices that are out of line with everyone else's, found before kick-off.</p></div></div>
<div class="status" id="status" role="status">Starting…</div></div></div>
<main class="in">
<section aria-labelledby="h-live"><h2 id="h-live">Out of line right now</h2>
<p class="sub">Each row is one bookmaker paying clearly more than the average of all the others for the same result.
To act on a row you would back that result at that bookmaker yourself, at that price. Offside reads the page again about a minute later and records the price, as one unit staked, only if it is still there.</p>
<div class="scroll" id="live"></div>
<div class="work" id="work" style="display:none" aria-label="What Context.dev has done since switch-on"></div></section>

<section aria-labelledby="h-feed"><h2 id="h-feed">Live reads</h2>
<p class="sub">Every read as it happens: the match, how many bookmakers came back, how long Context.dev took, and its request id. "Fresh" means the page was fetched from the site for this read, not served from a cache. Use "Read now" on any match to trigger one yourself.</p>
<div class="scroll feed" id="feed"></div></section>

<section aria-labelledby="h-seen"><h2 id="h-seen">Out of line earlier</h2>
<p class="sub">Every price that has been out of line in the last day and a half, whether or not it lasted long enough to be recorded.</p>
<div class="scroll" id="seen"></div></section>

<section aria-labelledby="h-all"><h2 id="h-all">Every match being watched</h2>
<p class="sub" id="allsub"></p><div class="scroll" id="all"></div>
</section>

<section aria-labelledby="h-ev"><h2 id="h-ev">Does this rule hold up?</h2>
<p class="sub" id="evsub"></p>
<div class="grid"><div class="panel"><div class="seg" id="switch" role="group" aria-label="How strict the rule is"></div>
<div class="figs" id="figs"></div><p class="small" id="tnote" style="margin:0 0 14px"></p><div id="chart"></div>
<div class="key"><span><i style="background:var(--win)"></i>Betting the out-of-line price</span>
<span><i style="background:var(--chart2)"></i>The same bets at the average price</span></div>
<h3 style="margin-top:26px">Season by season</h3><p class="small" id="seasnote" style="margin:0 0 8px"></p><div id="seasons"></div>
<h3 style="margin-top:26px">What that means for a pot of money</h3><div id="pot"></div></div>
<div class="panel"><h3>How to read the chart</h3>
<ul><li>One unit is staked on every bet the rule would have made, in date order.</li>
<li>The gap between the two lines is what the out-of-line price is worth. The picks alone do not make money.</li>
<li>The rule and its settings come from a study published in 2017 using data up to 2015. Every match here was played after that, so the rule could not have been fitted to it.</li>
<li id="basel"></li>
<li id="yieldnote"></li>
<li>The return is on money staked, per bet. A bet settles in about two hours, so the same pot is used again and again. That is why a 13% return per bet can be a much larger return on the pot over a year.</li>
<li>The ceiling is capacity, not the edge. The rule fires a few dozen times a season, and bookmakers cut the stakes of accounts that win this way.</li></ul></div></div></section>

<section aria-labelledby="h-rec"><h2 id="h-rec">The system's record</h2>
<p class="sub">One shared record for everyone using Offside. It is not a personal account. It shows what would have happened if every result Offside confirmed since switch-on had been backed once, at the price recorded, with 2% of a bank of 1,000 on each. No real money is involved. The proof of each price is its screenshot and saved page, captured at that moment, with Context.dev's request id.</p>
<p class="sub"><button type="button" class="mini" id="notif" style="margin:0">Turn on alerts in this browser</button> <span class="small" id="notifmsg"></span>
<button type="button" class="mini" id="settle" style="margin:0 0 0 8px">Check results now</button> <span class="small" id="settlemsg"></span></p>
<div class="pot" id="paper"></div>
<p class="sub" id="recsub"></p><div class="scroll" id="record"></div></section>

<section class="grid"><div><h2>How it works</h2><ol>
<li>Context.dev finds upcoming matches from league pages, renders each match's odds page as a UK visitor, and its parse rules return every bookmaker's price as structured data.</li>
<li>The average price across all bookmakers is taken as the fair view of the match.</li>
<li>A bookmaker's price is flagged when it pays more than that view allows, even after a safety cushion of 3 to 5 points.</li>
<li>If the price is still there a minute later, it is added to the system's record with a screenshot and a saved copy of the page as proof. Each result is added once per match.</li>
<li>Context.dev then searches for the bookmaker's own page for that match and looks for the same price there, as a second, independent source.</li>
<li>Context.dev also searches for team news that might explain why that bookmaker is out of line.</li>
<li>After the match, the final score is looked up and the entry is marked won or lost.</li></ol></div>
<div><h2>Limits</h2><ul>
<li>In the tested leagues the medium rule fires once or twice a week. This is a small edge.</li>
<li>Bookmakers restrict accounts that bet this way, and can cancel a bet placed at a mistaken price.</li>
<li>The test covers 18 European leagues. Anything else is marked as outside the tested leagues.</li>
<li>Offside finds prices. It does not place bets, and betting can lose money.</li></ul></div></section>
<footer>Offside version VERSION_HERE. Fixtures, odds pages, price extraction, screenshots and team news: Context.dev.
Historical results and odds: football-data.co.uk. The rule follows Kaunitz, Zhong and Kreiner (2017).</footer></main>
<script>
let S=null, setting="4", seen=new Set(), first=true;
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const clock=t=>t?new Date(t*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}):"";
const day=t=>t?new Date(t*1000).toLocaleString([], {weekday:"short",hour:"2-digit",minute:"2-digit"}):"";
function until(k,now){ if(!k) return "time not given"; const d=k-now; if(d<=0) return "started";
  const h=Math.floor(d/3600), m=Math.floor(d%3600/60);
  return h>=48? "in "+Math.floor(h/24)+" days" : h>0? "in "+h+" h "+m+" min" : "in "+Math.max(1,m)+" min"; }
const names={home:"Home win",draw:"Draw",away:"Away win"};
const matchName=e=> e.home? esc(e.home)+" v "+esc(e.away) : "<span class='small'>teams not read from the page</span>";
const scope=e=> (e.scope==="tested"? "" : " <span class='tag'>outside the tested leagues</span>")+(e.in_play&&e.book? " <span class='tag'>in play, not covered by the test</span>":"");
const plural=(n,w)=>n+" "+w+(n===1?"":(w.endsWith("ch")?"es":"s"));
function checked(v, there){ return v==="seen"? "<span class='tag ok'>same price found on the bookmaker's own site</span>" :
  v==="moved"? "<span class='tag'>bookmaker's own site showed "+(there? Number(there).toFixed(2):"a different price")+"</span>" :
  v==="not shown"? "<span class='tag'>bookmaker's match page found, price not readable</span>" :
  v==="unreadable"? "<span class='tag'>bookmaker's match page not found or blocked</span>" :
  (v==="no link"||v==="no site")? "<span class='tag'>bookmaker's own site not checked</span>" : ""; }

function renderStatus(){
  const nL=S.leagues.length, nP=S.pages.length, bad=S.leagues.concat(S.pages).filter(x=>x.source&&x.source.error&&x.source.error!=="match has started");
  let h="<span class='dot"+(S.last_read?"":" off")+"'></span>";
  h+= S.last_read? "<b>"+plural(nP+ (nL? S.matches.length:0),"match")+"</b> watched"+
      (S.matches.length<nP? " ("+S.matches.length+" of "+nP+" read so far)":"")+"<br>Prices last read at <b>"+esc(clock(S.last_read))+"</b>. Every "+(Math.round(S.interval/6)/10)+" min in a match's last 3 hours, and again within a minute when a price is out of line"+(S.in_play_mode?"<br>In-play reading is on":"") : "First read in progress";
  if(S.quota) h+="<br>Odds data credits left this month: <b>"+esc(S.quota.remaining)+"</b>";
  if(S.backlog) h+="<br>"+plural(S.backlog,"page")+" queued for the next pass";
  if(bad.length) h+="<br>"+plural(bad.length,"source")+" could not be read";
  if(!S.has_ctx_key) h+="<br>No Context.dev key set: page checks and news are off";
  $("status").innerHTML=h;
}
function renderLive(){
  const L=S.live;
  if(!S.leagues.length && !S.pages.length){ $("live").innerHTML="<div class='setup'><div class='d' style='font-size:1.45rem;font-weight:600'>Nothing is being watched yet.</div>"+
    "<p>Stop the app, find matches, then start it again:</p><p><code>python app.py discover --soon</code> finds matches kicking off in the next day and an odds page for each.<br>"+
    "<code>python app.py serve</code> starts Offside. You can also paste an odds page address into the box below.</p></div>"; return; }
  if(!L.length){ $("live").innerHTML="<div class='quiet'><div class='ghost' aria-hidden='true'></div><div><div class='big'>No price is out of line right now.</div>"+
    "<p>That is normal. In the tested leagues the medium rule fires once or twice a week. The table below shows how close each match is.</p></div></div>"; return; }
  let h="<table><thead><tr><th>Kick-off</th><th>Match</th><th>Result to back</th><th>Bookmaker</th><th>Price</th><th>Everyone else</th><th>How far out of line</th><th>Status</th></tr></thead><tbody>";
  for(const e of L){ const k=e.mid+"|"+e.book+"|"+e.outcome, fresh=!first&&!seen.has(k); seen.add(k);
    const ev=S.evidence&&S.evidence.settings&&S.evidence.settings[String(e.points)];
    h+="<tr"+(fresh?" class='fresh'":"")+"><td class='when'><span class='num'>"+esc(until(e.kickoff,S.now))+"</span><div class='small'>"+esc(day(e.kickoff))+"</div></td>"+
    "<td class='m'><span class='match'>"+matchName(e)+"</span>"+scope(e)+
    (e.book_link? "<div style='margin-top:4px'><a class='go' href='"+esc(e.book_link)+"' target='_blank' rel='noopener'>Bet at "+esc(e.book)+"</a></div>":"")+
    "<div class='small'>"+(e.url? "<a href='"+esc(e.url)+"' target='_blank' rel='noopener'>"+(e.book_link?"all bookmakers' live prices":"open the live prices")+"</a>" : esc(e.league_title))+"</div></td><td>"+names[e.outcome]+"</td><td class='nw'>"+esc(e.book)+"</td>"+
    "<td><span class='yc'>"+e.price.toFixed(2)+"</span></td>"+
    "<td><span class='num'>"+e.average.toFixed(2)+"</span><div class='small'>average. Rule needs above "+e.needed.toFixed(2)+"</div></td>"+
    "<td><span class='num pos'>+"+(100*(e.price/e.average-1)).toFixed(0)+"%</span> <span class='small nw'>above the average</span>"+
    "<div class='small'>passes the minus "+e.points+" setting"+(ev? " ("+(ev.roi>=0?"+":"")+(100*ev.roi).toFixed(0)+"% in the test)":"")+"</div></td>"+
    "<td class='st'>"+(e.status==="confirmed"?"<span class='tag ok'>confirmed</span>":"<span class='tag'>checking again in about a minute</span>")+
    (e.verify?"<div style='margin-top:5px'>"+checked(e.verify, e.book_price)+"</div>":"")+
    (e.news?"<div class='small' style='margin-top:5px'>"+esc(e.news)+"</div>":"")+"</td></tr>"; }
  $("live").innerHTML=h+"</tbody></table>"; first=false;
}
function renderAll(){
  const M=S.matches, extra=waitingRows();
  $("allsub").textContent = M.length? "The bar shows how close the most generous price is to the line at the medium setting. A full bar means it is out of line." :
    (S.pages.length? plural(S.pages.length,"match")+" saved. Prices appear as each one is read." : "No matches are being watched yet.");
  if(!M.length && !extra){ $("all").innerHTML="<div class='setup small'>Nothing to show yet.</div>"; return; }
  let h="<table><thead><tr><th>Kick-off</th><th>Match</th><th>Bookmakers</th><th>Average price: home, draw, away</th><th>Closest to the line</th><th>How it has moved</th></tr></thead><tbody>";
  for(const m of M){ const n=m.nearest; let c="<span class='small'>too few bookmakers to judge</span>";
    if(n){ const pct=Math.max(0,Math.min(1,(n.ratio-0.85)/0.15)); c="<div class='nw'><span class='meter"+(n.ratio>=1?" hot":"")+"'><i style='width:"+(100*pct).toFixed(0)+"%'></i></span>"+
      "<span class='num'>"+n.price.toFixed(2)+"</span> <span class='small'>needs above "+n.needed.toFixed(2)+"</span></div>"+
      "<div class='small'>"+esc(n.book)+", "+names[n.outcome].toLowerCase()+"</div>"; }
    h+="<tr><td class='when'><span class='num'>"+esc(until(m.kickoff,S.now))+"</span><div class='small'>"+esc(day(m.kickoff))+"</div></td>"+
    "<td><span class='match'>"+matchName(m)+"</span>"+scope(m)+(m.in_play?" <span class='tag'>in play</span>":"")+"<div class='small'>"+esc(m.league)+", read "+esc(ago(m.read_t))+" at "+esc(clock(m.read_t))+"</div>"+
    ((m.kickoff&&m.kickoff>S.now)? (((S.read_note||{})[m.id]||{}).state==="reading"? "<button type='button' class='mini' disabled>Reading…</button>" :
      "<button type='button' class='mini' onclick='readNow("+JSON.stringify(m.id).replace(/'/g,"&#39;")+", this)'>Read now</button>"):"")+readNote(m)+"</td><td><span class='num'>"+m.books+"</span></td>"+
    "<td class='num nw'>"+(m.avg? m.avg.map(a=>a.toFixed(2)).join("&nbsp;&nbsp;&nbsp;"):"<span class='small'>n/a</span>")+"</td><td>"+c+"</td><td>"+spark(m.hist)+"</td></tr>"; }
  $("all").innerHTML=h+extra+"</tbody></table>";
}
function spark(h){
  // the average price (grey) and the best price on offer (yellow) over the last reads, for the result nearest the line
  if(!h || h.length<2) return "<span class='small'>builds up as it is read</span>";
  const W=150,H=34,xs=h.map(p=>p[0]),vs=h.flatMap(p=>[p[1],p[2]]),x0=Math.min(...xs),x1=Math.max(...xs)+1,y0=Math.min(...vs),y1=Math.max(...vs);
  const X=v=>2+(v-x0)/(x1-x0)*(W-4), Y=v=>H-3-(y1===y0?0.5:(v-y0)/(y1-y0))*(H-6);
  const line=i=>h.map((p,j)=>(j?"L":"M")+X(p[0]).toFixed(1)+" "+Y(p[i]).toFixed(1)).join(" ");
  return "<svg class='spark' viewBox='0 0 "+W+" "+H+"' role='img' aria-label='Average and best price over the last reads'>"+
    "<path d='"+line(1)+"' fill='none' stroke='var(--chart2)' stroke-width='1.6'/><path d='"+line(2)+"' fill='none' stroke='var(--card)' stroke-width='2'/></svg>"+
    "<div class='small'>"+h[0][1].toFixed(2)+" to "+h[h.length-1][1].toFixed(2)+" average</div>";
}
function waitingRows(){
  // watched matches that have no prices on the board yet: say so, and say why
  const have=new Set(S.matches.map(m=>m.id)); let h="";
  for(const w of S.pages){ if(have.has("page:"+w.url)) continue;
    const src=w.source||{}, why = src.error? (src.error==="match has started"? "Kicked off. No longer read." : "Last read failed: "+src.error) : "Waiting for its first read.";
    h+="<tr><td class='when'><span class='num'>"+esc(until(w.kickoff,S.now))+"</span><div class='small'>"+esc(day(w.kickoff))+"</div></td>"+
       "<td><span class='match'>"+matchName(w)+"</span>"+scope(w)+"</td><td colspan='4'><span class='small'>"+esc(why).slice(0,260)+"</span></td></tr>"; }
  return h;
}
function renderRecord(){
  const R=S.record, m=S.summary, c=S.closing||{};
  let t = m.recorded===0? "Nothing recorded yet. Entries appear here once a flagged price is still there on a second read." :
    plural(m.recorded,"confirmed price")+" backed, "+m.settled+" finished, "+m.won+" won. A win pays the stake times the price minus one, a loss costs the stake."+
    (m.settled? " Return on stakes so far "+(m.roi>=0?"+":"")+(100*m.roi).toFixed(1)+"%. This is far too few bets to prove anything." : " Results appear after each match ends.");
  const k=S.checks||{}, nk=(k.seen||0)+(k.moved||0)+(k["not shown"]||0)+(k.unreadable||0);
  if(nk) t+=" Second source: for "+nk+" of these, Context.dev looked for the match on the bookmaker's own site. "+(k.seen||0)+" showed the same price there, "+(k.moved||0)+
    " showed a different price, "+(k["not shown"]||0)+" had a page whose price could not be read, and for "+(k.unreadable||0)+" no page could be found or opened.";
  if(c.n) t+=" Against the closing prices, "+c.beat+" of "+c.n+" recorded prices were still above the average at kick-off, by "+(100*c.edge).toFixed(1)+"% on average.";
  if(S.missed) t+=" "+plural(S.missed,"other price")+" "+(S.missed===1?"was":"were")+" out of line on one read and gone by the next, so "+(S.missed===1?"it is":"they are")+" not counted.";
  $("recsub").textContent=t;
  if(!R.length){ $("record").innerHTML="<div class='setup small'>The record is empty.</div>"; return; }
  let h="<table><thead><tr><th>Confirmed</th><th>Match</th><th>Backed</th><th>Price taken</th><th>At kick-off</th><th>Outcome</th><th>Profit</th><th>Proof</th></tr></thead><tbody>";
  for(const r of R){ const out = r.result==="won"? "<span class='won'>Won "+esc(r.score)+"</span>" : r.result==="lost"? "<span class='rc'>Lost "+esc(r.score)+"</span>" : "<span class='small'>not finished</span>";
    const close = r.close_avg? "<span class='num'>"+r.close_avg.toFixed(2)+"</span><div class='small "+(r.price>r.close_avg?"pos":"neg")+"'>"+(r.price>r.close_avg?"still ":"")+((100*(r.price/r.close_avg-1))>=0?"+":"")+(100*(r.price/r.close_avg-1)).toFixed(0)+"% against the closing average</div>" :
      (r.close_avg===0? "<span class='small'>no read before kick-off</span>" : "<span class='small'>not kicked off</span>");
    h+="<tr><td class='when'>"+esc(day(r.t))+"<div class='small'>"+(r.kickoff? "kick-off "+esc(day(r.kickoff)):"")+"</div></td><td class='m'><span class='match'>"+matchName(r)+"</span>"+scope(r)+
    (r.verify?"<div style='margin-top:4px'>"+checked(r.verify, r.source_price)+"</div>":"")+
    (r.news?"<div class='small' style='max-width:34ch;margin-top:4px'>"+esc(r.news)+"</div>":"")+"</td>"+
    "<td>"+names[r.outcome]+"<div class='small'>at "+esc(r.book)+"</div><div class='small'>"+(r.stake? "stake "+r.stake.toFixed(2):"")+", passes minus "+r.points+"</div>"+
    "</td>"+
    "<td><span class='num'>"+r.price.toFixed(2)+"</span><div class='small'>average then "+r.average.toFixed(2)+"</div></td><td>"+close+"</td><td>"+out+"</td>"+
    "<td><span class='num "+(r.profit>0?"pos":r.profit<0?"neg":"")+"'>"+(r.profit==null?"":(r.profit>0?"+":"")+(r.profit*(r.stake||1)).toFixed(2))+"</span>"+
    (r.profit==null?"":"<div class='small'>"+(r.profit>0?"+":"")+r.profit.toFixed(2)+" units</div>")+"</td>"+
    "<td>"+(r.shot? "<a href='/proof/"+esc(r.shot)+"' target='_blank' rel='noopener'><img class='thumb' src='/proof/"+esc(r.shot)+"' alt='Screenshot of the odds page when this price was recorded'></a>":"")+
    (r.proof? "<a class='small' href='/proof/"+esc(r.proof)+"' target='_blank' rel='noopener'>saved page</a>":"<span class='small'>none</span>")+
    (r.request_id? "<div class='small'>Context.dev request "+esc(r.request_id.slice(0,8))+"</div>":"")+"</td></tr>"; }
  $("record").innerHTML=h+"</tbody></table>";
}
function renderSeen(){
  const L=S.seen||[];
  if(!L.length){ $("seen").innerHTML="<div class='setup small'>Nothing has been out of line yet.</div>"; return; }
  const dur=s=>{const d=s.last-s.first; return s.reads<2? "one read" : (d<90? "about a minute" : (d<3600? Math.round(d/60)+" min" : (d/3600).toFixed(1)+" h"));};
  let h="<table><thead><tr><th>First seen</th><th>Match</th><th>Result</th><th>Bookmaker</th><th>Best price</th><th>Average</th><th>Lasted</th><th>What happened</th></tr></thead><tbody>";
  for(const s of L){ const live=S.now-s.last<420 && (!s.kickoff || s.kickoff>S.now);
    h+="<tr><td class='when'>"+esc(day(s.first))+"</td><td class='m'><span class='match'>"+esc(s.match)+"</span></td><td class='nw'>"+names[s.outcome]+"</td><td class='nw'>"+esc(s.book)+"</td>"+
    "<td><span class='num'>"+s.price.toFixed(2)+"</span><div class='small pos'>+"+(100*(s.price/s.average-1)).toFixed(0)+"% above the average</div></td>"+
    "<td><span class='num'>"+s.average.toFixed(2)+"</span><div class='small'>passes minus "+s.points+"</div></td><td class='nw'>"+dur(s)+"<div class='small'>"+plural(s.reads,"read")+"</div></td>"+
    "<td class='st' style='min-width:230px;max-width:300px;white-space:normal'>"+(s.recorded? "<span class='tag ok' style='white-space:normal'>confirmed and added to the record</span>" :
      (s.reads>=2&&s.backed)? "<span class='tag ok'>confirmed</span><div class='small' style='margin-top:4px'>Not added again: this result is already in the record at "+s.backed.price.toFixed(2)+" with "+esc(s.backed.book)+".</div>" :
      live? "<span class='tag'>still out of line</span>" :
      s.reads>=2? "<span class='tag'>seen on "+s.reads+" reads, not added to the record</span>" : "<span class='tag'>gone before it could be confirmed</span>")+
      (s.verify? "<div style='margin-top:4px'>"+checked(s.verify, s.book_price).replace("class='tag", "style='white-space:normal' class='tag")+"</div>":"")+"</td></tr>"; }
  $("seen").innerHTML=h+"</tbody></table>";
}
function renderFeed(){
  const F=S.feed||[];
  if(!F.length){ $("feed").innerHTML="<span class='small'>Reads appear here as they happen.</span>"; return; }
  $("feed").innerHTML=F.map(f=>"<div><b>"+esc(clock(f.t))+"</b>&nbsp; "+esc(f.match)+": "+
    (f.ok? "<span class='ok'>"+f.books+" bookmakers</span>" : "<span class='bad'>read failed</span>")+" in "+f.secs.toFixed(1)+" s"+
    (f.ok&&f.fresh? ", fresh":"")+(f.method? ", via "+esc(f.method):"")+(f.request_id? " <span class='small'>request "+esc(f.request_id.slice(0,8))+"</span>":"")+"</div>").join("");
}
function renderPot(){
  const p=S.pot; if(!p){ $("paper").innerHTML=""; return; }
  const d=p.now-p.start, money=x=>x.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  $("paper").innerHTML="<div><b>"+money(p.now)+"</b><span>the bank now, from a start of "+money(p.start)+"</span></div>"+
    "<div><b>"+(d>=0?"+":"")+money(d)+"</b><span>profit or loss on finished matches</span></div>"+
    "<div><b>"+p.open_bets+"</b><span>still to finish, "+money(p.open)+" staked</span></div>"+
    "<div><b>"+p.stake_pct+"%</b><span>of the bank on every confirmed price</span></div>"+
    "<div><b>"+((S.alerts&&(S.alerts.ntfy||S.alerts.webhook||S.alerts.sms))?"On":"Off")+"</b><span>"+((S.alerts&&S.alerts.sms)? "text messages to the phone ending "+esc(S.alerts.sms) :
      (S.alerts&&S.alerts.ntfy)? "phone alerts, topic "+esc(S.alerts.ntfy) :
      (S.alerts&&S.alerts.webhook)? "alerts to a chat channel" : "phone alerts. Start with --ntfy and a topic name")+"</span></div>";
}
async function readNow(id, btn){ btn.disabled=true; btn.textContent="Reading through Context.dev…";
  try{ await fetch("/api/read",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:id})}); }catch(e){}
  for(const ms of [2500,5000,8000,11000,14000,18000,23000,30000,40000,55000]) setTimeout(tick, ms); }
function readNote(m){ const n=(S.read_note||{})[m.id]; if(!n) return "";
  if(n.state==="reading") return "<div class='small' style='color:var(--ink)'>Reading this page through Context.dev now. It usually takes 10 to 20 seconds.</div>";
  if(n.state==="done") return "<div class='small' style='color:var(--win);font-weight:600'>Read at "+esc(clock(n.t))+": "+n.books+" bookmakers in "+n.secs.toFixed(1)+" s.</div>";
  return "<div class='small' style='color:var(--loss);font-weight:600'>Read failed: "+esc(n.why||"")+"</div>"; }
function ago(t){ const d=Math.max(0,Math.round(S.now-t)); return d<90? d+" s ago" : Math.round(d/60)+" min ago"; }
function renderWork(){
  const s=S.stats||{}; if(!s.pages){ $("work").innerHTML=""; $("work").style.display="none"; return; }
  $("work").style.display="";
  const cell=(n,l)=>"<div><b>"+Number(n||0).toLocaleString()+"</b><span>"+l+"</span></div>";
  $("work").innerHTML=cell(s.pages,"odds pages read through Context.dev")+cell(s.prices,"bookmaker prices read from them")+
    cell((S.seen||[]).length,"prices seen out of line")+cell(S.summary.recorded,"prices recorded")+cell(s.screenshots,"proof screenshots")+cell(s.news,"team-news searches")+
    (s.credits!=null? cell(s.credits,"Context.dev credits left"):"")+
    "<div><b>"+esc(day(s.since))+"</b><span>running since</span></div>";
}
function renderEvidence(){
  const E=S.evidence;
  if(!E || !E.settings || !Object.keys(E.settings).length || !E.split){ $("evsub").textContent="The chart has not been built yet (or was built by an older version). Stop the app and run: python app.py evidence";
    $("switch").innerHTML=""; $("figs").innerHTML=""; $("chart").innerHTML=""; $("basel").textContent=""; $("tnote").textContent="";
    $("seasons").innerHTML=""; $("pot").innerHTML=""; $("seasnote").textContent=""; $("yieldnote").textContent=""; return; }
  $("evsub").textContent="Tested on "+E.matches.toLocaleString()+" matches in "+E.leagues+" European leagues, "+E.first.slice(0,4)+" to "+E.last.slice(0,4)+
    ", using only mainstream bookmakers named in the data.";
  $("basel").textContent="Betting every result at the average price over the same matches lost "+Math.abs(100*E.baseline_roi).toFixed(1)+"%. That is the bookmakers' margin.";
  const labels={"3":"Loose: minus 3","4":"Medium: minus 4","5":"Strict: minus 5"};
  if(!E.settings[setting]) setting=Object.keys(E.settings)[0];
  $("switch").innerHTML=Object.keys(E.settings).map(k=>"<button type='button' aria-pressed='"+(k===setting)+"' data-k='"+k+"'>"+labels[k]+"</button>").join("");
  for(const b of $("switch").querySelectorAll("button")) b.onclick=()=>{setting=b.dataset.k; renderEvidence();};
  const s=E.settings[setting], pc=x=>x==null?"n/a":(x>=0?"+":"")+(100*x).toFixed(1)+"%", sp=E.split.slice(0,2)+"/"+E.split.slice(2);
  $("figs").innerHTML="<div><b>"+pc(s.roi)+"</b><span>return per unit staked, whole period</span></div>"+
    "<div><b>"+pc(s.older_roi)+"</b><span>seasons before "+esc(sp)+" ("+s.older_bets+" bets)</span></div>"+
    "<div><b>"+pc(s.newer_roi)+"</b><span>seasons from "+esc(sp)+" ("+s.newer_bets+" bets)</span></div>"+
    "<div><b>"+s.bets.toLocaleString()+"</b><span>bets, about "+(s.per_week>=1? s.per_week.toFixed(1)+" a week" : "one every "+(1/s.per_week).toFixed(1)+" weeks")+"</span></div>";
  $("tnote").textContent = s.t==null? "" : "t statistic "+s.t.toFixed(1)+". Above 2 means the result is unlikely to be luck. Won "+(100*s.win).toFixed(0)+"% of bets at average odds of "+s.avg_odds.toFixed(2)+".";
  drawChart(s.series, E.split_month, sp);
  $("yieldnote").textContent="This setting returned "+pc(s.roi)+" per bet. Professional bettors typically aim for 2% to 5%.";
  if(!s.seasons){ $("seasons").innerHTML=""; $("pot").innerHTML=""; $("seasnote").textContent="Run: python app.py evidence, to add the season table."; return; }
  $("seasnote").textContent=s.winning_seasons+" of "+s.n_seasons+" seasons made money. Bars show profit in units, one unit staked per bet. A single season has few bets, so it is noisy.";
  drawSeasons(s.seasons);
  let h="<div class='scroll'><table style='min-width:0'><thead><tr><th>Stake per bet from a 1,000 pot</th><th>Profit over "+s.years+" years</th><th>Average a year on the pot</th><th>Worst season</th></tr></thead><tbody>";
  for(const r of s.pot) h+="<tr><td><span class='num'>"+r.stake+"</span> <span class='small'>("+r.stake_pct+"% of the pot)</span></td><td><span class='num pos'>"+(r.total>=0?"+":"")+r.total.toLocaleString()+"</span></td>"+
    "<td><span class='num pos'>"+(r.per_year_pct>=0?"+":"")+r.per_year_pct+"%</span></td><td><span class='num neg'>"+r.worst_season.toLocaleString()+"</span></td></tr>";
  $("pot").innerHTML=h+"</tbody></table></div><p class='small' style='margin:8px 0 0'>Flat stakes, no compounding, from the test. It assumes the recorded price was available every time and the account was never limited.</p>";
}
function drawSeasons(rows){
  const W=820,H=230,L=46,R=10,T=18,B=44, n=rows.length, vals=rows.map(r=>r.units).concat([0]);
  let y0=Math.min(...vals), y1=Math.max(...vals); const pad=(y1-y0)*0.12||1; y0-=pad; y1+=pad;
  const Y=v=>T+(y1-v)/(y1-y0)*(H-T-B), bw=(W-L-R)/n;
  let g="<line x1='"+L+"' x2='"+(W-R)+"' y1='"+Y(0)+"' y2='"+Y(0)+"' stroke='var(--muted)' stroke-width='1.2'/>";
  rows.forEach((r,i)=>{ const x=L+i*bw+bw*0.16, w=bw*0.68, y=r.units>=0?Y(r.units):Y(0), h=Math.max(1,Math.abs(Y(r.units)-Y(0)));
    g+="<rect x='"+x.toFixed(1)+"' y='"+y.toFixed(1)+"' width='"+w.toFixed(1)+"' height='"+h.toFixed(1)+"' rx='2' fill='"+(r.units>=0?"var(--win)":"var(--loss)")+"'/>"+
       "<text x='"+(x+w/2).toFixed(1)+"' y='"+(r.units>=0? y-5 : y+h+13).toFixed(1)+"' text-anchor='middle' font-size='12' fill='var(--ink)'>"+(r.units>0?"+":"")+r.units.toFixed(0)+"</text>"+
       "<text x='"+(x+w/2).toFixed(1)+"' y='"+(H-24)+"' text-anchor='middle' font-size='12' fill='var(--muted)'>"+esc(r.season)+"</text>"+
       "<text x='"+(x+w/2).toFixed(1)+"' y='"+(H-9)+"' text-anchor='middle' font-size='11' fill='var(--muted)'>"+r.bets+" bets</text>"; });
  $("seasons").innerHTML="<svg viewBox='0 0 "+W+" "+H+"' role='img' aria-label='Profit in units by season'>"+g+"</svg>";
}
function drawChart(series, splitMonth, sp){
  if(!series.length){ $("chart").innerHTML=""; return; }
  const W=820,H=360,L=50,R=16,T=24,B=32;
  const tx=ym=>{const p=ym.split("-"); return +p[0]+(+p[1]-1)/12;};
  const xs=series.map(p=>tx(p[0])), x0=Math.min(...xs), x1=Math.max(...xs)+0.01;
  const vals=series.flatMap(p=>[p[1],p[2]]).concat([0]); let y0=Math.min(...vals), y1=Math.max(...vals); const pad=(y1-y0)*0.08||1; y0-=pad; y1+=pad;
  const X=v=>L+(v-x0)/(x1-x0)*(W-L-R), Y=v=>T+(y1-v)/(y1-y0)*(H-T-B);
  const path=i=>series.map((p,j)=>(j?"L":"M")+X(xs[j]).toFixed(1)+" "+Y(p[i]).toFixed(1)).join(" ");
  let g="";
  const step=Math.pow(10,Math.floor(Math.log10((y1-y0)/4))), st=[1,2,5,10].map(m=>m*step).find(s=>(y1-y0)/s<=6);
  for(let v=Math.ceil(y0/st)*st; v<=y1; v+=st){ g+="<line x1='"+L+"' x2='"+(W-R)+"' y1='"+Y(v)+"' y2='"+Y(v)+"' stroke='var(--line)' stroke-width='"+(Math.abs(v)<1e-9?1.8:0.8)+"'/>"+
    "<text x='"+(L-9)+"' y='"+(Y(v)+4)+"' text-anchor='end' font-size='13' fill='var(--muted)'>"+(v>0?"+":"")+Math.round(v)+"</text>"; }
  for(let yr=Math.ceil(x0); yr<=x1; yr++){ g+="<text x='"+X(yr)+"' y='"+(H-9)+"' text-anchor='middle' font-size='13' fill='var(--muted)'>"+yr+"</text>"; }
  if(splitMonth){ const sx=X(tx(splitMonth)); g+="<line x1='"+sx+"' x2='"+sx+"' y1='"+T+"' y2='"+(H-B)+"' stroke='var(--muted)' stroke-dasharray='4 4' stroke-width='1'/>"+
    "<text x='"+(sx+7)+"' y='"+(T+12)+"' font-size='13' fill='var(--muted)'>newer seasons, from "+sp+"</text>"; }
  $("chart").innerHTML="<svg viewBox='0 0 "+W+" "+H+"' role='img' aria-label='Running profit in units, month by month'>"+g+
    "<path d='"+path(2)+"' fill='none' stroke='var(--chart2)' stroke-width='2.2' stroke-linejoin='round'/>"+
    "<path d='"+path(1)+"' fill='none' stroke='var(--win)' stroke-width='3' stroke-linejoin='round'/>"+
    "<text x='"+L+"' y='"+(T-8)+"' font-size='13' fill='var(--muted)'>Running profit in units, one unit staked per bet</text></svg>";
}
let alerted=new Set(), alertsReady=false;
function browserAlerts(){
  if(!("Notification" in window)){ $("notifmsg").textContent="This browser does not support alerts."; return; }
  $("notif").style.display = Notification.permission==="granted"? "none":"";
  $("notifmsg").textContent = Notification.permission==="granted"? "Alerts are on in this browser. You will get one the first time a price is out of line." :
    Notification.permission==="denied"? "Alerts are blocked for this page in the browser's settings." : "";
  for(const e of (S.live||[])){ const k=e.mid+"|"+e.book+"|"+e.outcome+"|"+e.status;
    if(alerted.has(k)) continue; alerted.add(k);
    if(!alertsReady || Notification.permission!=="granted") continue;
    const who={home:e.home+" to win",draw:"The draw",away:e.away+" to win"}[e.outcome];
    const n=new Notification((e.status==="confirmed"?"Confirmed: ":"Out of line: ")+e.home+" v "+e.away,
      {body: who+" at "+e.book+": "+e.price.toFixed(2)+", everyone else averages "+e.average.toFixed(2)+(e.status==="confirmed"?". Added to the system's record.":". Checking again in a minute."), tag:k});
    if(e.url) n.onclick=()=>{ window.open(e.url,"_blank"); n.close(); }; }
  alertsReady=true;
}
$("notif").addEventListener("click", async ()=>{ try{ await Notification.requestPermission(); }catch(e){} browserAlerts();
  if(Notification.permission==="granted") new Notification("Offside alerts are on", {body:"You will get one of these the first time a price is out of line."}); });
$("settle").addEventListener("click", async ()=>{ $("settlemsg").textContent="Looking up final scores through Context.dev…";
  try{ const r=await fetch("/api/settle",{method:"POST"}); const j=await r.json();
    $("settlemsg").textContent = j.checking? "Looking up "+j.checking+" finished match"+(j.checking>1?"es":"")+". Results appear here within a minute." : "No finished matches are waiting for a result."; }
  catch(e){ $("settlemsg").textContent="Could not reach the app."; }
  setTimeout(tick, 15000); setTimeout(tick, 40000); setTimeout(tick, 70000); });
const PAGE_VERSION="VERSION_HERE";
async function tick(){ try{ const r=await fetch("/api/state"); S=await r.json(); renderStatus(); renderLive(); renderAll(); if(S.version && S.version!==PAGE_VERSION){ location.reload(); return; }
  renderRecord(); renderPot(); renderFeed(); renderSeen(); renderWork(); renderEvidence(); browserAlerts(); }
  catch(e){ $("status").innerHTML="<span class='dot off'></span>The scanner is not answering. Is app.py still running?"; } }

tick(); setInterval(tick, 5000);
</script></body></html>
"""


# --------------------------------------------------------------------------- web server
def make_handler(engine):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE.replace("VERSION_HERE", VERSION), "text/html; charset=utf-8")
            if self.path == "/api/state":
                return self._send(200, json.dumps(engine.state()), "application/json")
            m = re.match(r"^/proof/([0-9a-f]{10}\.(txt|jpg|png|webp))$", self.path)
            if m and os.path.exists(os.path.join(DATA, "proofs", m.group(1))):
                if m.group(2) == "txt":
                    with open(os.path.join(DATA, "proofs", m.group(1)), encoding="utf-8") as f:
                        return self._send(200, f.read(), "text/plain; charset=utf-8")
                with open(os.path.join(DATA, "proofs", m.group(1)), "rb") as f:
                    return self._send(200, f.read(), "image/" + ("jpeg" if m.group(2) == "jpg" else m.group(2)))
            self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path == "/api/settle":
                return self._send(200, json.dumps({"ok": True, "checking": engine.check_results()}), "application/json")
            if self.path == "/api/read":
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode() or "{}")
                    return self._send(200, json.dumps({"ok": engine.read_now(body.get("id") or "")}), "application/json")
                except Exception as e:
                    return self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")
            if self.path != "/api/watch":
                return self._send(404, "not found", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode() or "{}")
                url = (body.get("url") or "").strip()
                if not engine.ctx_key:
                    return self._send(200, json.dumps({"ok": False, "error": "Reading odds pages needs CONTEXT_DEV_API_KEY to be set."}), "application/json")
                if not re.match(r"^https?://", url):
                    return self._send(200, json.dumps({"ok": False, "error": "That does not look like a web address."}), "application/json")
                self._send(200, json.dumps({"ok": engine.add_watch(url)}), "application/json")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")

        def log_message(self, *a): pass
    return H


def run_loop(engine, stop, rediscover_hours=0):
    last_find = time.time()
    while not stop.is_set():
        try:
            engine.cycle()
            if engine.reads:
                bad = [k for k, v in engine.sources.items() if v.get("error") and v["error"] != "match has started"]
                print("  %s  read %d source(s): %d matches on the board, %d out of line, %d recorded%s" % (
                    time.strftime("%H:%M:%S"), engine.reads, len(engine.matches), len(engine.live), len(engine.record),
                    ("  | %d could not be read" % len(bad)) if bad else ""))
            hubs = load("hubs.json", [])
            if rediscover_hours and hubs and engine.ctx_key and time.time() - last_find > rediscover_hours * 3600:
                last_find = time.time()
                print("  %s  looking for new matches in the background ..." % time.strftime("%H:%M:%S"))
                threading.Thread(target=lambda: run_hub(engine, engine.ctx_key, hubs, log=lambda *a: None, research=False, workers=2), daemon=True).start()
        except Exception as e:
            print("  scan pass failed: %s" % e)
        if not engine.backlog:
            engine.wake.wait(min(30, engine.interval))
        engine.wake.clear()


# --------------------------------------------------------------------------- self-test
def selftest():
    held = {k: os.environ.pop(k) for k in ("OFFSIDE_WEBHOOK", "OFFSIDE_NTFY", "OFFSIDE_SMS_TO", "TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM") if k in os.environ}
    try:
        return _selftest()
    finally:
        os.environ.update(held)


def _selftest():
    C = []
    clock = [1_000_000.0]
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    def bm(key, title, h, d, a):
        return {"key": key, "title": title, "markets": [{"key": "h2h", "outcomes": [{"name": "Alpha FC", "price": h}, {"name": "Beta United", "price": a}, {"name": "Draw", "price": d}]}]}
    slow = [2.75]
    def events():
        return [{"id": "e1", "commence_time": iso(clock[0] + 7200), "home_team": "Alpha FC", "away_team": "Beta United", "bookmakers": [
                    bm("bet365", "Bet365", 2.30, 3.40, 3.00), bm("williamhill", "William Hill", 2.25, 3.30, 3.10), bm("unibet", "Unibet", 2.35, 3.45, 2.95),
                    bm("betfred", "Betfred", 2.30, 3.40, 3.00), bm("tenbet", "10Bet", 2.28, 3.35, 3.05),
                    dict(bm("slow", "SlowBook", slow[0], 3.30, 2.90), link="https://slowbook.example/alpha-v-beta"),
                    bm("betfair_ex_uk", "Betfair Exchange", 3.40, 3.60, 3.20)]},
                {"id": "e2", "commence_time": iso(clock[0] - 600), "home_team": "Alpha FC", "away_team": "Beta United", "bookmakers": []},
                {"id": "e3", "commence_time": iso(clock[0] + 30 * 86400), "home_team": "Alpha FC", "away_team": "Beta United", "bookmakers": []}]
    scores = []
    alerts_ = []
    e = Engine(interval=60, now_fn=lambda: clock[0], odds_fn=lambda lg: (events(), {"x-requests-remaining": "480", "x-requests-used": "20"}),
               scores_fn=lambda lg: scores, news_fn=lambda rec: ("note", ""), alert_fn=lambda t, m, c: alerts_.append((t, m, c)),
               verify_fn=lambda link, price: (("seen", "Alpha FC 7/4") if link else ("no link", "")), persist=False)
    C.append(("a tested league is recognised", e.add_league("soccer_england_league1") and e.leagues[0]["scope"] == "tested" and not e.add_league("soccer_england_league1")))
    b = event_books(events()[0])
    C.append(("bookmaker prices are read and the exchange is left out", len(b) == 6 and b["SlowBook"] == (2.75, 3.30, 2.90) and "Betfair Exchange" not in b))
    e.cycle()
    C.append(("started and far-off matches are skipped", len(e.matches) == 1 and e.matches[0]["home"] == "Alpha FC"))
    C.append(("first sighting is only 'checking', nothing recorded", len(e.live) == 1 and e.live[0]["status"] == "checking" and e.record == []))
    C.append(("remaining data credits are tracked", e.quota["remaining"] == "480"))
    clock[0] += 60; e.cycle()
    C.append(("still there on the next read: recorded once, with the news note", e.live[0]["status"] == "confirmed" and len(e.record) == 1 and e.record[0]["news"] == "note"))
    clock[0] += 60; e.cycle()
    C.append(("the bookmaker's own page is checked when the price is recorded", e.record[0]["verify"] == "seen" and e.record[0]["link"].startswith("https://slowbook") and e.live[0]["verify"] == "seen"))
    C.append(("a price is recognised as a decimal or a fraction, not inside other numbers", price_on_page("Alpha FC 7/4 Draw 23/10", 2.75) and price_on_page("home 2.75", 2.75)
              and not price_on_page("total 12.75 goals 17/4", 2.75) and not price_on_page("", 2.75)))
    C.append(("a third read does not record it again", len(e.record) == 1))
    slow[0] = 2.31; clock[0] += 60; e.cycle(); slow[0] = 2.75; clock[0] += 60; e.cycle(); clock[0] += 60; e.cycle()
    C.append(("a price that goes back in line and comes out again is not counted twice", len(e.record) == 1 and e.live and e.live[0]["status"] == "confirmed"))
    slow[0] = 2.31; clock[0] += 60; e.cycle()
    C.append(("when the price comes back in line the flag disappears", e.live == [] and len(e.record) == 1))
    n = e.matches[0]["nearest"]
    C.append(("'closest to the line' reports a price just short of it", n and n["ratio"] < 1 and n["price"] < n["needed"]))
    slow[0] = 2.75; clock[0] += 60; e.cycle(); slow[0] = 2.31; clock[0] += 60; e.cycle()
    C.append(("a flag that vanishes before the second read is never recorded", len(e.record) == 1))
    scores.append({"id": "e1", "completed": True, "home_team": "Alpha FC", "away_team": "Beta United",
                   "scores": [{"name": "Alpha FC", "score": "2"}, {"name": "Beta United", "score": "0"}]})
    clock[0] += 5 * 3600; e.cycle()
    r = e.record[0]
    C.append(("after the match it is settled from the score feed with the right profit", r["result"] == "won" and abs(r["profit"] - 1.75) < 1e-9 and r["score"] == "2-0"))
    C.append(("settlement rules for all three results", settle_outcome("draw", 1, 1) and settle_outcome("away", 0, 2) and not settle_outcome("home", 0, 0)))
    st = e.state()
    slow[0] = 2.75
    e5 = Engine(interval=60, now_fn=lambda: clock[0], odds_fn=lambda lg: (events(), {}), scores_fn=lambda lg: [], news_fn=lambda rec: ("", ""),
                verify_fn=lambda link, price: ("seen", ""), persist=False)
    e5.add_league("soccer_england_league1"); e5.cycle(); slow[0] = 2.31; clock[0] += 60; e5.cycle()
    two = page.replace("| 10Bet | 2.28 |", "| 10Bet | 2.80 |") if "page" in dir() else None
    C.append(("every out-of-line price is kept in the day's log with how long it lasted", len(e.seen) == 1 and list(e.seen.values())[0]["reads"] >= 2
              and e.state()["seen"][0]["recorded"] is True))
    C.append(("a flag that vanished before confirmation is counted as seen-once, not as a bet", len(e5.missed) == 1 and e5.missed[0]["book"] == "SlowBook" and e5.record == []))
    pot_ = e.pot()
    C.append(("an alert goes out at the first sighting and again when it is confirmed", len(alerts_) >= 2 and alerts_[0][0].startswith("Out of line: Alpha FC")
              and "SlowBook" in alerts_[0][1] and alerts_[1][0].startswith("Confirmed")))
    sent_ = []
    os.environ["OFFSIDE_NTFY"] = "test-topic"
    n_ = notify("Türkiye v X", "msg", "https://x.example/slip", post=lambda url, payload: sent_.append((url, payload)))
    os.environ.pop("OFFSIDE_NTFY")
    C.append(("phone alerts carry a tap-through link and cope with accented names", n_ == 1 and sent_[0][0] == "https://ntfy.sh/" and sent_[0][1]["topic"] == "test-topic"
              and sent_[0][1]["click"] == "https://x.example/slip" and sent_[0][1]["title"] == "Türkiye v X"))
    texts_ = []
    for k_, v_ in (("TWILIO_SID", "AC1"), ("TWILIO_TOKEN", "tok"), ("TWILIO_FROM", "+15550001111"), ("OFFSIDE_SMS_TO", "+358401234567")): os.environ[k_] = v_
    n2_ = notify("Out of line: A v B", "A to win at Book: 2.30.", "https://x.example/slip", post=lambda u, pl: None, sms=lambda *a_: texts_.append(a_))
    for k_ in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM", "OFFSIDE_SMS_TO"): os.environ.pop(k_)
    C.append(("a text message carries the alert and the link to tap", n2_ == 1 and texts_[0][3] == "+358401234567" and texts_[0][4].endswith("https://x.example/slip")))
    os.environ["OFFSIDE_WEBHOOK"] = "https://discord.com/api/webhooks/1/abc"
    probs_, posted_ = [], []
    def bad_post(url, payload): raise urllib.error.HTTPError(url, 404, "Not Found", {}, __import__("io").BytesIO(b'{"message": "Unknown Webhook"}'))
    n3_ = notify("t", "m", "https://x", post=bad_post, problems=probs_)
    n4_ = notify("t", "m", "https://x", post=lambda u, pl: posted_.append(pl))
    os.environ.pop("OFFSIDE_WEBHOOK")
    C.append(("a failed alert says why, and a Discord message carries the link", n3_ == 0 and "404" in probs_[0] and "Unknown Webhook" in probs_[0]
              and n4_ == 1 and list(posted_[0]) == ["content"] and posted_[0]["content"].endswith("https://x")))
    C.append(("with no alert address set nothing is sent", notify("a", "b", post=lambda u, pl: sent_.append(1)) == 0))
    C.append(("'Check results now' queues finished matches for a lookup", isinstance(e.check_results(), int) and e.wake.is_set()))
    C.append(("the paper account stakes 2% of the pot and pays out at the recorded price", e.record[0]["stake"] == 20.0 and abs(pot_["now"] - (1000 + 20 * 1.75)) < 0.01 and pot_["open_bets"] == 0))
    C.append(("the dashboard data adds up and can be sent as JSON", st["summary"]["settled"] == 1 and st["summary"]["won"] == 1 and abs(st["summary"]["roi"] - 1.75) < 1e-9 and json.dumps(st)))
    page = ("# Gamma Town vs Delta City 1X2\n| bet365 | 2.30 | 3.40 | 3.00 |\n| William Hill | 2.25 | 3.30 | 3.10 |\n| Unibet | 2.35 | 3.45 | 2.95 |\n"
            "| Betfred | 2.30 | 3.40 | 3.00 |\n| 10Bet | 2.28 | 3.35 | 3.05 |\n| SlowBook | 2.75 | 3.30 | 2.90 |\n")
    e2 = Engine(interval=60, now_fn=lambda: clock[0], scrape_fn=lambda u: (page, None), result_fn=lambda rec: {"home_goals": 0, "away_goals": 1},
                news_fn=lambda rec: ("", ""), persist=False)
    reads = [0]
    def flaky_page(u):
        reads[0] += 1
        return (page, None) if reads[0] != 2 else ("Loading odds", None)
    e3 = Engine(interval=60, now_fn=lambda: clock[0], scrape_fn=flaky_page, news_fn=lambda rec: ("", ""), persist=False)
    e3.add_watch("https://odds.example/m9"); e3.cycle(); clock[0] += 60; e3.cycle()
    C.append(("a failed read keeps the last prices on show and does not cancel a flag awaiting confirmation",
              len(e3.matches) == 1 and e3.matches[0]["stale"] and e3.record == [] and len(e3.pending) == 1))
    clock[0] += 60; e3.cycle()
    C.append(("the flag is then confirmed on the next good read", len(e3.record) == 1))
    e2.add_watch("https://odds.example/m1"); e2.cycle(); clock[0] += 60; e2.cycle()
    page2 = page.replace("| 10Bet | 2.28 |", "| 10Bet | 2.80 |")
    e6 = Engine(interval=60, now_fn=lambda: clock[0], scrape_fn=lambda u: (page2, None), news_fn=lambda rec: ("", ""), persist=False)
    e6.add_watch("https://odds.example/m6"); e6.cycle(); clock[0] += 60; e6.cycle()
    e9 = Engine(interval=60, now_fn=lambda: clock[0], scrape_fn=lambda u: (page, None), news_fn=lambda rec: ("", ""), persist=False,
                confirm_fn=lambda book, home, away, price: ("not shown", "https://slowbook.example/m", ""))
    e9.add_watch("https://odds.example/m9b"); e9.cycle(); clock[0] += 60; e9.cycle()
    al9 = []
    e10 = Engine(interval=60, now_fn=lambda: clock[0], scrape_fn=lambda u: (page, None), news_fn=lambda rec: ("", ""), persist=False,
                 confirm_fn=lambda book, home, away, price: ("seen", "https://slowbook.example/event/123", "page"), alert_fn=lambda t, m, c: al9.append((t, m, c)))
    calls10 = []
    e10.confirm = (lambda inner: (lambda *a_: (calls10.append(1), inner(*a_))[1]))(e10.confirm)
    e10.add_watch("https://odds.example/m10"); e10.cycle(); clock[0] += 60; e10.cycle()
    s10 = e10.state()["seen"][0]
    C.append(("the earlier log carries what the bookmaker's own site showed", s10.get("verify") == "seen" and s10.get("book_link", "").endswith("/event/123") and s10["recorded"]))
    C.append(("a live row carries the bookmaker's own page once it is found", e10.state()["live"][0]["book_link"] == "https://slowbook.example/event/123"))
    C.append(("the first alert is followed by one linking to the bookmaker's own page, and the check is not repeated",
              len(al9) == 3 and al9[0][0].startswith("Out of line") and al9[1][0].startswith("Bet at") and al9[1][2] == "https://slowbook.example/event/123" and "same price" in al9[1][1]
              and al9[2][0].startswith("Confirmed") and al9[2][2] == "https://slowbook.example/event/123" and len(calls10) == 1
              and e10.record[0]["link"] == "https://slowbook.example/event/123"))
    e11 = Engine(interval=60, now_fn=lambda: clock[0], scrape_fn=lambda u: (page, None), news_fn=lambda rec: ("", ""), persist=False,
                 alert_fn=lambda t, m, c: al9.append("x"))
    e11.alert_min = 5; n_before = len(al9); e11.add_watch("https://odds.example/m11"); e11.cycle()
    C.append(("alerts can be limited to the stricter settings", len(al9) == n_before or e11.live[0]["points"] >= 5))
    def src_http(found_price):
        def h(url, body, timeout=75):
            if url == CTX_SEARCH:
                return {"results": [{"url": "https://www.starsports.bet/promotions", "title": "Offers"},
                                    {"url": "https://www.starsports.bet/event/romania-v-poland-123", "title": "Romania v Poland"}]} if "includeDomains" in body else {"results": []}
            if "promotions" in body["url"]:
                return {"markdown": {"success": True, "data": "Free bets and offers"}, "json": {"success": True, "data": {"is_this_match": False}}}
            return {"markdown": {"success": True, "data": "Romania v Poland. Match result."},
                    "json": {"success": True, "data": {"is_this_match": True, "home_win": 4.75, "draw": 3.9, "away_win": found_price}}}
        return h
    s1 = ctx_confirm_at_source("k", "Star Sports", "Romania", "Poland", 1.73, "away", http=src_http(1.73))
    s2 = ctx_confirm_at_source("k", "Star Sports", "Romania", "Poland", 1.73, "away", http=src_http(1.67))
    s3 = ctx_confirm_at_source("k", "Star Sports", "Romania", "Poland", 1.73, "away", http=src_http(None))
    C.append(("the bookmaker's own match page is found among search results and its price read",
              s1[0] == "seen" and s1[1].endswith("romania-v-poland-123") and s2[0] == "moved" and s2[3] == 1.67 and s3[0] == "not shown" and s3[1].endswith("123")))
    def art_http(url, body, timeout=75):
        if url == CTX_SEARCH: return {"results": [{"url": "https://news.bet365.com/en-gb/article/efl-cup-draw/123", "title": "Norwich v Cardiff City and more"}]}
        return {"markdown": {"success": True, "data": "Norwich v Cardiff City in the cup draw"}, "json": {"success": True, "data": {"is_this_match": False}}}
    def coupon_http(found):
        def h(url, body, timeout=75):
            if url == CTX_SEARCH: return {"results": [{"url": "https://www.coral.co.uk/en/sports/coupons/football/today-s-matches/334", "title": "Today's matches"}]}
            return {"markdown": {"success": True, "data": "Today. Romania v Poland 4/1 3/1 8/13. Sweden v Bosnia 4/9 7/2 7/10."},
                    "json": {"success": True, "data": {"is_this_match": True, "home_win": 5.0, "draw": 4.0, "away_win": found}}}
        return h
    c1 = ctx_confirm_at_source("k", "Coral", "Romania", "Poland", 1.70, "away", http=coupon_http(1.70))
    c2 = ctx_confirm_at_source("k", "Coral", "Romania", "Poland", 1.70, "away", http=coupon_http(None))
    C.append(("on a page listing many matches, only the price read for this match counts as the same price",
              c1[0] == "seen" and c1[3] == 1.70 and c2[0] == "not shown" and c2[1].endswith("/334")))
    C.append(("a news article naming both teams is not taken for the bookmaker's match page",
              ctx_confirm_at_source("k", "bet365", "Norwich", "Cardiff City", 1.73, "home", http=art_http)[0] == "unreadable"))
    C.append(("an unknown bookmaker, or no page for the match, is reported as such",
              ctx_confirm_at_source("k", "Nobody Bets", "A", "B", 2.0)[0] == "no site"
              and ctx_confirm_at_source("k", "Star Sports", "Romania", "Poland", 1.73, "away", http=lambda u, b, timeout=75: {"results": []} if u == CTX_SEARCH else {"json_content": {}})[0] == "unreadable"))
    C.append(("a recorded price carries the result of the check on the bookmaker's own site", e9.record[0]["verify"] == "not shown" and e9.record[0]["link"] == "https://slowbook.example/m"))
    C.append(("two bookmakers out of line on the same result count as one bet, at the better price",
              len(e6.live) == 2 and len(e6.record) == 1 and e6.record[0]["price"] == 2.80))
    kick7 = clock[0] + 100
    e7 = Engine(interval=60, now_fn=lambda: clock[0], scrape_fn=lambda u: (page, None), news_fn=lambda rec: ("", ""), persist=False, in_play=True)
    e7.add_watch("https://odds.example/m7", kickoff_ts=kick7); e7.cycle(); clock[0] += 130; e7.cycle(); clock[0] += 130; e7.cycle()
    C.append(("with in-play reading on, a match is still read after kick-off and its entries are tagged", e7.matches and e7.matches[0]["in_play"]
              and len(e7.matches[0]["hist"]) >= 2 and e7.record and e7.record[-1]["in_play"] in (True, False)))
    e8 = Engine(interval=60, now_fn=lambda: clock[0], scrape_fn=lambda u: (page, None), news_fn=lambda rec: ("", ""), persist=False)
    e8.add_watch("https://odds.example/m8", kickoff_ts=clock[0] - 5); e8.cycle()
    C.append(("without it, a match that has kicked off is not read", e8.matches == []))
    e2.read_now("page:https://odds.example/m1")
    rown = [r_ for r_ in e2.matches if r_["id"] == "page:https://odds.example/m1"]
    C.append(("'Read now' reads at once, updates the row and reports what happened",
              e2.read_note["page:https://odds.example/m1"]["state"] == "done" and e2.read_note["page:https://odds.example/m1"]["books"] >= 5
              and "https://odds.example/m1" in e2.prefetch and (not rown or rown[0]["stale"] is None)))
    C.append(("every read is logged for the live feed, and 'Read now' makes a match due", e2.feed and e2.feed[-1]["books"] >= 5 and e2.read_now("page:https://odds.example/m1")
              and e2.read_at["https://odds.example/m1"] < 0 and e2.wake.is_set() and not e2.read_now("page:nope")))
    C.append(("a pasted odds page is read, named from its heading and recorded", len(e2.record) == 1 and e2.record[0]["home"] == "Gamma Town" and e2.record[0]["url"] and e2.record[0]["verify"] == "seen"))
    clock[0] += 5 * 3600; e2.cycle()
    C.append(("a page-sourced entry is settled by lookup", e2.record[0]["result"] == "lost" and e2.record[0]["profit"] == -1.0))
    good = "| Bet365 | 2.30 | 3.40 | 3.00 |\n| Unibet | 2.25 | 3.30 | 3.10 |\n| Coral | 2.35 | 3.45 | 2.95 |\n| Betfred | 2.30 | 3.40 | 3.00 |\n| Betway | 2.28 | 3.35 | 3.05 |\n"
    def fake_ask(task, shape):
        if "matches" in shape:
            return {"matches": [{"home": "Alpha FC", "away": "Beta United", "kickoff_utc": iso(clock[0] + 7200)},
                                {"home": "Gamma Town", "away": "Delta City", "kickoff_utc": iso(clock[0] + 9000)},
                                {"home": "Old Match", "away": "Long Ago", "kickoff_utc": iso(clock[0] - 9000)},
                                {"home": "Epsilon", "away": "Zeta", "kickoff_utc": iso(clock[0] + 9500)}]}
        return {"url": "https://odds.example/alpha" if "Alpha FC" in task else ("https://odds.example/thin" if "Gamma" in task else "")}
    five = {"Bet365": (2.3, 3.4, 3.0), "Unibet": (2.25, 3.3, 3.1), "Coral": (2.35, 3.45, 2.95), "Betfred": (2.3, 3.4, 3.0), "Betway": (2.28, 3.35, 3.05)}
    def fake_search(q): return [] if "Epsilon" in q else ["https://bad.example/x", "https://odds.example/" + ("alpha" if "Alpha" in q else "thin")]
    def fake_read(u): return (five, "page", "") if "alpha" in u else (({"Bet365": (2.3, 3.4, 3.0)}, "page", "") if "thin" in u else ({}, None, "blocked"))
    rows = discover("k", ["Test League"], ask=fake_ask, search=fake_search, read=fake_read, now=clock[0], log=lambda *a: None)
    st_ = {r["home"]: r["status"] for r in rows}
    C.append(("discovery tries each search result and keeps the first odds page it can really read", st_["Alpha FC"].startswith("watching (5 bookmakers")
              and rows[0]["url"] == "https://odds.example/alpha" and "best had 1" in st_["Gamma Town"]
              and "already started" in st_["Old Match"] and st_["Epsilon"] == "no search results" and rows[0]["kickoff"] == clock[0] + 7200))
    lines = []
    u_, n_, best_ = find_odds_page("Alpha FC", "Beta United", fake_search, fake_read, log=lines.append)
    C.append(("the page finder reads candidates side by side and reports each one", u_ == "https://odds.example/alpha" and n_ == 5 and len(lines) == 2
              and any("page not readable" in l for l in lines)))
    ra = lambda task, shape: {"bookmakers": [{"name": n, "home": h, "draw": d, "away": a, "source_url": "https://site%d.example/m" % (i % 2)}
                                             for i, (n, (h, d, a)) in enumerate(five.items())] + [{"name": "Guess", "home": 2.3, "draw": None, "away": 3.0, "source_url": ""}]}
    rb, rt, rw, rl, rs = ctx_research_odds("k", "Alpha FC", "Beta United", ask=ra)
    C.append(("web research returns bookmakers with the page each was read from", len(rb) == 5 and rl["Bet365"].startswith("https://site") and len(rs) == 2 and "Guess" not in rb and rw == ""))
    er = Engine(interval=60, now_fn=lambda: clock[0], news_fn=lambda rec: ("", ""), verify_fn=lambda link, price: ("seen" if link else "no link", ""), persist=False)
    slowbook = dict(five, SlowBook=(2.75, 3.3, 2.9))
    er.research = lambda home, away: (slowbook, "researched text", "", {"SlowBook": "https://slowbook.example/m"}, ["a.example", "b.example"])
    er.add_research("Alpha FC", "Beta United", scope="tested"); er.cycle(); clock[0] += 60; er.cycle()
    C.append(("a researched match is flagged, then checked on the page the price came from", len(er.record) == 1 and er.record[0]["verify"] == "seen"
              and er.record[0]["link"] == "https://slowbook.example/m" and er.matches[0]["league"] == "Web research, 2 sites"))
    def td(bk, txt, extra=""): return '<td class="bc" data-bk="%s"%s><p>%s</p></td>' % (bk, extra, txt)
    head = "<tr>" + "<td></td>" + "".join('<td data-bk="%s"><a title="%s"><img alt="logo"></a></td>' % (c, n) for c, n in
                                           (("B3", "bet365"), ("SK", "Sky Bet"), ("PP", "Paddy Power"), ("WH", "William Hill"), ("LD", "Ladbrokes"), ("ZZ", "NewBook"))) + "</tr>"
    def row(label, prices): return '<tr data-bname="%s"><td><a>%s</a></td>%s</tr>' % (label, label, "".join(td(c, x) for c, x in zip(("B3", "SK", "PP", "WH", "LD", "ZZ"), prices)))
    page_h = "<table>" + head + row("Wigan", ["11/10", "11/10", "23/20", "11/10", "23/20", ""]) + row("Draw", ["2", "2", "9/4", "2", "2", "2"]) \
             + row("Barnsley", ["27/10", "27/10", "13/5", "27/10", "14/5", "3"]) + row("Over 2.5", ["10/11"] * 6) + "</table>"
    hgb = html_grid(page_h)
    C.append(("the odds table is read exactly from the page HTML, by bookmaker code", len(hgb) == 5 and hgb["bet365"] == (2.1, 3.0, 3.7)
              and hgb["Paddy Power"] == (2.15, 3.25, 3.6) and "NewBook" not in hgb))
    C.append(("a whole number among fractions means that number to one", _price("2") == 3.0 and _price("11/10") == 2.1 and _price("2.10") == 2.1 and _price("x") is None))
    C.append(("a misread draw price is rejected", valid_books([{"name": "Bet365", "home": 2.1, "draw": 2, "away": 3.7}]) == {}
              and len(valid_books([{"name": "Bet365", "home": 2.1, "draw": 3.0, "away": 3.7}])) == 1))
    html_post = lambda body: {"request_id": "r", "html": {"success": True, "data": page_h}, "markdown": {"success": True, "data": "x"},
                              "json": {"success": True, "data": {"bookmakers": [{"name": "Wrong", "home": 9, "draw": 9, "away": 9}]}}, "parsed": {"success": True, "data": {"rows": []}}}
    bk5, _, _ = ctx_read_odds("k", "https://odds.example/h", post=html_post)
    C.append(("the exact HTML reading is trusted ahead of the model's extraction", len(bk5) == 5 and bk5["Sky Bet"] == (2.1, 3.0, 3.7)))
    bcodes = ("B3", "WH", "UN", "FR", "LD", "ZZ", "MA")
    bnames = ("bet365", "William Hill", "Unibet", "Betfred", "Ladbrokes", "NewBook", "Matchbook")
    offers = "".join('<div data-bk="%s" class="OfferDesktopContainer"><img alt="present icon"></div>' % c for c in bcodes)
    side = '<div class="names"><span>Reading</span><span>Draw</span><span>Stockport</span></div>'
    logos_ = "".join('<a><img alt="%s" class="bookmakerImg_b1 lazyloaded"></a>' % n for n in bnames)
    coded = "".join('<a><img alt="%s" class="bookmakerImg_b1" data-src="https://static.x.example/content/logos/OC/%s/2/%s-main.svg"></a>' % (n, c.lower(), c.lower())
                    for c, n in (("ZZ", "NewBook"), ("WH", "Hills"), ("IVB", "Ivy Bet")))
    def brow(prices):
        return '<div class="oddsAreaWrapper"><div><h4>Sportsbooks</h4><h4>Odds</h4></div>%s</div>' % "".join(
            '<button type="button" class="OddsCellDesktop" data-bk="%s" aria-label="Add to betslip">%s</button>' % (c, x) for c, x in zip(bcodes, prices))
    bpage = ("<h1>Stockport vs Reading Betting Odds</h1><table><tr><td>unsupported browser</td></tr></table>" + offers + side + logos_
             + brow(["6/4", "6/4", "13/8", "6/4", "8/5", "6/4", "7/4"]) + brow(["12/5", "23/10", "12/5", "5/2", "12/5", "", "5/2"])
             + brow(["7/4", "17/10", "7/4", "9/5", "7/4", "2", "15/8"]))
    hinfo = {}
    hb = html_buttons(bpage, "stockport", "reading", info=hinfo)
    C.append(("price buttons are read by bookmaker code, named from the logo strip, and matched to home, draw and away",
              len(hb) == 5 and hb["bet365"] == (2.75, 3.4, 2.5) and hb["William Hill"] == (2.7, 3.3, 2.5) and "NewBook" not in hb
              and "Matchbook" not in hb and "present icon" not in hb and "away, draw, home" in hinfo["rows"]))
    i2 = {}
    C.append(("without result names the page is not used, because the row order cannot be assumed",
              html_buttons(bpage.replace(side, ""), "stockport", "reading", info=i2) == {} and "could not tell" in i2["rows"]))
    hb2 = html_buttons(bpage.replace(side, ""), "stockport", "reading", rows_hint=["Stockport", "Reading", "Draw"])
    C.append(("row names supplied separately fix the order (home, away, draw as on the real site)", hb2["bet365"] == (2.5, 2.75, 3.4)))
    C.append(("the page's own row names win over the model's", html_buttons(bpage, "stockport", "reading", rows_hint=["Stockport", "Draw", "Reading"]) == hb))
    side2 = '<div class="names"><span>Stockport</span><span>Reading</span><span>Draw</span></div>'
    hb4 = html_buttons(bpage.replace(side, side2), "stockport", "reading")
    C.append(("home, away, draw order is read correctly", hb4["bet365"] == (2.5, 2.75, 3.4)))
    hb3 = html_buttons(bpage.replace(logos_, coded), "stockport", "reading")
    C.append(("the bookmaker's code decides its name, whatever the logo beside it says", "William Hill" in hb3 and "Hills" not in hb3 and "bet365" in hb3 and len(hb3) == 5))
    C.append(("team names are taken from the page address", teams_in_address("https://x.example/football/english/league-1/stockport-v-reading/winner") == ("stockport", "reading")))
    btn_post = lambda body: {"request_id": "r", "html": {"success": True, "data": bpage}, "markdown": {"success": True, "data": "x"},
                             "json": {"success": True, "data": {"bookmakers": []}}, "parsed": {"success": True, "data": {"rows": []}}}
    bk6, _, _ = ctx_read_odds("k", "https://odds.example/football/english/league-1/stockport-v-reading/winner", post=btn_post)
    C.append(("a page of price buttons is read even when the model's extraction returns nothing", len(bk6) == 5 and bk6["Ladbrokes"] == (2.75, 3.4, 2.6)))
    pr_ = {"codes": list(bcodes) * 3, "prices": ["6/4", "6/4", "13/8", "6/4", "8/5", "6/4", "7/4"] + ["12/5", "23/10", "12/5", "5/2", "12/5", "", "5/2"] + ["7/4", "17/10", "7/4", "9/5", "7/4", "2", "15/8"],
           "logo_names": list(bnames), "logo_srcs": ["https://static.x/content/logos/OC/%s/2/%s-main.svg" % (c.lower(), c.lower()) for c in bcodes],
           "row_names": ["Reading", "Draw", "Stockport"]}
    rb_ = rules_books(pr_, "stockport", "reading")
    C.append(("Context.dev's parse rules alone give the same prices as reading the HTML", rb_ == hb and rb_["bet365"] == (2.75, 3.4, 2.5)))
    C.append(("without recognisable row names the parse-rule result is not used", rules_books(dict(pr_, row_names=["A", "B", "C"]), "stockport", "reading") == {}))
    rule_post = lambda body: {"request_id": "abcd1234ef", "cache_metadata": {"status": "miss", "age_ms": 0}, "parsed": {"success": True, "data": pr_}, "html": {"success": True, "data": ""}}
    bkr, note_, _ = ctx_read_odds("k", "https://odds.example/football/english/league-1/stockport-v-reading/winner", post=rule_post, light=True)
    meta_ = READ_META["https://odds.example/football/english/league-1/stockport-v-reading/winner"]
    C.append(("a repeat read uses the parse rules and notes the request id and that it was fresh", len(bkr) == 5 and "parse rules" in note_ and meta_["fresh"] and meta_["request_id"] == "abcd1234ef"))
    C.append(("fractions are read as prices", fractions_to_decimals("Bet365 6/4 11/5 13/8 evens 14/11/2026") == "Bet365 2.50 3.20 2.62 2.00 14/11/2026"))
    fake_post = lambda body: {"markdown": {"success": True, "data": "# Alpha FC v Beta United"}, "json": {"success": True, "data": {"bookmakers": [
        {"name": "Bet365", "home": 2.3, "draw": 3.4, "away": 3.0}, {"name": "Unibet", "home": 2.25, "draw": 3.3, "away": 3.1},
        {"name": "Betfair Exchange", "home": 2.4, "draw": 3.6, "away": 3.2}, {"name": "Broken", "home": 2.3, "draw": None, "away": 3.0},
        {"name": "Nonsense", "home": 1.2, "draw": 1.3, "away": 1.4}, {"name": "Coral", "home": "2.35", "draw": 3.45, "away": 2.95}]}}}
    bk, md_, _ = ctx_read_odds("k", "https://odds.example/alpha", post=fake_post)
    C.append(("structured extraction keeps real bookmaker rows only", sorted(bk) == ["Bet365", "Coral", "Unibet"] and bk["Coral"] == (2.35, 3.45, 2.95) and md_.startswith("#")))
    many = {"Match %d" % i: (1.3 + 0.35 * i, 3.4, 9.0 - 0.6 * i) for i in range(12)}
    C.append(("a list of different matches is not mistaken for one match's bookmakers", coherent(many) == {} and len(coherent(five)) == 5))
    one_off = dict(five, Typo=(9.9, 3.4, 3.0))
    C.append(("one stray row is dropped, the real bookmakers are kept", sorted(coherent(one_off)) == sorted(five)))
    codes = ["B3", "SK", "PP", "WH", "XX"]
    grid = {"rows": [{"name": "Crewe", "bks": codes, "odds": ["2.3", "2.25", "2.35", "2.3", "2.28"]},
                     {"name": "Draw", "bks": codes, "odds": ["3.4", "3.3", "3.45", "3.4", "3.35"]},
                     {"name": "Newport", "bks": codes, "odds": ["3.0", "3.1", "2.95", "3.0", "3.05"]},
                     {"name": "Over 2.5", "bks": ["B3"], "odds": ["1.9"]}]}
    gb = grid_books(grid)
    C.append(("a table with bookmakers in columns is read down each column", len(gb) == 5 and gb["bet365"] == (2.3, 3.4, 3.0) and gb["XX"] == (2.28, 3.35, 3.05)))
    grid_post = lambda body: {"markdown": {"success": True, "data": "\n".join("| Row %d | 6/4 | 13/8 | 7/4 |" % i for i in range(88))},
                              "json": {"success": False, "data": None}, "parsed": {"success": True, "data": grid}}
    bk3, _, _ = ctx_read_odds("k", "https://grid.example/m", post=grid_post)
    C.append(("88 misread text rows are rejected and the real table is used instead", sorted(bk3) == sorted(gb)))
    table = ("| | [![bet365](a.png)](https://x/b3) | [![Sky Bet](b.png)](https://x/sk) | Paddy Power | William Hill | Coral | Betfred |\n|---|---|---|---|---|---|---|\n"
             "| Crewe | 13/10 | 5/4 | 27/20 | 13/10 | 6/5 | |\n| Draw | 12/5 | 23/10 | 12/5 | 5/2 | 12/5 | 12/5 |\n| Newport County | 2/1 | 21/10 | 19/10 | 2/1 | 2/1 | 2/1 |\n"
             "| Over 2.5 | 10/11 | 9/10 | 1/1 | 10/11 | 5/6 | 5/6 |")
    gt = grid_from_text(table)
    C.append(("bookmakers-in-columns text is read down each column, with names from the header", len(gt) == 5 and gt["bet365"] == (2.3, 3.4, 3.0)
              and gt["Sky Bet"] == (2.25, 3.3, 3.1) and "Betfred" not in gt))
    loose = "Crewe 2.30 2.25 2.35 2.30 2.20\nNewport 3.00 3.10 2.90 3.00 3.00\nDraw 3.40 3.30 3.40 3.50 3.40\n"
    gl = grid_from_text(loose)
    C.append(("the draw row is recognised wherever it sits", len(gl) == 5 and gl["Bookmaker 1"] == (2.3, 3.4, 3.0)))
    hub = "https://www.oddschecker.com/football/english/league-2"
    mp = match_pages(hub, [hub + "/crewe-v-newport-county/winner", hub + "/crewe-v-newport-county/both-teams-to-score", "https://www.oddschecker.com/tennis",
                           "https://other.example/a-v-b", hub + "/bristol-rovers-v-chesterfield"])
    C.append(("match pages are picked out of a league page's links, once each", mp == [(hub + "/crewe-v-newport-county/winner", "Crewe", "Newport County"),
              (hub + "/bristol-rovers-v-chesterfield/winner", "Bristol Rovers", "Chesterfield")]))
    link_post = lambda body: {"parsed": {"success": True, "data": {"links": ["/football/english/league-2/a-v-b/winner", "#top"]}},
                              "markdown": {"success": True, "data": "[x](https://www.oddschecker.com/football/english/league-2/c-v-d/winner)"}}
    lk, _ = ctx_links("k", hub, post=link_post)
    lk2, _ = ctx_links("k", hub, post=lambda body: {"parsed": {"success": True, "data": {"links": ["football/english/league-1/a-v-b/winner"]}}, "markdown": {"success": True, "data": ""}})
    C.append(("a link that repeats the site's first folder is not doubled up", lk2 == ["https://www.oddschecker.com/football/english/league-1/a-v-b/winner"]))
    C.append(("only football matches from the league's own page are followed", match_pages(hub, ["https://www.oddschecker.com/boxing/fights/fury-v-joshua/winner",
              "https://www.oddschecker.com/football/english/league-1/a-v-b/winner", hub + "/c-v-d/winner"]) == [(hub + "/c-v-d/winner", "C", "D")]
              and len(match_pages("https://www.oddschecker.com/football", ["https://www.oddschecker.com/football/english/league-1/a-v-b/winner",
                                                                           "https://www.oddschecker.com/boxing/fights/fury-v-joshua/winner"])) == 1))
    none_post = lambda body: {"request_id": "r", "metadata": {"title": "Stockport v Reading Betting Odds"}, "markdown": {"success": True, "data": "Match postponed. " * 50},
                              "json": {"success": True, "data": {"bookmakers": []}}, "parsed": {"success": True, "data": {"rows": []}}}
    b0, _, w0 = ctx_read_odds("k", "https://odds.example/pp", post=none_post)
    C.append(("a page with no prices is reported as such, with its title", b0 == {} and "shows no prices" in w0 and "Stockport v Reading" in w0))
    C.append(("league pages are picked out of the football page's links", league_pages("https://www.oddschecker.com/football", [
              "https://www.oddschecker.com/football/english/league-1", "https://www.oddschecker.com/football/english/league-1/a-v-b/winner",
              "https://www.oddschecker.com/football/english", "https://www.oddschecker.com/tennis/atp/x"]) == ["https://www.oddschecker.com/football/english/league-1"]))
    light_post = lambda body: {"html": {"success": True, "data": bpage}, "parsed": {"success": False, "data": None}, "key_metadata": {"credits_remaining": 41234}} if set(body["formats"]) == {"parse", "html", "markdown"} else None
    bkl, _, _ = ctx_read_odds("k", "https://odds.example/football/english/league-1/stockport-v-reading/winner", post=light_post, light=True)
    C.append(("if the parse rules return nothing the returned HTML is read instead, and credits left are noted", len(bkl) == 5 and CREDITS.get("left") == 41234))
    C.append(("the tested leagues are recognised from the site's real addresses", all(TESTED_PATHS.search(u) for u in (
              "/football/germany/bundesliga/bayern-munich-v-cologne/winner", "/football/spain/la-liga-primera/athletic-bilbao-v-espanyol/winner",
              "/football/italy/serie-a/como-v-cagliari/winner", "/football/english/championship/millwall-v-qpr/winner", "/football/english/league-2/a-v-b/winner"))
              and not any(TESTED_PATHS.search(u) for u in ("/football/uefa-nations-league/romania-v-poland/winner", "/football/english/fa-cup/a-v-b/winner"))))
    shifted = dict(pr_, logo_names=["bet365"] + list(bnames[:-1]))      # logo list out of step with the codes
    C.append(("a bookmaker is always named from its own code, even if the page's logo list is out of step",
              rules_books(shifted, "stockport", "reading")["William Hill"] == rb_["William Hill"] and "bet365" in rules_books(shifted, "stockport", "reading")))
    C.append(("tips and prediction articles are not treated as matches", match_pages("https://www.oddschecker.com/football", ["https://www.oddschecker.com/tips/football/x/abc-real-vs-barca-predictions-odds",
              "https://www.oddschecker.com/football/spanish/la-liga/real-v-barca/winner"]) == [("https://www.oddschecker.com/football/spanish/la-liga/real-v-barca/winner", "Real", "Barca")]
              and TESTED_PATHS.search("/football/english/league-2/a-v-b") and not TESTED_PATHS.search("/football/world/friendlies/a-v-b")))
    seq = []
    def flaky(body):
        seq.append(body["sharedParams"]["waitFor"])
        ok = len(seq) >= 2
        return {"request_id": "r%d" % len(seq), "markdown": {"success": True, "data": table if ok else "Loading odds"},
                "json": {"success": True, "data": {"bookmakers": []}}, "parsed": {"success": True, "data": {"rows": []}}}
    bk4, _, _ = ctx_read_odds("k", "https://odds.example/flaky", post=flaky)
    C.append(("a page whose table had not loaded is read again with a longer wait", len(bk4) == 5 and seq == [3000, 9000]))
    far = Engine(interval=300, now_fn=lambda: clock[0], persist=False)
    far.pending[("page:https://x/m", "Book", "home")] = {"first_seen": clock[0]}
    C.append(("a page with a flag waiting to be confirmed is read again within a minute", far.cadence(clock[0] + 6 * 3600, "https://x/m") == 60
              and far.cadence(clock[0] + 6 * 3600, "https://x/other") == 1200))
    far.pending.clear()
    far.failed.add("https://x/bad")
    C.append(("a page whose last read failed is tried again within a minute", far.cadence(clock[0] + 6 * 3600, "https://x/bad") == 60))
    far.failed.clear()
    C.append(("matches are read more often as kick-off approaches", far.cadence(clock[0] + 3 * 86400) == 3600 and far.cadence(clock[0] + 6 * 3600) == 1200
              and far.cadence(clock[0] + 3600) == 300 and far.cadence(None) == 300))
    C.append(("links are collected from the page and made absolute", lk == ["https://www.oddschecker.com/football/english/league-2/a-v-b/winner", "https://www.oddschecker.com/football/english/league-2/c-v-d/winner"]))
    C.append(("UK clock times convert correctly in winter and summer", uk_time_to_epoch("2026-11-14 15:00") == dt.datetime(2026, 11, 14, 15, tzinfo=dt.timezone.utc).timestamp()
              and uk_time_to_epoch("2026-07-01 15:00") == dt.datetime(2026, 7, 1, 14, tzinfo=dt.timezone.utc).timestamp() and uk_time_to_epoch("soon") is None))
    frac_post = lambda body: {"markdown": {"success": True, "data": "| Bet365 | 13/10 | 12/5 | 2/1 |\n| Unibet | 5/4 | 23/10 | 21/10 |\n| Coral | 27/20 | 12/5 | 19/10 |"}, "json": {"success": False, "data": None}}
    bk2, _, _ = ctx_read_odds("k", "https://odds.example/frac", post=frac_post)
    C.append(("a fractional-odds page is still read when extraction returns nothing", len(bk2) == 3 and bk2["Bet365"] == (2.3, 3.4, 3.0)))
    C.append(("kick-off times are understood in several formats", parse_kickoff("2026-11-15 15:00") and parse_kickoff("15.11.2026 15:00") and parse_kickoff("soon") is None
              and iso_utc("2026-11-15T15:00:00Z") == dt.datetime(2026, 11, 15, 15, tzinfo=dt.timezone.utc).timestamp()))
    ms, rng = [], random.Random(2)
    for k in range(600):
        won = rng.random() < 0.5
        ms.append({"season": "2021" if k < 300 else "2122", "league": "E0", "date": "20%02d-%02d-%02d" % (20 + (k >= 300), 1 + k % 12, 1 + k % 28),
                   "y": 0 if won else 2, "avg": [2.0, 3.6, 3.8], "max": [2.3, 3.7, 3.9], "named": [2.3, 3.7, 3.9], "pin": None, "b365": None, "n_books": 5})
    ev = build_evidence(ms); s4 = ev["settings"]["4"]
    C.append(("chart data: running totals end at the right profit", abs(s4["series"][-1][1] - s4["roi"] * s4["bets"]) < 0.05 and s4["series"][-1][3] == s4["bets"]))
    C.append(("the season table and the pot figures add up", sum(x["bets"] for x in s4["seasons"]) == s4["bets"] and abs(sum(x["units"] for x in s4["seasons"]) - s4["units"]) < 0.3
              and s4["pot"][0]["stake"] == 20 and s4["pot"][1]["total"] == round(s4["roi"] * s4["bets"] * 50) and s4["n_seasons"] == 2))
    C.append(("chart data: the outlier line beats the average-price line", s4["series"][-1][1] > s4["series"][-1][2] and s4["older_bets"] + s4["newer_bets"] == s4["bets"] and ev["split_month"]))
    C.append(("the page template is complete", PAGE.count("<script>") == 1 and "/api/state" in PAGE))
    ok = True
    for name, passed in C:
        print("  %s  %s" % ("PASS" if passed else "FAIL", name)); ok = ok and bool(passed)
    print("\nSELF-TEST %s  (version %s, %d checks)" % ("PASSED" if ok else "FAILED", VERSION, len(C)))
    return 0 if ok else 1


# --------------------------------------------------------------------------- command line
def main():
    ap = argparse.ArgumentParser(description="Offside")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest"); sub.add_parser("evidence"); sub.add_parser("leagues")
    p = sub.add_parser("alert-test"); p.add_argument("--ntfy", default="", help="ntfy.sh topic to send the test to")
    p.add_argument("--sample", action="store_true", help="send what a real pair of alerts looks like")
    p = sub.add_parser("betlinks", help="show what an odds page holds that could link straight to a bookmaker's bet"); p.add_argument("url")
    p = sub.add_parser("source", help="look for a bookmaker's own page for a match and read its price")
    p.add_argument("book"); p.add_argument("home"); p.add_argument("away"); p.add_argument("price", type=float)
    p.add_argument("outcome", choices=("home", "draw", "away"))
    p = sub.add_parser("discover"); p.add_argument("leagues", nargs="*", help='league names in plain words, in quotes')
    p.add_argument("--soon", action="store_true", help="any professional match worldwide in the next day")
    p.add_argument("--tested", action="store_true", help="these are among the 18 tested leagues")
    p.add_argument("--days", type=int, default=3); p.add_argument("--max", type=int, default=25, help="most matches to watch")
    p = sub.add_parser("matches"); p.add_argument("fixtures", nargs="+", help='each as "Home v Away", in quotes')
    p.add_argument("--kickoff", default="", help='local kick-off time for all of them, e.g. "2026-11-14 17:00"')
    p.add_argument("--tested", action="store_true", help="these are in the 18 tested leagues")
    p = sub.add_parser("research"); p.add_argument("fixtures", nargs="+", help='each as "Home v Away"')
    p.add_argument("--kickoff", default=""); p.add_argument("--tested", action="store_true")
    p.add_argument("--watch", action="store_true", help="also add the matches to the board")
    p = sub.add_parser("page"); p.add_argument("url", help="show exactly what Context.dev returns for this page")
    p = sub.add_parser("hub"); p.add_argument("url", nargs="*", help="odds pages that link to matches (default: the site's football page)")
    p.add_argument("--kickoff", default="", help='local kick-off time to use when a match page does not show one')
    p.add_argument("--tested", action="store_true"); p.add_argument("--max", type=int, default=40)
    p.add_argument("--workers", type=int, default=4, help="pages read at the same time")
    p.add_argument("--deep", action="store_true", help="also follow every league page linked from the page")
    p.add_argument("--within-hours", type=int, default=30, help="with --deep, only watch matches kicking off this soon")
    p = sub.add_parser("league"); p.add_argument("keys", nargs="+")
    p = sub.add_parser("add"); p.add_argument("url"); p.add_argument("--home", default=""); p.add_argument("--away", default="")
    p.add_argument("--kickoff", default="", help='local time, for example "2026-11-15 15:00"'); p.add_argument("--tested", action="store_true")
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8000)
    p.add_argument("--minutes", type=float, default=5.0, help="minutes between reads of a match in its last 3 hours")
    p.add_argument("--rediscover-hours", type=float, default=3.0, help="how often to look for new matches (0 = never)")
    p.add_argument("--in-play", action="store_true", help="keep reading prices after kick-off (not covered by the test)")
    p.add_argument("--ntfy", default="", help="send phone alerts to this ntfy.sh topic")
    p.add_argument("--alert-min", type=int, default=3, choices=(3, 4, 5), help="only alert on prices that pass at least this setting (3 = every flag)")
    p.add_argument("--parallel", type=int, default=6, help="how many pages to read side by side (Context.dev allows up to 50 at once)")
    p.add_argument("--pot", type=float, default=1000.0, help="starting size of the paper account")
    p.add_argument("--stake-pct", type=float, default=2.0, help="paper stake per bet, as a percentage of the pot")
    p.add_argument("--regions", default="uk,eu", help="bookmaker regions; each region costs one data credit per league per read")
    a = ap.parse_args()
    if a.cmd == "selftest":
        sys.exit(selftest())
    if a.cmd == "alert-test":
        if a.ntfy: os.environ["OFFSIDE_NTFY"] = a.ntfy
        if not (os.environ.get("OFFSIDE_NTFY") or os.environ.get("OFFSIDE_WEBHOOK") or os.environ.get("OFFSIDE_SMS_TO")):
            sys.exit("Nothing to send to. Set the text-message settings (TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, OFFSIDE_SMS_TO) "
                     "or run:  python app.py alert-test --ntfy offside-owen-7391")
        if a.sample:
            hook_ = (os.environ.get("OFFSIDE_WEBHOOK") or "").strip()
            if hook_: print("posting to the chat webhook ending ...%s" % hook_[-6:])
            n = notify("Out of line: Romania v Poland", "Poland to win at Star Sports: 1.73, everyone else averages 1.64 (+5%). Kick-off Sat 21:45. On the page, tap Star Sports's "
                       "price in the Poland row to add it to the bet slip. Checking again in a minute.", "https://www.oddschecker.com/football/uefa-nations-league/romania-v-poland/winner")
            n += notify("Star Sports's own page for Romania v Poland", "Poland to win. The same price, 1.73, is showing on Star Sports's own site. This link goes straight to the match there.",
                        "https://www.starsports.bet/")
            print("sent %d sample alert(s). These are examples of the wording, not live prices." % n); return
        n = notify("Offside test alert", "If you can read this on your phone, alerts are working. Tap to open the odds site.", "https://www.oddschecker.com/football")
        print("sent %d alert(s)%s" % (n, "" if n else ". Nothing was sent: check the connection and the topic name.")); return
    if a.cmd == "betlinks":
        key_ = os.environ.get("CONTEXT_DEV_API_KEY")
        if not key_: sys.exit("Set CONTEXT_DEV_API_KEY first.")
        res = scan.http(scan.CTX_SCRAPE, {"url": a.url, "formats": {"html": True}, "sharedParams": {"waitFor": 3500, "country": "GB", "dismissPopups": True},
                                          "maxAgeMs": 0, "timeoutOpts": {"milliseconds": 45000, "behavior": "return-partial"}}, key_, timeout=120)
        page_html = (res.get("html") or {}).get("data") or ""
        os.makedirs(DATA, exist_ok=True)
        open(os.path.join(DATA, "debug_betlinks.html"), "w", encoding="utf-8").write(page_html)
        print("page size: %d characters (saved to %s)" % (len(page_html), os.path.join(DATA, "debug_betlinks.html")))
        import html as _h
        txt = _h.unescape(page_html)
        print("\n1. One price button, with everything written on it:")
        m_ = re.search(r"<button[^>]*data-bk=[^>]*>", txt)
        print("   " + (m_.group(0)[:600] if m_ else "none found"))
        print("\n2. Links on the page that leave for a bookmaker (first 8 different shapes):")
        seen_ = []
        for u in re.findall(r'(?:https?:)?//[^"\'\s<>]*(?:clickout|betslip|bet-slip|redirect|deeplink|outbound)[^"\'\s<>]*', txt):
            shape = re.sub(r"=[^&]*", "=…", u)
            if shape not in [s for s, _ in seen_]: seen_.append((shape, u))
        for shape, u in seen_[:8]: print("   " + u[:300])
        if not seen_: print("   none found")
        print("\n3. Identifiers in the page's own data (name, how many, two examples):")
        for name in ("betId", "bet_id", "ocBetId", "selectionId", "marketId", "ocMarketId", "eventId", "subeventId", "bookmakerCode", "clickoutUrl", "betslipUrl", "deepLink", "url"):
            vals = re.findall(r'"%s"\s*:\s*"?([^",}\s]{1,120})' % name, txt)
            if vals: print("   %-14s %5d   %s" % (name, len(vals), " | ".join(vals[:2])))
        print("\n4. Data blocks in the page:")
        for name, pat in (("__NEXT_DATA__", r'id="__NEXT_DATA__"'), ("__INITIAL_STATE__", r"__INITIAL_STATE__"), ("__PRELOADED_STATE__", r"__PRELOADED_STATE__"),
                          ("application/json script", r'type="application/json"'), ("application/ld+json", r'type="application/ld\+json"')):
            print("   %-26s %s" % (name, "present" if re.search(pat, txt) else "not present"))
        return
    if a.cmd == "source":
        key_ = os.environ.get("CONTEXT_DEV_API_KEY")
        if not key_: sys.exit("Set CONTEXT_DEV_API_KEY first.")
        print("Looking for %s's own page for %s v %s through Context.dev (search, then a read with extraction) ..." % (a.book, a.home, a.away))
        t0 = time.time()
        status, link, text, there = ctx_confirm_at_source(key_, a.book, a.home, a.away, a.price, a.outcome)
        print({"seen": "SAME PRICE on the bookmaker's own site", "moved": "Match page found. It shows a DIFFERENT price",
               "not shown": "Match page found, but its price could not be read", "unreadable": "No page for this match could be found or opened",
               "no site": "This bookmaker's site is not in the list"}[status])
        print("  price there: %s   (you gave %.2f)" % (("%.2f" % there) if there else "not read", a.price))
        print("  page: %s" % (link or "none"))
        print("  took %.0f seconds" % (time.time() - t0)); return
    if a.cmd == "evidence":
        print("reading the cached odds files (run `python odds.py backtest` first if this finds nothing) ...")
        matches = load_backtest_matches()
        if not matches: sys.exit("No match data found.")
        ev = build_evidence(matches); save("evidence.json", ev)
        for k, s in ev["settings"].items():
            print("  minus %s points: %d bets, return %+.1f%%, t=%.2f" % (k, s["bets"], 100 * s["roi"], s["t"] or 0))
        print("saved %s" % os.path.join(DATA, "evidence.json")); return
    okey, ckey = os.environ.get("ODDS_API_KEY"), os.environ.get("CONTEXT_DEV_API_KEY")
    if a.cmd == "hub":
        if not ckey: sys.exit("Set CONTEXT_DEV_API_KEY first:  set CONTEXT_DEV_API_KEY=ctxt_secret_...")
        t0 = time.time()
        hubs = a.url or [DEFAULT_HUB]
        eng = Engine(odds_key=okey, ctx_key=ckey)
        save("hubs.json", sorted(set(load("hubs.json", []) + hubs)))
        added = run_hub(eng, ckey, hubs, tested=a.tested, default_kick=parse_kickoff(a.kickoff), limit=(max(a.max, 150) if a.deep else a.max),
                        workers=a.workers, deep=a.deep, within_h=a.within_hours, research=not a.deep)
        print("\n%d new match(es) watched, %d in total. Took %.0f seconds. Start the dashboard with: python app.py serve"
              % (added, len(eng.watch), time.time() - t0))
        return
    if a.cmd == "research":
        if not ckey: sys.exit("Set CONTEXT_DEV_API_KEY first:  set CONTEXT_DEV_API_KEY=ctxt_secret_...")
        eng, kick = Engine(odds_key=okey, ctx_key=ckey), parse_kickoff(a.kickoff)
        for fx in a.fixtures:
            parts = re.split(r"\s+(?:v|vs|vs\.|-)\s+", fx.strip(), maxsplit=1, flags=re.I)
            if len(parts) != 2: print('  skipped "%s": write it as "Home v Away"' % fx); continue
            t0 = time.time()
            books, text, why, links, sites = ctx_research_odds(ckey, parts[0].strip(), parts[1].strip())
            print("\n%s v %s: %d bookmakers from %d site(s) in %.0fs  %s" % (parts[0].strip(), parts[1].strip(), len(books), len(sites), time.time() - t0, why))
            print(text)
            if a.watch and len(books) >= scan.MIN_BOOKS:
                print("  added to the board" if eng.add_research(parts[0].strip(), parts[1].strip(), scope="tested" if a.tested else "untested", kickoff_ts=kick) else "  already on the board")
        return
    if a.cmd == "page":
        if not ckey: sys.exit("Set CONTEXT_DEV_API_KEY first:  set CONTEXT_DEV_API_KEY=ctxt_secret_...")
        body = {"url": a.url, "formats": {"json": True, "markdown": True, "parse": True, "html": True},
                "jsonParams": {"schema": ODDS_SCHEMA, "instructions": ODDS_INSTRUCTIONS}, "parseParams": {"rules": GRID_RULES},
                "sharedParams": {"waitFor": 2500, "country": "GB", "dismissPopups": True}, "maxAgeMs": 0,
                "timeoutOpts": {"milliseconds": 60000, "behavior": "return-partial"}}
        try:
            res = scan.http(scan.CTX_SCRAPE, body, ckey, timeout=90)
        except urllib.error.HTTPError as e:
            sys.exit("HTTP %d %s" % (e.code, e.read().decode()[:500]))
        os.makedirs(DATA, exist_ok=True)
        md = (res.get("markdown") or {}).get("data") or ""; html = (res.get("html") or {}).get("data") or ""
        tm = re.search(r"<table.*?</table>", html, re.S | re.I)
        table_html = tm.group(0) if tm else ""
        slug = re.sub(r"[^a-z0-9]+", "-", urllib.parse.urlparse(a.url).path.lower()).strip("-")[-60:] or "page"
        dump = os.path.join(DATA, "debug_%s.txt" % slug)
        with open(dump, "w", encoding="utf-8") as f:
            f.write("URL %s\nTITLE %s\nREQUEST %s\n\n===== EXTRACTION =====\n%s\n\n===== TABLE RULE =====\n%s\n\n===== PAGE TEXT =====\n%s\n\n===== THE ODDS TABLE'S HTML (or the first 600,000 characters of the page if no table) =====\n%s" % (
                a.url, (res.get("metadata") or {}).get("title"), res.get("request_id"), json.dumps((res.get("json") or {}).get("data"), indent=1),
                json.dumps((res.get("parsed") or {}).get("data"), indent=1)[:20000], md, (table_html or html[:600000])))
        print("title: %s   request id: %s" % ((res.get("metadata") or {}).get("title"), res.get("request_id")))
        for k in ("markdown", "html", "json", "parsed"):
            o = res.get(k) or {}
            print("%-9s success=%s %s" % (k, o.get("success"), (o.get("message") or "")[:120]))
        print("text length %d, html length %d" % (len(md), len(html)))
        print("extraction returned: %s" % json.dumps((res.get("json") or {}).get("data"))[:900])
        pd = ((res.get("parsed") or {}).get("data") or {}).get("rows") or []
        print("table-cell rule matched %d rows%s" % (len(pd), (": " + json.dumps(pd[:3])[:500]) if pd else ""))
        for label, pat in (("data-odig", r"data-odig"), ("data-bk", r"data-bk"), ("data-bname", r"data-bname"), ("<table", r"<table")):
            print("html contains %-11s %d times" % (label, len(re.findall(pat, html))))
        how = {}
        hint = ((res.get("json") or {}).get("data") or {}).get("result_rows")
        print("row names according to the extraction: %s" % hint)
        hg = html_grid(html) or html_buttons(html, *teams_in_address(a.url), info=how, rows_hint=hint)
        print("\nPRICES READ STRAIGHT FROM THE HTML: %d bookmakers" % len(hg))
        if how: print("   (%s; bookmaker names %s)" % (how.get("rows"), how.get("names")))
        for k, v in sorted(hg.items()): print("   %-24s home %.2f   draw %.2f   away %.2f" % ((k,) + v))
        bpos = [m.start() for m in re.finditer(r"<button[^>]*data-bk", html)]
        if bpos:
            nb = len(set(re.findall(r'<button[^>]*data-bk="([^"]+)"', html)))
            print("\n%d price buttons for %d bookmakers. HTML just before and after the first button of each row:" % (len(bpos), nb))
            for i in (0, nb, 2 * nb):
                if i < len(bpos):
                    print("   --- button %d ---\n   %s" % (i + 1, re.sub(r"\s+", " ", html[max(0, bpos[i] - 700):bpos[i] + 450])))
        tm = re.search(r"<table.*?</table>", html, re.S | re.I)
        table_html = tm.group(0) if tm else ""
        tags = re.findall(r"<[^<>]*data-bk[^<>]*>", html)
        print("STRUCTURE SAMPLE (paste this back if the line above says 0):")
        for t in tags[:2] + tags[len(tags) // 2: len(tags) // 2 + 3]: print("   " + t[:400])
        rm = re.search(r"<tr[^>]*>(?:(?!</tr>).)*data-bk(?:(?!</tr>).)*</tr>", table_html, re.S)
        rows_all = re.findall(r"<tr[^>]*>.*?</tr>", table_html, re.S)
        for r in rows_all[:1] + rows_all[1:2] + rows_all[-1:]:
            print("   ROW: " + re.sub(r"\s+", " ", r)[:1100])
        conv = fractions_to_decimals(md).splitlines()
        rows = [l for l in conv if len(scan.ODDS.findall(l)) >= 3]
        print("%d text lines hold three or more prices. The first 25 (cut to 230 characters):" % len(rows))
        for l in rows[:25]: print("   " + l[:230])
        print("readers: columns-from-text %d, rows-from-text %d bookmakers" % (len(coherent(grid_from_text(md))), len(coherent(scan.parse_page(fractions_to_decimals(md))))))
        m = re.search(r"data-odig", html)
        if m: print("html around the first price cell:\n   " + html[max(0, m.start() - 500):m.start() + 400].replace("\n", " "))
        print("\nSaved everything to %s  <- upload this file to the chat" % dump); return
    if a.cmd == "matches":
        if not ckey: sys.exit("Set CONTEXT_DEV_API_KEY first:  set CONTEXT_DEV_API_KEY=ctxt_secret_...")
        kick = parse_kickoff(a.kickoff)
        if a.kickoff and not kick: sys.exit('Could not read the kick-off time. Write it like "2026-11-14 17:00".')
        eng, added, t0 = Engine(odds_key=okey, ctx_key=ckey), 0, time.time()
        for fx in a.fixtures:
            parts = re.split(r"\s+(?:v|vs|vs\.|-)\s+", fx.strip(), maxsplit=1, flags=re.I)
            if len(parts) != 2:
                print('  skipped "%s": write it as "Home v Away"' % fx); continue
            home, away = parts[0].strip(), parts[1].strip()
            print("  %s v %s" % (home, away))
            url, n, best = find_odds_page(home, away, lambda q: ctx_search(ckey, q), lambda u: ctx_read_odds(ckey, u))
            if url:
                added += eng.add_watch(url, home, away, scope="tested" if a.tested else "untested", kickoff_ts=kick)
                print("      -> watching: %d bookmakers read from %s" % (n, url))
            else:
                rb, _, rw, _, sites = ctx_research_odds(ckey, home, away)
                if len(rb) >= scan.MIN_BOOKS:
                    added += eng.add_research(home, away, scope="tested" if a.tested else "untested", kickoff_ts=kick)
                    print("      -> watching via web research: %d bookmakers from %s" % (len(rb), ", ".join(sites)))
                else:
                    print("      -> no readable odds page, and %s" % rw)
        print("\n%d of %d match(es) now being watched. Took %.0f seconds. Start the dashboard with: python app.py serve" % (added, len(a.fixtures), time.time() - t0))
        return
    if a.cmd == "discover":
        if not ckey: sys.exit("Set CONTEXT_DEV_API_KEY first:  set CONTEXT_DEV_API_KEY=ctxt_secret_...")
        leagues = list(a.leagues) + ([SOON] if a.soon or not a.leagues else [])
        days = 2 if (a.soon and not a.leagues) else a.days
        print("Searching with Context.dev: fixtures, then odds pages, then reading prices. Allow a few minutes per match.")
        rows = discover(ckey, leagues, days=days, limit=a.max)
        eng, added = Engine(odds_key=okey, ctx_key=ckey), 0
        for r in rows:
            if r["status"].startswith("watching"):
                added += eng.add_watch(r["url"], r["home"], r["away"], scope="tested" if a.tested else "untested", kickoff_ts=r["kickoff"])
        print("\n%d match(es) found, %d now being watched." % (len(rows), added))
        for r in rows:
            when = time.strftime("%a %H:%M", time.localtime(r["kickoff"])) if r["kickoff"] else "time unknown"
            print("  %-10s %-44s %s" % (when, (r["home"] + " v " + r["away"])[:44], r["status"]))
        if rows and not added:
            print("\nMatches were found but no odds page could be read for any of them. Paste this output back.")
        return
    if a.cmd == "leagues":
        if not okey: sys.exit("Set ODDS_API_KEY first (free key from the-odds-api.com):  set ODDS_API_KEY=yourkey")
        data, _ = api_get("/sports", {}, okey)
        rows = [s for s in data if (s.get("key") or "").startswith("soccer") and s.get("active") and not s.get("has_outrights")]
        print("Football leagues with odds right now (* = one of the 18 tested leagues):")
        for s in sorted(rows, key=lambda s: (s["key"] not in TESTED_LEAGUES, s["key"])):
            print("  %s %-42s %s" % ("*" if s["key"] in TESTED_LEAGUES else " ", s["key"], s.get("title") or ""))
        print("\nwatch some with:  python app.py league KEY KEY ..."); return
    if a.cmd == "league":
        eng = Engine(odds_key=okey, ctx_key=ckey)
        for k in a.keys:
            print("  %s %s%s" % ("added  " if eng.add_league(k) else "already", k, "  (tested league)" if k in TESTED_LEAGUES else "  (outside the tested leagues)"))
        return
    if a.cmd == "add":
        if not ckey: sys.exit("Reading odds pages needs Context.dev:  set CONTEXT_DEV_API_KEY=ctxt_secret_...")
        ok = Engine(odds_key=okey, ctx_key=ckey).add_watch(a.url, a.home, a.away, a.kickoff, "tested" if a.tested else "untested")
        print("added" if ok else "already being watched"); return
    if a.cmd == "serve":
        eng = Engine(odds_key=okey, ctx_key=ckey, interval=max(60.0, a.minutes * 60), regions=a.regions, in_play=a.in_play)
        eng.pot_start, eng.stake_pct = a.pot, a.stake_pct
        eng.alert_min = a.alert_min
        eng.parallel = max(1, min(40, a.parallel))
        if os.environ.get("OFFSIDE_WEBHOOK"): print("Alerts ON: posting to the chat webhook, for prices passing minus %d or stricter." % a.alert_min)
        if a.ntfy: os.environ["OFFSIDE_NTFY"] = a.ntfy
        if os.environ.get("OFFSIDE_NTFY"): print("Phone alerts ON: ntfy topic '%s'." % os.environ["OFFSIDE_NTFY"])
        if os.environ.get("TWILIO_SID") and os.environ.get("OFFSIDE_SMS_TO"): print("Text messages ON: to %s." % os.environ["OFFSIDE_SMS_TO"])
        if eng.leagues and not okey:
            sys.exit("Leagues are being watched but ODDS_API_KEY is not set:  set ODDS_API_KEY=yourkey")
        if eng.watch and not ckey:
            sys.exit("Odds pages are being watched but CONTEXT_DEV_API_KEY is not set.")
        cost = len(eng.leagues) * len(a.regions.split(","))
        stop = threading.Event()
        threading.Thread(target=run_loop, args=(eng, stop, a.rediscover_hours), daemon=True).start()
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(eng))
        print("Offside (version %s) is running. Open http://127.0.0.1:%d in your browser. Ctrl+C to stop." % (VERSION, a.port))
        print("%d match page(s) watched. Each is read every %.0f min in its last 3 hours, every 20 min within a day, hourly before that."
              % (len(eng.watch), eng.interval / 60.0))
        if a.in_play: print("In-play reading is ON: matches are read every 2 min after kick-off. The test does not cover in-play prices.")
        if eng.leagues: print("%d league feed(s), %d odds data credit(s) per read." % (len(eng.leagues), cost))
        if not ckey: print("CONTEXT_DEV_API_KEY is not set: team news notes and page watching are off.")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            stop.set(); print("\nstopped")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
