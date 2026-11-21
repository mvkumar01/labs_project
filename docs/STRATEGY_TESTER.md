# Strategy Tester (`strategy_tester/`)

Natural-language backtester served at `/labs/backtest` (and `/labs/strategy-tester/`). Added to PA by another user
and committed here as uploaded on 2026-11-21, so later changes can be rolled back.

## How it is wired on PA

It does not modify any Labs file. The labs web app's WSGI file
(`/var/www/labs-mvkumar01_pythonanywhere_com_wsgi.py`, not in git: it holds secrets) installs it after the app import:

```python
try:
    from strategy_tester.flask_app import install as _st_install
    _st_install(application)
except Exception:
    import logging as _st_logging
    _st_logging.exception("Strategy Tester failed to load; original /labs/backtest kept")
```

`install()` registers the `/labs/strategy-tester/` blueprint (page, `api/status`, `api/strategies`, `api/query`,
`api/backtest`) and points the `labs.backtest_page` endpoint at the tester page. If the import fails the original
`labs/engine/backtest.py` page stays.

## Settings (environment)

- `ANTHROPIC_API_KEY` / `USE_CLAUDE=0` - optional Claude prompt parsing + commentary (off on PA: no key).
- `ST_DATA_PROVIDER=direct|yfinance`, `ST_CACHE_DIR` (default `strategy_tester/data_cache/`, gitignored).
- `ST_PARALLEL=1`, `ST_MAX_JOBS` - threads are off on PA web workers.

## Deploy caution

PA's labs checkout is deployed by uploading committed files. A `git stash --include-untracked` on PA would remove
any untracked copy of this package - deploy it as committed files like everything else.
