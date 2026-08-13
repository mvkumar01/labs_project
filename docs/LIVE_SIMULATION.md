# Labs Live Simulation

## Purpose

Live Simulation is a historical intraday replay terminal at:

`https://labs-mvkumar01.pythonanywhere.com/labs/simulation/`

It provides broker-style manual trading against historical one-minute candles. It
is simulation-only: no broker order endpoint is imported or called.

## Data architecture

- NIFTY reads the existing Labs/shared one-minute files. It does not create a
  second NIFTY collection pipeline.
- Equity candles use a separate Kite app, token, and cache under
  `data/simulation/1min/`.
- The separate private files are `config/simulation_kite.json` and
  `config/simulation_kite_token.json`. Both are gitignored.
- Five-minute, 15-minute, and one-hour candles are resampled from one-minute
  candles and anchored to the 09:15 IST market open.
- Replay slices the one-minute frame at the current simulated timestamp before
  resampling or calculating indicators. Future candles never reach the browser.

The configured equity universe uses the top five constituents from the NIFTY 50
and NIFTY Next 50 factsheets dated 2026-07-31. Rebalance updates belong in
`labs/simulation/config.py`.

## Separate Kite setup

Create this private file on PythonAnywhere:

```json
{
  "api_key": "SIMULATION_KITE_API_KEY",
  "api_secret": "SIMULATION_KITE_API_SECRET"
}
```

Save it as `/home/mvkumar01/labs_project/config/simulation_kite.json` with
owner-only permissions. Configure this redirect URL in the separate Kite app:

`https://labs-mvkumar01.pythonanywhere.com/labs/simulation/kite/callback`

Then open the simulator and click **Connect Kite Data**. The resulting access
token is written only to `config/simulation_kite_token.json`.

Kite access tokens expire daily. The supplied key file contains only the API key
and secret, so the simulator requires a daily interactive connection before it
can fetch an uncached equity date. Existing NIFTY data remains available without
this simulator token.

## PythonAnywhere processes

No additional always-on task is required. Replay and order matching are driven by
browser API requests and session state is persisted in `storage/simulation.db`.
The database uses SQLite rollback-journal mode for PythonAnywhere web workers.

If unattended daily token generation is later required, add full Kite login and
TOTP credentials to a separate private configuration and create a scheduled task.
Do not reuse the Labs collector credentials or token.

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
