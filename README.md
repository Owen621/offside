# Offside

Finds bookmaker prices that are out of line with everyone else's, on the live web, before kick-off, and alerts you while the price is still there.

Built at Junction 2026 for the Context.dev challenge.

## What it does

Bookmakers as a group price a football match very accurately. Any single bookmaker is sometimes out of line: slow to update, running a promotion, or simply wrong. Offside does not predict matches. It reads every bookmaker's price for a match, takes the average as the fair view, and flags the one bookmaker paying clearly more than that.

On its first evening it caught one bookmaker offering 5/4 on a home win while 23 others were between 19/20 and 11/10. The price was confirmed, recorded with a screenshot, and gone three minutes later.

When a price is out of line, Offside:

1. posts a card to Discord at the first sighting, with a link to the bookmaker, and edits that same card as the price is confirmed, checked or gone;
2. reads the page again about a minute later, and only counts the price if it is still there;
3. reads the price on the bookmaker's own site, bet365 included, as a second source, and keeps checking while the price stays out of line;
4. saves a screenshot and the page as proof, with Context.dev's request id;
5. adds the price to a shared record, settles it when the match ends, and posts the result to a results channel.

## The rule

The rule comes from Kaunitz, Zhong and Kreiner (2017), *Beating the bookies with their own numbers*:

1. Take the average odds for a result across all bookmakers and turn them into a chance (1 / odds).
2. Subtract a fixed cushion of 3 to 5 percentage points.
3. Flag any bookmaker whose price is above 1 / that chance.

## Does the rule hold up?

<!-- evidence:start -->
`odds.py` tests the rule on 74,911 matches in 18 European leagues, August 2015 to November 2026 (11.3 years), using only mainstream bookmakers named in the data, with no betting exchanges. The rule and its settings were published in 2017 using data up to 2015. Every match in this test was played after that, so the rule could not have been fitted to it.

**The return is per bet, on the money staked each time. It is not a yearly figure and it is not the return over the whole period.** A bet settles in about two hours, so the same money is staked again and again.

At the medium setting the rule returned **+13.2% on every bet** over 975 bets. Staking £20 a bet from a £1,000 bank, that is +£2,580 in total, an average of **+23% a year** on the bank, with 9 winning seasons out of 12.

| Setting | Return on every bet | Bets a year | Profit on a £1,000 bank, £20 a bet | Average a year on the bank | Worst season |
|---|---|---|---|---|---|
| Loose (minus 3) | **+4.2%** | about 397 | +£3,780 | **+34%** | -£476 |
| Medium (minus 4) | **+13.2%** | about 86 | +£2,580 | **+23%** | -£120 |
| Strict (minus 5) | **+21.7%** | about 34 | +£1,676 | **+15%** | -£60 |

Betting every result at the average price over the same matches returned -7.7% per bet. That is the bookmakers' margin. A return of 2% to 5% per bet is usually thought of as a good long-run result for a professional.

The bank figures use flat stakes with no compounding. They assume the recorded price was available every time and the account was never limited. Bookmakers do limit accounts that win this way, so capacity is the practical ceiling, not the edge.

<details>
<summary>More detail: totals, significance and season by season</summary>

| | Loose (minus 3) | Medium (minus 4) | Strict (minus 5) |
|---|---|---|---|
| Bets in the test | 4,490 | 975 | 386 |
| Total profit, staking 1 unit a bet | +189 units | +129 units | +84 units |
| Total profit at £100 a bet | +£18,900 | +£12,900 | +£8,380 |
| Profit a year at £100 a bet | about +£1,673 | about +£1,142 | about +£742 |
| Seasons that made money | 8 of 12 | 9 of 12 | 11 of 12 |
| t statistic (above 2 is unlikely to be luck) | 1.93 | 3.43 | 3.59 |
| Per bet, seasons before 21/22 | +3.0% | +11.6% | +16.7% |
| Per bet, seasons from 21/22 | +8.8% | +17.8% | +30.4% |

Season by season, profit in units at 1 unit a bet (number of bets in brackets). A single season has few bets, so one season on its own proves little.

| Season | Loose (minus 3) | Medium (minus 4) |
|---|---|---|
| 15/16 | -23 (944) | +5 (161) |
| 16/17 | +132 (944) | +41 (174) |
| 17/18 | +14 (932) | +35 (211) |
| 18/19 | -3 (317) | -6 (60) |
| 19/20 | -24 (201) | +6 (42) |
| 20/21 | +10 (229) | +2 (66) |
| 21/22 | +59 (276) | +19 (121) |
| 22/23 | -14 (106) | -6 (36) |
| 23/24 | +7 (91) | +11 (25) |
| 24/25 | +19 (129) | +15 (31) |
| 25/26 | +6 (283) | +8 (42) |
| 26/27 | +4 (38) | 0 (6) |

