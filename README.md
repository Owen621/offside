# Offside

Finds bookmaker prices that are out of line with everyone else's, on the live web, before kick-off.

Built at Junction 2026 for the Context.dev challenge.

## The idea

Bookmakers as a group price a football match very accurately. Any single bookmaker is sometimes out of line.
Offside does not try to predict matches. It takes the average price across all bookmakers as the fair view and
flags a bookmaker whose price is clearly more generous than that.

The rule comes from Kaunitz, Zhong and Kreiner (2017), *Beating the bookies with their own numbers*:

1. Take the average odds for a result across all bookmakers and turn them into a chance (1 / odds).
2. Subtract a fixed cushion of 3 to 5 percentage points.
3. Flag any bookmaker whose price is above 1 / that chance.

## Does the rule hold up?

`odds.py` tests it on 74,911 matches in 18 European leagues, 2015 to 2026, using only mainstream bookmakers
named in the data (no betting exchanges). One unit staked per bet.

| Setting | Bets | Return | t | Older seasons | Newer seasons |
|---|---|---|---|---|---|
| Minus 3 points | 4,490 | +4.2% | 1.93 | +3.0% | +8.8% |
| Minus 4 points | 975 | +13.2% | 3.43 | +11.6% | +17.8% |
| Minus 5 points | 386 | +21.7% | 3.59 | +16.7% | +30.4% |

Betting every result at the average price over the same matches lost 7.7%.

The rule and its settings were published in 2017 using data up to 2015. Every match in this test was played
after that, so the rule could not have been fitted to it.

## How it works

1. **Find matches.** Context.dev reads a league's odds page and returns every link on it. Offside keeps the links
   that are match pages.
2. **Read prices.** Context.dev renders each match page as a UK visitor and returns its HTML. Offside reads every
   bookmaker's home, draw and away price directly from the page's price grid, matched by bookmaker code.
3. **Flag.** A price is flagged when it beats the rule.
4. **Confirm.** A flag is recorded only if the price is still there on the next read.
5. **Prove.** When a price is recorded, Context.dev takes a screenshot of the page, and the page is saved with the
   time, before kick-off.
6. **Explain.** Context.dev searches for team news that might explain why that bookmaker is out of line.
7. **Settle.** After the match, the final score is looked up and the entry is marked won or lost.

Each match is read hourly when kick-off is more than a day away, every 20 minutes within a day, and every
5 minutes in the last 3 hours. New matches are looked for every 3 hours.

## Context.dev endpoints used, and why

| Endpoint | Used for | Why it is needed |
|---|---|---|
| `POST /web/scrape` with `formats.parse` (custom rule returning every link) | Discovering match pages from a league page | No fixture list has to be typed in or maintained |
| `POST /web/scrape` with `formats.html`, `sharedParams.waitFor`, `country: GB`, `dismissPopups`, `maxAgeMs: 0` | Reading every bookmaker's price for a match | The price grid is drawn by scripts, differs by country and changes by the minute, so it needs a rendered, fresh, UK view of the page |
| `POST /web/scrape` with `formats.json` and a schema | Reading the kick-off time, whether the match has started, and the names of the result rows | Small facts a model reads reliably. It is not trusted for the prices themselves |
| `POST /web/scrape` with `formats.screenshot` | Proof for each recorded price | Shows the price existed before kick-off |
| `POST /web/answers` | Team news for a flagged match, and final scores | Researched answers in a fixed shape |
| `POST /web/search` | Finding odds pages when matches are named by hand (`matches` command) | A fallback when no league page is given |

The edge only exists for someone who can see many bookmakers' prices at the same moment. No single feed gives
that view, which makes this a live web problem.

## Run it

Python 3.8 or later. No packages to install.

```
set CONTEXT_DEV_API_KEY=your key
python app.py selftest
python odds.py backtest
python app.py evidence
python app.py hub "https://www.oddschecker.com/football/english/league-1" "https://www.oddschecker.com/football/english/league-2"
python app.py serve
```

Then open http://127.0.0.1:8000. On macOS or Linux use `export` instead of `set`.

| Command | What it does |
|---|---|
| `python odds.py backtest` | Downloads the historical odds files and prints the full test |
| `python app.py evidence` | Builds the chart data for the dashboard |
| `python app.py hub [pages]` | Finds matches on odds pages and starts watching them |
| `python app.py serve` | Runs the scanner and the dashboard |
| `python app.py page URL` | Shows exactly what was read from one page, for checking |
| `python app.py selftest` | Offline checks of the logic |

## Files

- `app.py`: the scanner, the Context.dev calls, the record and the dashboard
- `scan.py`: the flagging rule and shared helpers
- `odds.py`: the historical test

## Limits

- In the tested leagues the medium setting fires once or twice a week. This is a small edge.
- Bookmakers restrict accounts that bet this way and can cancel a bet placed at a mistaken price.
- The test covers 18 European leagues. Anything else is labelled as outside the tested leagues.
- Live prices currently come from one odds comparison site. Reading a site automatically may be against its terms.
- Offside finds prices. It does not place bets. Betting can lose money and is regulated differently in each country.

## Data

- Live prices, links, screenshots, team news and results: Context.dev
- Historical results and odds: football-data.co.uk
