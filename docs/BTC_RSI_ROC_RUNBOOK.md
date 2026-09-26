# BTC RSI/ROC short — paper book runbook

Paper only. Never places orders.

**Rule** (Strategy Tester `btcusdt/ui_20260923_162834_bfa8`, rank 409, strategy `f6041879c2acf239`):
`rsi(n=7)@1h x> 30 & roc(n=10)@15m x> 0 | gate rsi(n=14)@5m in [40,60]`, short 0.01 BTC,
stop 1% of entry, target 1.5R, no time stop, max hold 1,440 bars, 30-bar cooldown.

**Money:** Binance spot 0.1% of each leg's value + 1 tick (0.01 USDT) slippage per side, in USDT.
Rupees use the ECB USD/INR reference rate (frankfurter.dev) on the exit date (UTC); capital uses
the entry date. Weekends and holidays take the last published rate. USDT is treated as USD.

**Parity:** `research/experiments/2026-09-26_btc_rsi_roc_parity/parity.py` replays the Tester's
own cached data and reproduces all 40 rank-409 research trades exactly. Money matches the
Tester's 0.01 BTC what-if to within half a paisa per trade.

## Where things live

| What | Where |
|---|---|
| Engine, data, ledger | `labs/engine/btc_rsi_roc_tracker.py` |
| Bounded backfill (UI button) | `labs/engine/btc_rsi_roc_backfill.py` |
| 1-min klines (UTC) | table `btc_minute_bars` |
| USD/INR rates | table `fx_usd_inr` |
| Ledger | `btc_rsi_roc_daily` (one row per UTC day), `btc_rsi_roc_trades` |
| Tab | `/labs/live?tab=btc_rsi_roc` |
| Loop | `pa_paper_tracker_loop.py` runs it every ~60 s, 24/7 |

## Behaviour

- **Fixed replay window:** every run replays from 2026-05-02 (30 days of warm-up before the
  book start, 2026-06-01). With one position at a time, the replay start decides which signals
  are taken, so it never moves and the ledger is deterministic.
- **What each run does:** fetches the closed candles it is missing, replays and rewrites the
  ledger from 2026-06-01.
- **Open positions:** marked at the last completed 1-minute close.
- **First run:** fetches about 215 pages of 1,000 minutes (about a minute). Later runs take
  about 2 s.

## Data source

Binance public REST API, no key.

- **Endpoints:** `api.binance.com` is tried first, then `data-api.binance.vision`. The first
  refuses some regions with HTTP 451; the second serves the same market data.
- **If both fail:** the loop logs `[paper-loop:btc_rsi_roc] error: BtcInputError: …` and the
  tab keeps its last state.

Check reachability from a PythonAnywhere console:

```bash
curl -s "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1"; echo
curl -s -o /dev/null -w "%{http_code}\n" "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1"
curl -s "https://api.frankfurter.dev/v1/latest?base=USD&symbols=INR"; echo
```

## Backfill or refresh by hand

```bash
cd ~/labs_project && python3 -m labs.engine.btc_rsi_roc_backfill
```