</details>
<!-- evidence:end -->

This section is written by `python app.py evidence` from the test data, so the numbers are the ones the code produced.

## How Context.dev is used

The edge only exists for someone who can see many bookmakers' prices at the same moment. No single feed gives that view. The prices sit on pages that are drawn by scripts, differ by country and change by the minute, which makes this a live web problem.

| Call | Used for | Why it is needed |
|---|---|---|
| `POST /web/scrape` with `formats.parse` and a link rule | Finding matches. A league page comes back as a list of links, and Offside keeps the match pages. `hub --deep` follows every league page linked from the football page | No fixture list has to be typed in or maintained |
| `POST /web/scrape` with `formats.parse` and custom rules, plus `html` and `markdown`. Options: `waitFor`, `country: GB`, `dismissPopups`, `maxAgeMs: 0` | Reading prices. The parse rules return every price button with its bookmaker code, and the names of the result rows, as structured lists | The page must be rendered, seen as a UK visitor, and fresh. Parse rules are exact, with no model involved. The HTML is the fallback reader and the markdown is kept as proof |
| `POST /web/scrape` with `formats.json` and a schema | Reading the home, draw and away price from a bookmaker's own page. Also the kick-off time and row names the first time an unknown page is read | A small, clear extraction that a model does reliably |
| `POST /web/scrape` with `formats.screenshot` | A screenshot of the odds page at the moment a price is recorded | Shows the price existed before kick-off |
| `POST /web/search` with `includeDomains` | Finding the bookmaker's own page for a flagged match | A second, independent source for the price, and the link to act on |
| `POST /web/answers` | Team news for a flagged match, final scores, and a fallback for finding a bookmaker's page | Researched answers in a fixed shape |
| `GET /brand/retrieve` | Each bookmaker's logo and colours, fetched once per bookmaker and kept | The cards and the Discord alerts show the bookmaker's own mark, not initials |

`python app.py usage` prints how many calls of each kind the running app has made.

What we learned along the way:

- JSON extraction was unreliable on a dense grid of about 70 prices, so the grid is read with parse rules. Extraction is kept for small jobs, where it works well.
- Batches are asynchronous, which suits thousands of pages but not prices that must be seconds old. Offside sends single requests side by side instead.
- Bookmakers' own sites differ. The Star Sports, Coral and Unibet pages were found by search and read. bet365's pages cannot be found by search, because the site is built in the browser with no ordinary links. Given the address of bet365's page for a league, Context.dev reads it: on the first evening it returned 3.00 for Barnsley to beat Sheffield Wednesday, the same price the comparison site showed. `python app.py bookpage "bet365"` stores one address per league.
- When a check fails, the dashboard says why: no page was found, the page would not open, or the match was not on it.
- The comparison page's own data carries, for every price, the time the bookmaker last changed it. Offside shows that age on each flag.
- The comparison page does not carry a link to an exact bet slip. The link Offside gives goes to the bookmaker's page for the match, or for the league.

## How it works

1. **Find matches.** `python app.py hub --deep` reads the league pages and starts watching every match found. The running app repeats that search by itself every three hours.
2. **Read prices.** Each match page is read hourly when kick-off is more than a day away, every 20 minutes within a day, and every 5 minutes in the last 3 hours.
3. **Flag.** A price is flagged when it beats the rule and the match kicks off within 48 hours.
4. **Confirm.** The page is read again about a minute later. The price counts only if it is still out of line.
5. **Alert and check.** A Discord card goes out at the first sighting and is edited in place as things change. Meanwhile the price is looked for on the bookmaker's own site, and that check is repeated while the price stays out of line.
6. **Prove.** A screenshot and the page text are saved with the time and Context.dev's request id.
7. **Record and settle.** The price joins the system's record. Each result is added once per match. After the match the score is looked up, the entry is marked won or lost, and a result card is posted to Discord.

## The dashboard

- **Out of line right now.** Each flagged price with a chart: every bookmaker's price for that result is a dot, and the one beyond the dashed line is lit up. Four steps show how far it has got: seen, still out of line a minute later, checked on the bookmaker's own site, added to the record. When nothing is flagged it shows the match closest to the line.
- **Reads log.** Every page read as it happens, with the time taken and the request id. "Read now" on any match triggers a read on demand.
- **Out of line earlier.** Every price that has been out of line, marked as in the record or not counted, with the reason: the bookmaker cut it, or the others moved to meet it.
- **The system's record.** One shared record, not a personal account: what would have happened if every confirmed result had been backed once with 2% of a bank of 1,000. No real money is involved.
- **Does the rule hold up?** The historical test, switchable between the three settings.

