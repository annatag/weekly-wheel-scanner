# Weekly Wheel Scan

Read-only tooling for selling cash-secured puts and covered calls. It ranks a
universe of tickers, then tells you which strike, which expiry and what limit
price to use. **It never places, modifies or cancels an order.**

```bash
python weekly_wheel_scan.py                 # rank the best puts to sell this week
python wheel_advise.py INTC                 # deep-dive one ticker: strike, expiry, price
python wheel_trade_suggestions.py           # re-quote a saved scan before you trade
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

mkdir -p ~/.config/wheelscan && chmod 700 ~/.config/wheelscan
cp .env.example ~/.config/wheelscan/.env
chmod 600 ~/.config/wheelscan/.env      # then paste your Alpaca keys into it
```

**Credentials live in the macOS Keychain, not on disk.**

```bash
python wheel_secrets.py store      # prompt and save, input never echoed
python wheel_secrets.py migrate    # move an existing .env in, then shred it
python wheel_secrets.py status     # show where credentials resolve from
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
subscription is required. `.env` is gitignored.

Run the offline test suite with `python -m unittest test_wheelkit`.

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

**At sizing** — contracts are scaled so a two-sigma adverse move stays inside
a risk budget, rather than filling a fixed cash sleeve. Filling a sleeve puts
the most contracts on the cheapest stock, which is usually the most volatile:
a $28 name took five contracts and a 10% week cost five times what one would
have.

**While open** — monitoring, silent unless something trips:

```bash
python wheel_positions.py                 # full table plus alerts
python wheel_positions.py --alerts-only   # for a scheduled job
```

Alerts on: a position going in the money, delta past 0.50, 50% of max profit
captured, the last three days before expiry at a live delta, and a daily move
over 5% in the underlying.

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

`--source ibkr` refuses to fall back at all and errors instead, which is what
you want when checking whether TWS is actually reachable.

### Running it on a schedule

```bash
./scripts/install_monitor.sh
```

Weekdays at 15:00 (an hour left to act) and 16:15 (after the close, marks
settled). Alerts only, so a quiet day produces no output. Remove it with the
command the installer prints.

## The four commands

### `build_universe.py` — what to scan

Pulls every optionable US equity from Alpaca, screens on price and real
consolidated dollar volume, drops leveraged and inverse products, and writes
the survivors to `universe/symbols.txt`. Run it weekly or monthly, not daily.

**You do not have to run it first.** The scanner builds the universe itself
when the file is missing. Run this directly only to rebuild on demand or to
change the screen.

```bash
python build_universe.py                            # top 250 by liquidity
python build_universe.py --dry-run                  # preview without writing
python build_universe.py --max-price 250 --max-symbols 400
```

`--max-price` is your per-position cash ceiling divided by 100, since one
contract secures 100 shares. Leave it aligned with the scanner's `--max-cash`
or the scan will keep rejecting names the universe just admitted.

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

### `wheel_trade_suggestions.py` — is it still good?

Re-quotes the saved scan against the current market and reprices the limits.
Option quotes move far more than the underlying, so run this immediately before
trading. Contracts that no longer pass the spread check are marked `WAIT`, and
the original position size is preserved.

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

| Weight | Component | What it measures |
|--------|-----------|------------------|
| 25% | Premium | Annualised return **in excess of the risk-free rate** |
| 25% | IV edge | Variance risk premium (implied ÷ realised) |
| 20% | Safety | Cushion in σ, trend, distance from the ideal delta |
| 15% | Liquidity | Bid/ask spread, quote size, option volume |
| 10% | Quality | Average dollar volume, log-scaled |
| 5%  | Regime | SPY volatility and trend |

Adjust the weights in `WheelConfig.weights` in `wheelkit/strategy.py`.

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

## Limitations

- **Open interest is unavailable** on the free Alpaca tier, so liquidity is
  gated on spread, quote size and volume instead. Do not read its absence as a
  pass.
- **Quotes outside market hours are the previous close.** Spreads look far
  wider than they trade, and the trade card labels this.
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
