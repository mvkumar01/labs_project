# Labs Live Simulation

## Purpose

Live Simulation is a historical intraday replay terminal at:

`https://labs-mvkumar01.pythonanywhere.com/labs/simulation/`

It provides broker-style manual trading against historical one-minute candles. It
is simulation-only: no broker order endpoint is imported or called.

The same page also has a **Live Paper** mode. While the browser remains open, it
polls the server every 15 seconds and processes each newly completed one-minute
candle. It places simulated orders only and does not require an always-on task.

## Data architecture

- NIFTY reads the existing Labs/shared one-minute files. It does not create a
  second NIFTY collection pipeline.
- Equity candles use the rotating alphaIMB Kite token read directly from
  `~/alphaIMB/zerodha_access_token.json` and a separate cache under
  `data/simulation/1min/`. The token is never copied into Labs.
- Five-minute, 15-minute, and one-hour candles are resampled from one-minute
  candles and anchored to the 09:15 IST market open.
- Replay slices the one-minute frame at the current simulated timestamp before
  resampling or calculating indicators. Future candles never reach the browser.

The configured equity universe uses the top five constituents from the NIFTY 50
and NIFTY Next 50 factsheets dated 2026-07-31. Rebalance updates belong in
`labs/simulation/config.py`.

## Separate Kite setup

The normal PythonAnywhere data path needs no duplicate credentials. It reads:

`/home/mvkumar01/alphaIMB/zerodha_access_token.json`

The file must contain both `api_key` and `access_token`. There is no Kite login
or OAuth action in the simulator UI; alphaIMB remains the single owner of token
refresh.

Kite access tokens expire daily. The simulator automatically uses the alphaIMB
file after its existing daily refresh. If that refresh fails, uncached equity
fetches fail clearly; existing NIFTY data and cached equity dates remain usable.

## PythonAnywhere processes

No additional always-on task is required. Replay and order matching are driven by
browser API requests and session state is persisted in `storage/simulation.db`.
The database uses SQLite rollback-journal mode for PythonAnywhere web workers.

If unattended daily token generation is later required, add full Kite login and
TOTP credentials to a separate private configuration and create a scheduled task.
Do not reuse the Labs collector credentials or token.

Live Paper intentionally stops when the browser is closed or suspended. When the
page resumes after a gap longer than 90 seconds, missed candles are skipped rather
than executing orders retroactively. Keep the page open and the computer awake
for continuous paper execution. At 15:30 IST, the next successful poll cancels
pending orders and closes simulated positions using the latest completed bar.

After deployment, reload the existing PythonAnywhere web app. Do not restart the
Labs collector, paper strategy runner, or live trading runner for this feature.

## Execution assumptions

- Market orders fill at the latest visible candle close plus configured slippage.
- Limit and stop orders use visible OHLC and gap-aware deterministic fills.
- If stop-loss and target are both touched in one candle, stop-loss wins. This is
  intentionally conservative because tick order is unknowable from OHLC data.
- Closing one position removes its protective stop and target, providing OCO
  behavior.
- Charges are an explicit approximate equity intraday model and are displayed
  separately from gross and net P&L.
- End of replay cancels pending orders and closes open positions.

Partial position exits are supported. Exchange-style partial order fills and
dragging SL/target lines directly on the chart are deferred enhancements.

## Verification

```bash
python3 -m py_compile app.py labs/ui/simulation_routes.py labs/simulation/*.py
python3 -m pytest -q tests/test_simulation_engine.py
python3 tests/test_live_isolation.py
node --check static/simulation.js
```