First evening, Saturday 14 November 2026, as of 20:00: 5 prices confirmed and recorded, each with a screenshot. Of the 4 whose matches had kicked off, all 4 were still above the average price at kick-off, by 11.2% on average. Three matches had finished, with 1 win and 2 losses, which is about what those prices imply and far too few to mean anything.

## Run it

Python 3.8 or later. No packages to install.

```
set CONTEXT_DEV_API_KEY=your key
python app.py selftest
python odds.py backtest
python app.py evidence
python app.py hub --deep
python app.py serve
```

Then open http://127.0.0.1:8000. On macOS or Linux use `export` instead of `set`.

For alerts, set a Discord webhook address before `serve`, and send a sample to check it. A second webhook sends results to their own channel, and an invite link puts a Join button on the dashboard:

```
set OFFSIDE_WEBHOOK=your alerts webhook address
set OFFSIDE_RESULTS_WEBHOOK=your results webhook address
set OFFSIDE_DISCORD_INVITE=your invite link
python app.py alert-test --sample
```

Options for `serve`:

- `--window-hours 48` is the default: only matches kicking off within 48 hours are flagged and recorded, which is close to the window the test covers. `--window-hours 0` lifts it, so matches any time ahead can be flagged.
- `--alert-min 4` alerts only on the medium and strict settings. The default, 3, also alerts on the loose setting, which fires several times a day when many matches are watched.
- `--public` is for sharing the dashboard's address: pages cannot be added from it and its buttons are paced.

| Command | What it does |
|---|---|
| `python odds.py backtest` | Downloads the historical odds files and prints the full test |
| `python app.py evidence` | Builds the chart data for the dashboard and rewrites the results section of this README |
| `python app.py hub --deep` | Finds matches on the league pages and starts watching them. `--within-hours 200` widens it to the coming week |
| `python app.py serve` | Runs the scanner, the alerts and the dashboard |
| `python app.py page URL` | Shows exactly what was read from one odds page |
| `python app.py source BOOKMAKER HOME AWAY PRICE home\|draw\|away` | Looks for a bookmaker's own page for a match and reads its price |
| `python app.py bookpage "bet365"` | Stores a bookmaker's own page for each league being watched, one paste per league. Flags then link there and the price is checked there |
| `python app.py logos` | Fetches every bookmaker's logo through Context.dev's brand data. `serve` also fetches the missing ones when it starts |
| `python app.py usage` | Prints how many Context.dev calls of each kind have been made |
| `python app.py alert-test --sample` | Posts a sample alert card and a sample result card, marked as samples, which remove themselves |
| `python app.py selftest` | Offline checks of the logic. It makes no network calls |

## Limits

- The edge per bet is large and the number of bets is small. In the tested leagues the medium setting fires once or twice a week.
- Live flags are more frequent than the test suggests. The app reads about 24 bookmakers per match, reads each match many times over a week, and sees early prices, where the test saw one snapshot a day or two before kick-off. So the test's returns should not be assumed for every live flag. By default only matches within 48 hours of kick-off are flagged, and `--alert-min 4` keeps the alerts to the stricter settings.
- Bookmakers restrict accounts that bet this way and can cancel a bet placed at a mistaken price. That is the practical ceiling.
- The test uses one price snapshot per match and assumes the price could be had. It covers 18 European leagues. Anything else is labelled as outside the tested leagues.
- Live prices come from one odds comparison site. Reading a site automatically may be against its terms. The site-specific reader is one function, so the source can be changed.
- Offside finds prices. It does not place bets. Betting can lose money and is regulated differently in each country.

## What comes next

- Context.dev monitors for league pages, so a changed fixture list triggers the search for new matches.
- bet365 pages for every league, found without being pasted in.
- Proper hosting, in place of a laptop.
- Placing bets is not built. It would need bookmaker accounts and raises regulatory questions.

## Files

- `app.py`: the scanner, the Context.dev calls, the alerts, the record and the dashboard
- `scan.py`: the flagging rule and shared helpers
- `odds.py`: the historical test

## Data and credits

- Live prices, links, screenshots, team news, results and bookmaker logos: Context.dev
- Historical results and odds: football-data.co.uk
- The rule: Kaunitz, Zhong and Kreiner (2017)
- Design directions explored with Inspo by Context.dev
