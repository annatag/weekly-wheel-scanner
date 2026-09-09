# Weekly Wheel Scan

Read-only tooling for selling cash-secured puts and covered calls. It ranks a
universe of tickers, then tells you which strike, which expiry and what limit
price to use. **It never places, modifies or cancels an order.**

```bash
python weekly_wheel_scan.py                 # rank the best puts to sell this week
python wheel_advise.py INTC                 # deep-dive one ticker: strike, expiry, price
python wheel_positions.py --check ...       # validate a trade before you place it
python wheel_positions.py                   # monitor what is already open
```

See [Weekly routine](#weekly-routine) for the order to run these in.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python wheel_secrets.py store      # paste your Alpaca keys, input is hidden
python wheel_secrets.py status     # confirm where they resolve from
```

That is the whole setup. **Credentials go in the macOS Keychain, not a file** —
there is no `.env` to create.

```bash
python wheel_secrets.py migrate    # only if you already have a .env to move in
```

Resolved in order: real environment variables, then the Keychain, then
`$WHEELSCAN_ENV`, `~/.config/wheelscan/.env`, and finally `<repo>/.env`. Every
file location still works, so nothing breaks on upgrade, and a credentials
file readable by other users prints a warning naming the fix.

A file outside the repository already closes the accidental-leak paths — a
stray `git add -f`, a `zip -r` of the project, a shared folder, another
account on the machine. The Keychain closes the rest: nothing is plaintext at
rest, and backups store ciphertext rather than the keys themselves.

It is **not a sandbox**. Anything running as you can shell out to
`security find-generic-password` and read the same value. This raises the cost
of a leak; it does not make one impossible.

IBKR needs no credentials here: authentication is you logging into TWS, the
connection is to `127.0.0.1` only, and both call sites pass `readonly=True`.
There is no order-placing code anywhere in this repository.

Data comes from Alpaca's free tier by default. The tier splits by recency
rather than by product, so daily bars use `sip` (full consolidated volume)
while live quotes use `iex`, and options use the `indicative` feed. No paid
subscription is required.

Run the offline tests — 113 of them, no network — with:

```bash
python -m unittest test_wheelkit test_risk
```

## Weekly routine

Every command assumes:

```bash
cd ~/Repos/weekly-wheel-scan && source .venv/bin/activate
```

### Already running

The position monitor fires weekdays at 15:00 and 16:15 via launchd and stays
silent unless something trips. Read it whenever you like:

```bash
tail -20 logs/positions.log logs/positions.err
```

Alerts land in `positions.log`; `positions.err` carries the warning when
positions were read from the file instead of your broker.

It also **pushes**, so you do not have to remember to look:

```bash
python wheel_positions.py --notify-test     # prove both channels work
```

- **macOS banner** — nothing to install, but only reaches you at the machine.
- **ntfy.sh push** — reaches a phone, no account needed. Pick any
  hard-to-guess topic, store it, and subscribe to it in the ntfy app:

  ```bash
  python wheel_secrets.py --set WHEELSCAN_NTFY_TOPIC
  ```

  The topic is effectively a password — anyone who knows it can read your
  alerts — so it lives in the Keychain rather than a plist or shell profile.

`--no-banner` and `--no-push` disable either. Delivery failures never raise:
a notification server being briefly unreachable must not take down the
monitor and lose the alert it was trying to deliver.

### After Friday's expiry — reconcile

Expired and assigned positions vanish from the broker but stay in
`positions.csv`, where they keep alerting on contracts that no longer exist.

```bash
python wheel_positions.py --source ibkr     # authoritative, needs TWS open
```

Without TWS, delete the closed rows from `positions.csv` by hand.

### Then, in order

```bash
python weekly_wheel_scan.py --top 10                        # 1. what to sell
python wheel_advise.py HOOD                                 # 2. strike and expiry
python wheel_positions.py --check HOOD 84 P 2026-09-18 1.20 # 3. VALIDATE
python wheel_trade_suggestions.py                           # 4. re-quote
                                                            # 5. place it
python wheel_fills.py record HOOD P 2026-09-18 84 1 1.20    # 6. record the fill
```

**Step 3 is the one that changes outcomes.** Steps 1 and 2 tell you what is
worth selling; step 3 is the only one that stops you selling something else.
Four of the five positions in the paper book skipped it, and the two furthest
outside the delta band were the two largest losses.

**Step 6 is the one that makes the rest reviewable.** A strike gets nudged for
a better bid, an expiry slides a week to clear an earnings date, a limit gets
walked past the floor to fill at all — all reasonable at the ticket, and each
one breaks the link between what the scan recommended and what happened.
`wheel_fills.py` records the fill against the suggestion it came from and
computes the difference while the scan is still on disk, so the model is
graded on its own recommendations rather than on trades that drifted from
them.

Run the scan while the market is open. Outside hours, spreads widen enough
that "spread too wide" becomes the largest rejection bucket and the results
understate what is actually available.

### Monthly

```bash
python build_universe.py
```

Rebuild when the universe is a few weeks old, or whenever you change
`--max-cash` — the price ceiling has to move with it, and the scan will refuse
to run until it does.

### Ad hoc

| When | Command |
|---|---|
| A holding moved hard | `python wheel_positions.py` |
| Force a monitor run now | `launchctl kickstart -k gui/$(id -u)/com.wheelscan.positions` |
| After changing any code | `python -m unittest test_wheelkit test_risk` |
| Check where credentials resolve | `python wheel_secrets.py status` |

## Screening the business as well as the option

This scanner screens the **option**: implied volatility, spread, assignment
odds. It has no view on whether the underlying is a business worth owning,
which matters because assignment means holding the shares.

Point it at a Finviz screen to supply both the universe and the fundamentals:

```bash
python weekly_wheel_scan.py \
  --finviz-url "https://finviz.com/screener.ashx?v=121&f=fa_pe_u30,fa_peg_u2,sh_price_o50,ta_perf_13wup,ta_perf2_4wdown" \
  --max-pe 30 --max-peg 2.0
```

Use the **valuation view (`v=121`)** — it carries PEG, which the overview view
does not. Finviz decides which stocks qualify; everything downstream is
unchanged. Expect few survivors: the two screens pull in opposite directions,
because a low P/E and a stable PEG describe exactly the businesses whose
options are cheapest.

This is meant for a handful of page loads when you rebuild the universe, not
a per-symbol API. Finviz rate-limits aggressively and reserves bulk access for
Elite subscribers, who get a proper CSV export.

### Trend setups

The quarter sets the direction and the month says where price sits within it:

| Setup | Quarter | Month | Meaning |
|---|---|---|---|
| **pullback** | up | down | Uptrend intact, dip in progress — the setup a put seller wants |
| momentum | up | up | Healthy but extended, and volatility is usually cheap |
| rebound | down | up | Bouncing inside a downtrend |
| falling knife | down | down | Excluded by default |

`--require-pullback` restricts the scan to the first row. Falling knives are
dropped unless you pass `--allow-falling-knife`. Both gates apply only to
puts; a covered call is written against shares you already hold.

## Risk controls

The scanner ranks what is *worth* selling. Nothing enforced what actually got
sold, and that gap is where losses come from: on a five-position paper book,
four entries sat outside the scanner's own delta band, the two worst were the
two furthest outside it, and the single position inside the band was the
clean winner.

`wheel_positions.py` closes that gap at three points.

**Before you trade** — a pass/fail gate on a specific contract:

```bash
python wheel_positions.py --check GM 87 P 2026-09-18 1.09
```

Refuses a strike sold in the money, a delta above the band, an expiry that
spans earnings, or a falling-knife setup, and suggests a position size.

**On collateral.** The sizing model reserves strike × 100 per contract, which
is what a cash-secured put requires. An account can hold the same positions on
margin — identical contracts, different risk: assignment draws on borrowing
rather than on money already set aside, and a drawdown can force a close at the
worst moment instead of simply converting to stock. The monitor compares
committed collateral against settled cash and says so when they diverge:

```
$16,000 of collateral against $160 settled cash - $15,840 of these puts is
margin-secured, not cash-secured. Assignment would draw on borrowing, and
the sizing model assumes otherwise
```

It reports and never blocks. Which way to run the account is not the tool's
decision — but the trade card should not imply cash that is not there.

**At sizing** — contracts are scaled so a two-sigma adverse move stays inside
a risk budget, rather than filling a fixed cash sleeve. Filling a sleeve puts
the most contracts on the cheapest stock, which is usually the most volatile:
a $28 name took five contracts and a 10% week cost five times what one would
have.

The caps are tied to the sleeve, and the sleeve is set by the most expensive
stock the universe admits. At $220 × 100 = $22,000 on a $100,000 account:

| | value | why |
|---|---|---|
| Cash per position | $22,000 | one contract on a $220 stock |
| Capital per position | 22% | so the gate can size what the scanner suggests |
| Two-sigma risk budget | 2% ($2,000) | usually the binding constraint, not the caps |
| Max open positions | 3 | $22,000 each against a 60% total-capital cap |

**Raising the price ceiling costs diversification, and the position count says
so.** Two full-size positions fit inside the 60% total-capital cap; a third
has to be a cheaper name. The count was 8 when the sleeve was $15,000 — left
there, it would have described a spread the account can no longer hold. If the
book is mostly expensive names, three positions *is* the portfolio, and the
correlation cap matters more, not less.

Raising the sleeve also enlarges positions in cheap names, not just expensive
ones: a $100 stock that took one contract at a $15,000 sleeve takes two at
$22,000. The two-sigma budget still binds — that is what stops it becoming
five — but the whole book scales, which is the part that is easy to miss.

**While open** — monitoring, silent unless something trips:

```bash
python wheel_positions.py                 # full table plus alerts
python wheel_positions.py --alerts-only   # for a scheduled job
```

Alerts on: a position going in the money, delta past 0.50, 50% of max profit
captured, the last three days before expiry at a live delta, and a daily move
over 5% in the underlying.

**The checkpoint is asymmetric.** At 21 DTE a position that has captured less
than 35% of its credit is raised for a decision — close, roll out, or hold it
on purpose. One already past that threshold is working and stays silent. This
is deliberately not "close everything at 21 DTE": in the batch that prompted
the rule, GDX at 29.8% and SLV would both have tripped it, while DRAM was
already past 66% and went on to close at 90%. A flat rule would have
interrupted the trade that needed no decision at all, which is how a rule
stops being read.

```bash
python wheel_positions.py --time-stop-dte 21 --time-stop-capture 0.35
```

The checkpoint is **half the contract's life, capped at 21 DTE** — the same
rule the trade card prints as its roll date, so the monitor and the plan never
disagree. Half is what matters here: this scanner sells 7–21 day contracts, and
a flat 21-DTE checkpoint on a 16-day trade has already passed on the day you
open it. Working that out needs the original life, so add an optional
`entry_date` column to `positions.csv`:

```csv
symbol,expiration,strike,right,quantity,entry_credit,sector,entry_date
GDX,2026-09-18,89,P,-1,0.94,,2026-09-02
```

Without it the checkpoint falls back to the flat 21 DTE, which is early for a
two-week trade.

Positions read from a **broker** carry no entry date at all — IBKR reports what
you hold, not when you opened it, and its execution history reaches back only
to the current session. So for those the date comes from the fill log, matched
on symbol, right, expiry and strike. This is the second reason to record
fills: it started as a way to grade the scanner, and it turns out to be what
makes the exit rule correct. The monitor says how many positions are missing
one rather than quietly using a checkpoint that has already passed:

```
note: 5 of 6 position(s) have no entry date, so their checkpoint falls back
      to 21 DTE rather than half the trade's life.
```

The difference is not cosmetic. A C $138 call written 21 days out reported a
`21-DTE checkpoint (Aug 21)` — twelve days in the past — and with the fill
logged became a `10-DTE checkpoint (Sep 01)`.

**Deadlines land on trading days.** "21 DTE" resolves backwards to the nearest
prior session, never forwards. Subtracting calendar days puts the checkpoint
on a weekend two weeks in seven and on a holiday a few times a year, and a
deadline nobody can act on is met late by definition — always late, never
early, because the drift only goes one way.

**Correlated names count as one position.** The sector limit counts positions
per sector string, which ETFs do not carry: GDX and SLV both arrived
uncategorised and were treated as diversification while being a single bet on
the gold price. Underlyings are also grouped explicitly — precious metals,
semiconductors, energy, China, crypto proxies and so on — and the group is
capped both by position count (2) and by share of the account (20%):

```
 ! $13,700 (68% of the account) is committed to precious metals across
   GDX, SLV, above the 20% limit
```

The groups live in `wheelkit/correlation.py` and are meant to be extended.
A ticker in no group is constrained by neither cap, which is the honest
outcome — the table simply has nothing to say about it.

Severity follows the **odds**, not the calendar. An in-the-money position is
urgent once assignment odds pass 85% or expiry is within three days; anything
shallower with time left is a warning. Expiry day is stated as certain rather
than as a probability, because a cent in the money at the close is
auto-exercised and there is nothing left to estimate:

```
!! CCL $28P 7.8% ITM, 7 DTE - ~89% chance of assignment, 11% it recovers
!! C $136P 5.3% ITM, 0 DTE - assignment is now certain
 ! HOOD $90P moved +12.7% in a day
```

`!!` is urgent, `!` a warning, and urgent lines sort first so they survive the
notification's length cap.

Positions are read from IBKR, Alpaca or `positions.csv`, tried in that order.
The CSV matters because IBKR only reports while TWS is running, and a monitor
that silently reports nothing whenever TWS is closed is worse than none.

**When the CSV is used, it says so** — on stderr, including under
`--alerts-only`, so a scheduled run records it in the log:

```
WARNING: positions came from positions.csv, NOT your broker.
    ibkr     TWS not reachable
    alpaca   no positions
    csv      5 position(s) <- used
```

The file is hand-maintained: closed or expired positions keep alerting until
you edit it, and entry prices are whatever was typed in. A quiet fallback is
exactly the failure this tool exists to catch, so it is never quiet.

**An empty book and an unreachable broker are reported differently**, because
they are opposite facts that produce the same empty list. A run that found
nothing but could not reach every source says so on stderr and exits 2 — even
under `--alerts-only`, since that is the case the scheduled job most needs to
raise:

```
WARNING: found no positions, but not every source answered.

    ibkr     TWS not reachable
    alpaca   no positions
    csv      no positions
```

This replaces a line that printed the literal flag it had been given
(`Sources tried: auto`) rather than the per-source outcomes it had already
collected, so a monitor that could not see anything read exactly like a clean
book. It said that once while six positions were open.

`--source ibkr` refuses to fall back at all and errors instead, which is what
you want when checking whether TWS is actually reachable.

### Running it on a schedule

```bash
./scripts/install_schedule.sh
```

Installs three jobs, weekdays, in the machine's local time:

| | | |
|---|---|---|
| 10:30 | `requote` | re-quotes last night's scan and flags anything that gapped |
| 15:00 | `positions` | open-position alerts |
| 15:45 | `scan` | builds the watchlist for tomorrow |
| 16:15 | `positions` | open-position alerts, marks settled |

The pairing is the point: **scan late, trade the next morning.** The 15:45 run
picks contracts off a full day of price action; the 10:30 run re-prices them
against the open before you commit, because an option's bid/ask moves far more
overnight than the stock does.

15:45 rather than after the close, because the scanner needs live quotes.
Spreads blow out once market makers step back, and an after-hours scan rejects
most of what it would otherwise surface — `spread too wide` becomes the largest
rejection bucket. 15:45 is late enough to reflect the day and still inside the
session.

The morning run **pushes its verdict**, since nobody is watching it:

```
2 of 3 still tradable.
1 gapped past the threshold: BAC $62 PUT Sep 11 -2.7%
1 no longer tradable: APH $75 PUT Sep 18 (spread/quote check)
```

It exits 1 when anything gapped or dropped out — not an error, but "read this
before you trade". A clean hold exits 0 and says so explicitly, because the
whole job of that run is confirmation and silence is not an answer.

A gap is reported separately from a drop-out on purpose. A contract that fails
the spread check announces itself; a contract that still quotes perfectly well
while the stock moved 8% under it would otherwise just come back with different
limit prices and no comment. `--max-gap` sets the threshold, default 5% to
match the position monitor's daily-move alert.

`./scripts/install_monitor.sh` still installs only the position monitor, if
that is all you want. Both installers are safe to re-run.

## Command reference

### `build_universe.py` — what to scan

Pulls every optionable US equity from Alpaca, screens on price and real
consolidated dollar volume, drops leveraged and inverse products, and writes
the survivors to `universe/symbols.txt`. Run it weekly or monthly, not daily.

**You do not have to run it first.** The scanner builds the universe itself
when the file is missing. Run this directly only to rebuild on demand or to
change the screen.

```bash
python build_universe.py                            # top 400 by liquidity
python build_universe.py --dry-run                  # preview without writing
python build_universe.py --max-price 250 --max-symbols 500   # also set --max-cash 25000
```

`--max-price` is your per-position cash ceiling divided by 100, since one
contract secures 100 shares. Leave it aligned with the scanner's `--max-cash`
— the scan refuses to run when they disagree badly enough that names can
never be sized. The file records the screen it was built with, in its header,
which is what that check reads.

The universe lives in `universe/`, which is gitignored. It sits in its own
directory rather than the repository root because a branch checkout that
changes whether the file is tracked will otherwise delete it — which is
exactly what happened once, after which the scanner silently fell back to its
33 built-in tickers and reported a whole session of results as though it had
screened the full list.

That fallback is now loud. If the universe cannot be read or built, the scan
prints a warning naming the exact rebuild command before it runs, and
`--no-auto-build` disables the automatic rebuild entirely.

### `weekly_wheel_scan.py` — what to sell

Ranks every ticker in `symbols.txt` and prints a table plus a full trade card
for each pick.

```bash
python weekly_wheel_scan.py --top 5
python weekly_wheel_scan.py --right call            # covered calls on shares you hold
python weekly_wheel_scan.py --symbols SOFI,INTC,F --min-dte 7 --max-dte 14
python weekly_wheel_scan.py --require-uptrend       # only names above their 50-day
```

Results are written to `wheel_scan_results.csv`, including every sub-score so
you can see *why* something ranked where it did.

#### The screen, in numbers

Every default in one table, because a filter you cannot see is one you cannot
argue with. `--help` prints the same values; this is the version you can read
without running anything.

| Gate | Default | Flag |
|---|---|---|
| Share price | $5 – $220 | `build_universe.py --min-price / --max-price` |
| Cash per position | $3,000 – $22,000 | `--min-cash` / `--max-cash` |
| Days to expiry | 7 – 21 | `--min-dte` / `--max-dte` |
| Delta band | 0.10 – 0.22 | `--min-delta` / `--max-delta` |
| Bid/ask spread | ≤ 12%, **or ≤ 2 ticks** | `--max-spread-pct` |
| Credit | ≥ 0.3% of the strike, and ≥ $0.05 | `--min-credit-pct` |
| Implied ÷ realised vol | ≥ 1.0 | `--min-vrp` |
| Annualised return | ≥ 12% | `--min-annualised` |
| Dollar volume | ≥ $50M/day | `--min-dollar-volume` |
| Universe size | top 400 by liquidity | `build_universe.py --max-symbols` |
| Earnings | excluded **within ±3 days of expiry** | `--allow-earnings` |
| Down on the quarter *and* the month | excluded | `--allow-falling-knife` |

**`--max-price` × 100 = `--max-cash`.** One contract secures 100 shares, so
$220 × 100 = $22,000. These are set by two different commands with two
different defaults, so they drift — and when they do, the scan spends its
whole run pricing contracts it silently drops at sizing, leaving no trace but
an `outside cash sleeve` reject count nobody reads.

The scanner now checks. A universe wider than the sleeve can reach is refused
outright, with the exact fix:

```
REFUSING TO SCAN: the universe and the cash sleeve disagree.

  The universe admits stocks up to $220, but a $15,000 sleeve secures
  a strike of only $150. Reaching the top of the universe would need a
  strike 31.8% out of the money, deeper than the delta band goes -
  those names can never be sized and will be priced and then dropped
  every run. Set --max-cash 22000 to match, or rebuild the universe
  with --max-price 150.
```

A sleeve *larger* than the ceiling passes silently — that is merely
conservative. A sleeve slightly too small warns instead of refusing, because
part of the delta band is still reachable. `--allow-sleeve-mismatch` scans
anyway. The check reads the screen out of the universe file's own header, so
it compares what was actually built rather than what the flags say today.

Note that the ceiling is a *spot* price while sizing uses the *strike*, and a
put strike sits below spot. Across 7–21 DTE and 25–50% implied volatility the
0.10–0.22 delta band lands 2.5%–13.4% out of the money, so a $220 stock
actually needs somewhere between $19,000 and $21,500 — the $22,000 sleeve
covers the whole band with room to spare.

**The credit floor is a percentage, not a dollar amount.** It used to be a
flat $0.15 per share, which is the only gate in the list denominated in
dollars rather than in percent — so it screened on share price instead of on
premium. At a 0.18 delta and 14 days, an identical trade paying 0.73% of the
strike clears $0.15 comfortably on a $100 stock and fails it on anything under
about $20, no matter how rich the volatility. Credit as a share of the strike
is the same number at every price. The absolute floor survives at $0.05, where
it does the one job it is good for: rejecting a premium so small the spread
takes it back on the way out.

### `wheel_advise.py` — how to sell it

For a ticker you have already chosen. Prints three things:

1. **Duration table** — the best strike near your target delta at *each* expiry,
   so you can see which holding period actually pays. Annualised return is
   frequently much better at 7 days than at 35.
2. **Strike ladder** — the risk/reward trade-off across strikes for the best
   expiry, from 0.10 to 0.30 delta.
3. **Recommendation** — one contract, fully costed, with the limit ladder.

```bash
python wheel_advise.py INTC
python wheel_advise.py MU --right call --shares 300 --cost-basis 80
python wheel_advise.py SOFI --max-dte 45 --target-delta 0.25 --capital 10000
```

The advisor deliberately runs looser filters than the scanner. You named the
ticker, so it shows you the menu rather than returning an empty screen.

### `wheel_covered_call.py` — after you get assigned

Shares and cost basis come from the broker first — IBKR, then Alpaca, then
`shares.csv` — and the source is always stated:

```
Holdings from ibkr: 100 shares at $132.17 average cost.
```

This matters more than it looks. The basis gates every strike in the ladder,
and it used to be read from Alpaca alone, which on this machine is a different
account entirely from the one holding the wheel book. Falling through to a
hand-typed file is how `$135.19` survived against a lot the broker priced at
`$132.17` — a $302 error on 100 shares, in the number that decides whether a
strike is "below basis". When the file is used, it says so and tells you to
verify.

Assignment usually happens because the stock fell, so the shares often arrive
already underwater. That is the case a plain screen handles worst: strikes
above your basis pay almost nothing, and strikes that pay well would realise
a loss if called away. This shows both rather than hiding one.

```bash
python wheel_covered_call.py CCL --assigned-from 28 0.656   # strike, put credit
python wheel_covered_call.py CCL --shares 500 --basis 27.34
python wheel_covered_call.py GM                             # basis from the broker
```

`--assigned-from` derives the basis correctly: assignment cost you the strike,
but you kept the put premium, so the basis is the strike **minus** that credit.

Each strike is costed three ways — premium if it expires, profit or loss on
the shares if called away, and the two combined. That last column is the one
that matters, and it is not monotonic in the obvious way: on a $27.34 basis,
the $27 strike is below basis yet still nets **+$185** if called, because $358
of premium more than covers the $172 share loss. A blanket "never sell below
basis" rule would hide that trade.

Strikes below basis are excluded unless you pass `--allow-below-basis`, and
always marked with `*` when shown.

There is also recovery math: how many cycles of premium would close an
underwater gap if the stock goes nowhere — 4.3 cycles, roughly 22 weeks, in
the CCL example.

### `wheel_trade_suggestions.py` — is it still good?

Re-quotes the saved scan against the current market and reprices the limits.
Option quotes move far more than the underlying, so run this immediately before
trading. Contracts that no longer pass the spread check are marked `WAIT`, and
the original position size is preserved.

### `wheel_backtest.py` — did any of this work?

Every scan is archived. Every ranked candidate is resolved against where the
underlying actually closed on its expiry, whether or not you traded it.

```bash
python wheel_backtest.py resolve    # score everything that has matured
python wheel_backtest.py report     # what the archive says so far
```

**This exists because the trade log can never answer the question.** Three
concurrent positions on 7–21 day contracts is 60–100 trades a year; detecting
a few points of win rate needs several hundred. The scan ranks ten candidates
a run and you trade one — so nine free observations were being discarded every
time, including the ones that placed fourth through tenth, which are the
control group that says whether the ranking put the right contracts on top.

The report leads with the calibration test, because it is the one that can
falsify the pricing model outright:

```
  ASSIGNMENT RATE BY DELTA — the calibration test
    delta 0.10-0.14      2/31   =    6%
    delta 0.14-0.18      7/44   =   16%
    delta 0.18-0.22     11/52   =   21%
```

If 0.20-delta puts assign near 20%, the greeks are honest. Materially higher
and the cushion is lying — which is exactly what the gap-history discount
exists to correct.

Buckets thinner than `--min-sample` are marked `(thin)` rather than quietly
reported, and the score comparison refuses to draw a conclusion below twice
that. Nothing here changes what the live scan recommends.

#### When to read what

The archive is useless the day you start it and informative on a schedule.
Nothing here is worth acting on early.

| When | Look at | What would falsify something |
|---|---|---|
| weekly | `resolve` | Nothing. It accumulates. |
| ~3 weeks | first `report` | Read the counts, not the percentages. |
| ~3 months | assignment rate by delta | If 0.20-delta puts do not assign near 20%, the cushion is still lying and the gap discount is not enough. |
| ~3 months | IV rank comes online | 60 sessions of ATM history — the one measure the toolkit still cannot compute. |
| ~6 months | score halves | If the top half does not beat the bottom half, the weights are decoration and should be simplified rather than tuned. |

**Do not change the scoring while this accumulates.** Ten changes landed at
once in the last round; an eleventh before the first report makes all eleven
unattributable. And be realistic about what it can prove: ten ranked
candidates a run instead of one traded is roughly a tenfold rise in
observations, which turns "years" into "months" for coarse questions like
delta calibration. It will never make a two-point difference in win rate
visible on a three-position book. Expect the archive to falsify big errors,
not to tune weights.

`archive/atm_iv.csv` accumulates one at-the-money reading per symbol per day —
including symbols that produced no candidate, since IV rank needs the whole
distribution. That is the seed for the only measure this toolkit cannot
compute at all today: the free tier carries no option history, so "is this
option expensive *for this name*" has to be written down before it can be
asked. It becomes usable after roughly 60 sessions, which is why it starts now
rather than when it is wanted.

### `wheel_data.py` — moving to another machine

`git clone` gets you the code. It gets you none of the state, because all of
it is gitignored — deliberately, since it is position data.

```bash
python wheel_data.py pack                              # → wheelscan-data-2026-09-08.tgz
python wheel_data.py restore wheelscan-data-2026-09-08.tgz
```

Most of what it carries could be rebuilt. **Two things could not:**

- **`archive/`** — the evaluation harness, whose entire value is that it
  accumulates. The free data tier carries no option history, so a lost archive
  cannot be reconstructed from anywhere, and the clock to IV rank restarts at
  zero.
- **`fills.csv`** — the only record of what was actually traded, and the only
  source of entry dates for the checkpoint.

It also carries `positions.csv`, `shares.csv`, `universe/` and the earnings
cache. The cache is regenerable in principle; in practice it is fetched one
calendar day at a time and has taken over twenty minutes when the Nasdaq feed
is slow, so it rides along at 32 KB.

The summary counts rows rather than bytes, because "212 archived candidates,
59 more days to IV rank" tells you whether the restore worked and "16K" does
not.

**Credentials are deliberately excluded.** Putting Alpaca keys into a
plaintext tarball would undo the reason they live in the Keychain. `pack`
prints the `security find-generic-password` commands to move them yourself.

`restore` refuses to overwrite files that already have content — pass
`--force`, or `--dry-run` to see what would happen. The one exception is
`archive/scans`, which **merges**: the files are dated, so two machines'
histories union cleanly rather than one silently replacing the other.

### `wheel_fills.py` — what you actually sold

Records entries and exits against the scan that suggested them, so the
scanner can be graded on its own recommendations.

```bash
python wheel_fills.py record GDX P 2026-09-04 95 1 1.03   # symbol right expiry strike qty credit
python wheel_fills.py close  GDX P 2026-09-04 95 --debit 0.31
python wheel_fills.py close  FCX P 2026-09-11 68 --expired
python wheel_fills.py list
python wheel_fills.py report
python wheel_fills.py drop  NVDA P 2026-09-18 150      # a row that should not exist
```

`close` and `drop` are different operations. `close` records how a real trade
ended and keeps it in the record, where it is evidence about the scanner.
`drop` removes the row as though it had never been entered — for a fill typed
wrong, an order that never filled, or a trade from a different account. It
warns when the row it is removing was closed, refuses when more than one row
matches (`--all` to override), and writes a dated backup before every removal,
because a confirmation prompt does not survive a mistake made confidently and
a file does.

`record` looks the contract up in `wheel_scan_results.csv`, copies the
suggested strike, expiry, limit and score **into the row**, and states how the
fill differed:

```
Recorded FCX $68P Sep 11 x2 at $0.61 ($122 credit)
  Drifted from the 2026-08-24 scan: strike $68 vs $71 suggested;
  expiry Sep 11 vs Sep 04 suggested (+7d)
  Graded separately: the model did not recommend this contract.
```

The suggested numbers are copied rather than referenced because the next scan
overwrites the results file, and a pointer into a deleted file is worse than
no pointer. A fill within 2% on strike, 3 days on expiry and 15% on credit
counts as the suggested trade; anything further out is logged and graded
separately. `--no-scan` records a trade that had no scan behind it.

`report` splits the P/L three ways — as suggested, drifted, and no scan — which
is the only comparison that says anything about the model:

```
3 fill(s): 1 as suggested, 1 drifted, 1 with no scan attached.

  as suggested    1 closed  $      +72  1/1 green  70% of credit kept on average
  drifted         1 closed  $     +122  1/1 green  100% of credit kept on average
```

Fills live in `fills.csv`, which is gitignored along with the other position
files.

## Reading a trade card

```
  PRICE      Open at  $1.52  limit credit  → $152 total
             Likely   $1.48  (the mid)      → $148 total
             Floor    $1.46  do not go below → $146 total
```

Work the order down this ladder. Start near the ask, walk toward the mid, and
cancel rather than sell below the floor.

```
  BREAKEVEN  $80.52 (+12.4% from $91.97 spot, 0.87σ of the expected move)
  ODDS       81% chance of profit · 23% chance of assignment
  VOL        IV 103.6% vs realised 65.9% → VRP 1.53  (rich premium)
```

`σ` is the cushion measured in standard deviations of the expected move — the
honest way to compare a $5 cushion on a calm stock against $5 on a volatile
one. `VRP` is the variance risk premium: implied volatility divided by what the
stock has actually been doing. Above ~1.15 you are being paid a real premium;
below 1.0 you are selling volatility for less than the stock is realising,
which is a losing trade however tempting the headline yield looks.

```
  EXIT       Buy to close at $0.74 (50% of max profit, $90 locked in)
             Roll or close by Aug 04 (3 DTE) rather than holding into expiry
```

## How candidates are scored

A weighted blend, then **multiplied** by an event penalty:

```
score = blended × event_multiplier        # 0.55 … 1.00
```

| Weight | Component | What it measures |
|--------|-----------|------------------|
| 22% | Premium | Annualised return **in excess of the risk-free rate**, with diminishing returns |
| 22% | IV edge | Variance risk premium, discounted for skew, quiet realised vol, and extreme VRP |
| 18% | Safety | Cushion in σ (scaled by gap history) and trend, plus a support bonus |
| 14% | Setup | Quarter/month trend, scaled down by the 60-day drawdown |
| 12% | Liquidity | Bid/ask spread and option volume, floored for quotes at the tick grid |
| 8%  | Quality | Average dollar volume, log-scaled |
| 4%  | Regime | SPY volatility and trend |
| × | Event | Term-structure slope: 1.0 at or below 1.05, falling to 0.55 by 1.50 |

Adjust the weights in `WheelConfig.weights` in `wheelkit/strategy.py`.

**Event risk multiplies rather than averaging.** A flat weighted mean lets a
superb premium score carry a contract priced for a catalyst, which is backwards
for a strategy whose job is avoiding disasters. A 4% additive term could never
veto; a 0.55 multiplier can.

### The three discounts inside IV edge

VRP asks whether the option is rich against what the stock has been doing. Three
things can make that reading a lie, and each scales the term rather than getting
its own weight — they qualify the same claim, so they should not each get a vote.

| Discount | When | Why |
|---|---|---|
| ×0.75 | VRP above 2.5 | On a short-dated contract that usually means a priced event, not edge |
| → ×0.80 | Realised vol below its own 25th percentile | The denominator is small for reasons that rarely last, so the ratio flatters |
| → ×0.65 | Skew above 1.15 | Strike IV well above at-the-money means you are paid for the tail, not for edge |

### Skew and term structure

Both read the *shape* of the volatility surface rather than a single quote, and
neither needs a data subscription.

**Skew** is strike IV ÷ at-the-money IV for the same expiry. Ordinary equity
skew runs about 1.05–1.15. Well above that, the market is paying specifically
for downside protection at the strike you are selling. The at-the-money
reference comes from widening the existing strike request, not a second call.

**Term structure** is front-month ATM IV ÷ a ~45-day expiry's. Above ~1.10 is
backwardation — the near contract is pricing something the far one is not. It is
the only measure here that can see a catalyst the earnings feed does not list: a
court date, an FDA decision, a deal vote. Measured live: NVDA 0.90, KO 0.98,
MARA 1.03, GDX 1.06.

This one costs a second chain request, so it is spent only on symbols that
produced candidates. A failed fetch returns no signal rather than no candidate.

### Cushion is discounted by gap history

Cushion in standard deviations assumes the price diffuses continuously.
Assignment on a short put almost never arrives that way — it arrives overnight,
and gap risk is genuinely separate from the volatility the cushion is built
from:

| | realised vol | 5th-percentile overnight gap |
|---|---|---|
| KO | 14% | −0.70% |
| NVDA | 27% | −2.03% |
| GDX | 28% | −3.65% |
| HOOD | 55% | −3.50% |

HOOD realises twice GDX's volatility and gaps the same distance. Two contracts
with an identical `cushion_sigmas` are not carrying the same assignment risk, so
the cushion is scaled by how many typical bad opens the breakeven absorbs — a
cushion thinner than one bad open is halved, eight deep is untouched.

## What changed from version 1, and why

The previous version returned zero rows. Two independent causes:

1. **No IBKR market-data entitlement.** Every quote returned error 10089 and
   `reqHistoricalData` timed out, so every symbol failed before reaching the
   option chain.
2. **A dead Yahoo fallback.** This Python build ships without a CA bundle, so
   every HTTPS call raised `SSLCertVerificationError` — which the fallback
   caught as a generic `OSError` and turned into a silent "no data". Network
   calls now build their context from `certifi` and fail loudly.

Correctness fixes beyond the data layer:

- **Cheap stocks were silently excluded.** A `$7,000` cash floor became a
  `$70` minimum strike, so SOFI, F, INTC and T could never appear regardless
  of fit. Sizing now fills the sleeve with multiple contracts.
- **The top five could be five strikes on one ticker.** Ranking now takes one
  contract per symbol by default (`--allow-duplicate-symbols` to opt out).
- **The earnings filter never fired.** It depended on a hand-maintained
  `earnings.csv` that shipped empty. Dates now come from the Nasdaq calendar,
  cached for 12 hours, with the CSV kept as an override.
- **Config contradicted the README** — documented `$7k–$15k`, actually used
  `$7k–$50k`.

Scoring changes:

- IV was scored on an **absolute** scale, so the same high-beta names won every
  week whether or not their options were expensive *that* week. Now scored
  relative to realised volatility.
- Premium was scored on **raw** annualised return, making a 6% return on fully
  secured cash look attractive while T-bills paid 4% for none of the risk. Now
  scored on the excess.
- Downside cushion was scored in **raw dollars** (`cushion * 1000`), which
  cannot distinguish a generous cushion from a meaningless one. Now measured in
  standard deviations of the expected move.
- The quality term (`45 + 12 * log10(...)`) compressed every liquid name into a
  ~2-point band, so a quarter of the total weight did no ranking work at all.
- Market conditions were the hard-coded constant `60`. Now derived from SPY.

Greeks and implied volatility are **computed** from the quoted mid via
Black-Scholes rather than taken from a vendor, because the free tier does not
supply them. The test suite verifies this against put-call parity, finite-
difference deltas and one-day decay.

## What changed after the first live batch, and why

Five paper positions, reviewed after they closed. None of these are scoring
changes; every one is a gap between what the tool said and what could be
acted on.

- **"Close at 21 DTE" was the wrong rule, so it became a checkpoint.** GDX sat
  at 29.8% of its credit and SLV was in the same state, while DRAM was already
  past 66% and closed at 90%. A flat rule flags all three; the two that needed
  a decision were the two that had not paid their way. The check is now
  asymmetric and silent above the threshold, because a rule that interrupts
  working trades is a rule you stop reading.
- **Deadlines landed on days the market is shut.** `expiry − 21 days` is a
  Sunday two weeks in seven. Every deadline now resolves back to the previous
  session — back, never forward, since forward is already later than the rule
  asked for. NYSE holidays are generated from the rules rather than listed, so
  they stay correct without maintenance.
- **The sector cap could not see the biggest concentration in the book.** GDX
  and SLV are ETFs, carry no sector string, and so counted as two unrelated
  positions while being one bet on the gold price. Underlyings are now grouped
  by correlation as well, and capped by count and by share of the account.
- **The scan could not be graded.** Nothing recorded what was actually filled
  against what was suggested, so a review compared the model to trades that
  had drifted from it. `wheel_fills.py` closes that loop.
- **The earnings exclusion was off again, in a new way.** The cache was
  reachable only through the feed's freshness check, so `--offline-earnings`
  and any failed fetch produced an empty calendar — and an empty calendar
  excludes nothing. The cache is now read at any age, and the report says
  which source it used and how old it is. (This was not what hit GDX and SLV:
  neither is a company and neither reports. The correlation cap is the check
  that catches those two.)
- **Cheap stocks were still excluded, by a different gate.** The `$7,000` cash
  floor was fixed in version 2; the flat `$0.15` credit floor was not. It is
  the only screen denominated in dollars rather than percent, so it rejected
  an $8 stock at any plausible volatility while passing a $100 stock paying
  the identical 0.73% of strike. The floor is now relative, with `$0.05` kept
  as a spread-noise guard.

## Raising the price ceiling to $220

Done deliberately, and it moves four numbers at once because they are one
number wearing four hats:

| | was | now |
|---|---|---|
| `build_universe.py --max-price` | $150 | $220 |
| `--max-cash` | $15,000 | $22,000 |
| `max_capital_per_position_pct` | 15% | 22% |
| `max_open_positions` | 8 | 3 |
| `--max-symbols` | 250 | 400 |

Changing `--max-price` alone would have done nothing at all: `allow_single_
oversize` is off, so every strike above $150 fell straight into the `outside
cash sleeve` reject bucket. The scan would have fetched and priced those names
every run and dropped all of them.

Changing the sleeve without the per-position cap would have been worse than
nothing — the scanner would recommend $22,000 positions and
`wheel_positions.py --check` would refuse to size every one of them, the two
halves of the tool disagreeing on every expensive name.

What it costs: at $22,000 a position, a $100,000 account holds two full-size
positions plus a smaller third. That is the trade — a wider universe bought
with a narrower book. The 60% total-capital cap is unchanged and is the
backstop; a third full-size position trips it, which is the intended answer.

### `--max-symbols` had to move too

`--max-symbols` is a **budget, not a screen**: names compete for the slots,
ranked by dollar volume. So at 250 the wider price range did not widen the
universe — it *swapped* it. The first rebuild at $220 added 81 names and
evicted 81:

```
added    ACN BKNG CRWD HON RTX QCOM MS PLTR XOM TGT LOW ...
dropped  JOBY SMR CLSK CLF QXO RGTI QBTS DKNG TOST PINS ...
```

Expensive mega-caps out-rank cheap names on dollar volume every time, so the
cheap end of the book was paying for the expensive end — quietly undoing the
work that made cheap names reachable in the first place. Over 400 names clear
the $5–$220 price and volume screens, so 250 was the binding constraint, not
the price range.

**The default is now 400**, which admits the expensive end without evicting
the cheap end. The cost is scan time, which scales with the symbol count.
Lowering it again evicts the least liquid names first — which are the cheap
ones.

To go back, move all four together, or run
`python build_universe.py --max-price 150` with `--max-cash 15000`.

## Limitations

- **Open interest is unavailable** on the free Alpaca tier, so liquidity is
  gated on spread, quote size and volume instead. Do not read its absence as a
  pass.
- **Quotes outside market hours are the previous close.** Spreads look far
  wider than they trade, and the trade card labels this.
- **A cached earnings calendar can be stale.** When the Nasdaq feed does not
  answer, the last cached calendar is used at any age and the scan header says
  how old it is. Dates are published weeks ahead and rarely move by more than a
  day, but a report added since the cache was written is invisible.
- **Correlation groups are declared, not measured.** `wheelkit/correlation.py`
  is a hand-maintained table. It is deliberate — a rolling correlation matrix
  moves most in the crisis where the grouping is supposed to bind — but it
  means a pair that moves together and is not in the table will not be caught.
  Add names to it as you meet them.
- **The earnings calendar only covers scheduled reports.** A contract can carry
  a catalyst the calendar does not list — litigation, FDA, M&A. Implied
  volatility above 80% triggers a warning for exactly this reason; investigate
  before assuming rich premium is free money.
- **Probabilities are risk-neutral**, derived from the option's own implied
  volatility. They are not forecasts, and `P(profit)` reads higher than
  realised win rates because it ignores intra-period breaches you would close.
- **Black-Scholes assumes European exercise**; these are American options. The
  difference is immaterial for the short-dated out-of-the-money contracts the
  wheel sells, and material for deep in-the-money ones, which it does not.
- Dividends are not modelled. Early assignment risk on a short call rises
  sharply around an ex-dividend date.
- Always confirm the contract and the live quote in your broker before selling.
