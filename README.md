# Weekly Wheel Scan — IBKR manual scanner

A **read-only** Python scanner for cash-secured puts using market data from your running Interactive Brokers TWS or IB Gateway session. It never submits, modifies, or cancels orders.

## Your strategy encoded

- Cash required: **$7,000–$15,000** per contract
- Expiration: **7–14 calendar days**
- Absolute put delta: **0.15–0.20**
- Top **5**, or fewer when quality filters are not met
- No earnings before expiration (using `earnings.csv`)
- Underlying average daily volume at least 1,000,000 shares
- Option open interest at least 250
- Option volume at least 10
- Bid/ask spread no wider than 10% of midpoint
- Exclude stocks moving more than ±5% over the prior five sessions
- Output ranked by a 0–100 Wheel Score

## 1. IBKR setup

Install and open **Trader Workstation (TWS)** or **IB Gateway**.

In TWS: **File → Global Configuration → API → Settings**

- Enable **ActiveX and Socket Clients**
- Keep **Read-Only API** enabled (recommended)
- Note the socket port
  - TWS paper: usually `7497`
  - TWS live: usually `7496`
  - Gateway paper: usually `4002`
  - Gateway live: usually `4001`

You need the appropriate U.S. stock and options market-data subscriptions in IBKR for live bid/ask and Greeks. Without them, IBKR may return delayed/frozen data or blanks.

## 2. Install

```bash
cd weekly-wheel-scan
python3 -m venv .venv
source .venv/bin/activate       # macOS/Linux
# .venv\Scripts\activate        # Windows PowerShell
pip install -r requirements.txt
```

## 3. Update earnings dates

Before every scan, update `earnings.csv`:

```csv
symbol,earnings_date
AAPL,2026-08-06
AMD,2026-08-04
```

The script cannot guarantee the earnings exclusion when a symbol is absent from this file. ETFs generally do not have corporate earnings dates.

## 4. Run manually

Paper TWS:

```bash
python weekly_wheel_scan.py --port 7497 --symbols-file symbols.txt
```

Live TWS, still read-only:

```bash
python weekly_wheel_scan.py --port 7496 --symbols-file symbols.txt
```

Custom tickers:

```bash
python weekly_wheel_scan.py --symbols AAPL,AMD,QQQ,SPY,UBER --top 5
```

Results print in the terminal and save to `wheel_scan_results.csv`.

## Wheel Score

The score follows your weighting:

- 25% underlying quality/liquidity proxy
- 20% premium relative to risk
- 15% options liquidity
- 15% implied-volatility attractiveness
- 15% technical support
- 10% market conditions

The current script uses a neutral market-condition score because VIX access depends on your IBKR index data permissions. This is clearly isolated in `score_candidate()` so it can be upgraded later.

## Important limitations

- `earnings.csv` must be maintained manually for accurate event exclusion.
- Open interest and volume availability depends on IBKR permissions and the time of day.
- Snapshot values can change immediately after the scan.
- Assignment probability is not a guaranteed probability; absolute delta is only a rough market-implied proxy.
- Always verify the contract in TWS before submitting an order.
