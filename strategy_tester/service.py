"""Framework-agnostic request handling, shared by the FastAPI app (local) and the
Flask blueprint (PythonAnywhere / Labs)."""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import advisor, data, engine, llm, parser
from .strategies import STRATEGIES, catalog

log = logging.getLogger("strategy-tester")

# Threads are not available in some WSGI hosts (e.g. PythonAnywhere web apps); run sequentially there.
PARALLEL = os.environ.get("ST_PARALLEL", "0") == "1"
_pool = ThreadPoolExecutor(max_workers=6) if PARALLEL else None
MAX_JOBS = int(os.environ.get("ST_MAX_JOBS", "24"))


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def status() -> dict:
    return {"claude": llm.status(), "strategies": len(STRATEGIES), "data_provider": data.PROVIDER}


def strategies() -> list[dict]:
    return catalog()


def _map(fn, items):
    if _pool is None:
        out = []
        for it in items:
            try:
                out.append((it, fn(it), None))
            except Exception as e:  # keep the other runs
                out.append((it, None, e))
        return out
    futures = [(it, _pool.submit(fn, it)) for it in items]
    res = []
    for it, f in futures:
        try:
            res.append((it, f.result(), None))
        except Exception as e:
            res.append((it, None, e))
    return res


def _resolve_symbols(spec: dict) -> list[str]:
    out = []
    for s in spec.get("symbols") or []:
        s = (s or "").strip()
        if not s:
            continue
        alias = data.ALL_ALIASES.get(s.lower())
        out.append(alias or s.upper())
    return list(dict.fromkeys(out))


def _cfg(spec: dict) -> engine.BacktestConfig:
    c = spec.get("config") or {}
    allowed = engine.BacktestConfig.__dataclass_fields__.keys()
    return engine.BacktestConfig(**{k: v for k, v in c.items() if k in allowed and v is not None})


def _thin(points: list, max_points: int = 800) -> list:
    """Evenly subsample a chart series, always keeping the last point."""
    n = len(points)
    if n <= max_points:
        return points
    step = n / max_points
    out = [points[int(i * step)] for i in range(max_points)]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def _compact(r: dict) -> dict:
    """Small version of a result for the LLM commentary (no series)."""
    keep = {k: v for k, v in r.items() if k not in ("series", "markers", "trades", "monthly", "benchmark_monthly", "strategy_curves")}
    if "trades" in r:
        keep["recent_trades"] = r["trades"][-8:]
    return keep


def query(prompt: str) -> dict:
    prompt = (prompt or "").strip()
    if not prompt:
        raise ServiceError(400, "Type a request first.")
    if len(prompt) > 600:
        raise ServiceError(400, "That request is too long. Keep it under 600 characters.")
    t0 = time.time()
    spec = llm.parse(prompt) or parser.parse(prompt)
    if spec.get("parser") == "claude":
        rb = parser.parse(prompt)  # fill gaps Claude may have left
        if not spec.get("symbols"):
            spec["symbols"] = rb["symbols"]
        if not spec.get("start") and spec.get("intent") != "analyze":
            spec["start"] = rb["start"]
    symbols = _resolve_symbols(spec)
    if not symbols:
        raise ServiceError(422, "I couldn't tell which instrument you mean. Try a ticker like AAPL, RELIANCE.NS, "
                                "GC=F (gold), EURUSD or ^NSEI (Nifty 50).")
    spec["symbols"] = symbols
    intent = spec.get("intent", "backtest")
    cfg = _cfg(spec)
    if cfg.stop_loss_pct or cfg.trailing_stop_pct or cfg.take_profit_pct:
        spec.setdefault("notes", []).append(
            "After a stop or target closes a trade, the strategy waits for a fresh entry signal before re-entering.")
    response: dict = {"prompt": prompt, "spec": spec, "intent": intent}

    try:
        if intent == "analyze":
            done = _map(advisor.analyze, symbols[:3])
            results = [r for _, r, e in done if r is not None]
            errors = [f"{s}: {e}" for s, _, e in done if e is not None]
            if not results:
                raise ValueError("; ".join(errors) or "No results")
            response["results"] = results
            response["errors"] = errors
            response["commentary"] = llm.commentary(prompt, {"analyses": [_compact(r) for r in results]})
        else:
            strats = spec.get("strategies") or [{"key": "sma_cross", "params": {}}]
            jobs = [(s, st) for s in symbols for st in strats][:MAX_JOBS]
            single = len(jobs) == 1

            def run(job):
                sym, st = job
                return engine.run_backtest(sym, st["key"], st.get("params"), spec.get("start"), spec.get("end"),
                                           spec.get("interval", "1d"), cfg, robustness=single, detail=True)
            done = _map(run, jobs)
            results = [r for _, r, e in done if r is not None]
            errors = [f"{j[0]} / {j[1]['key']}: {e}" for j, _, e in done if e is not None]
            if not results:
                raise ValueError("; ".join(errors) or "No results")
            if not single:
                results.sort(key=lambda r: (r["metrics"].get("sharpe") or -99), reverse=True)
                one_symbol = len({r["symbol"] for r in results}) == 1
                for i, r in enumerate(results):
                    # The comparison view only draws equity curves (drilling into a row refetches
                    # the full report), so drop candles/overlays/trades and thin the curves.
                    s = r.get("series", {})
                    r["series"] = {"equity": _thin(s.get("equity", []))}
                    if i == 0 and one_symbol and s.get("benchmark"):
                        r["series"]["benchmark"] = _thin(s["benchmark"])
                    for k in ("monthly", "benchmark_monthly", "markers", "trades", "robustness"):
                        r.pop(k, None)
                response["intent"] = "compare"
            response["results"] = results
            response["errors"] = errors
            if spec.get("evaluate") or single:
                response["commentary"] = llm.commentary(prompt, {"results": [_compact(r) for r in results[:8]]})
    except ValueError as e:
        raise ServiceError(422, str(e))
    response["elapsed"] = round(time.time() - t0, 2)
    log.info("query '%s' -> %s %s in %.2fs (%s)", prompt[:80], intent, symbols, response["elapsed"], spec.get("parser"))
    return response


def detail(req: dict) -> dict:
    """Full single backtest (used when drilling into a row of a comparison)."""
    sym, strat = (req.get("symbol") or "").strip(), req.get("strategy")
    if not sym or strat not in STRATEGIES:
        raise ServiceError(400, "symbol and a valid strategy are required.")
    try:
        cfg = _cfg({"config": req.get("config") or {}})
        r = engine.run_backtest(sym, strat, req.get("params"), req.get("start"), req.get("end"),
                                req.get("interval") or "1d", cfg)
    except ValueError as e:
        raise ServiceError(422, str(e))
    return {"intent": "backtest", "results": [r], "spec": {
        "symbols": [sym], "strategies": [{"key": strat, "params": req.get("params") or {}}],
        "start": req.get("start"), "end": req.get("end"), "interval": req.get("interval") or "1d",
        "config": req.get("config") or {}, "notes": [], "parser": "direct"}}
