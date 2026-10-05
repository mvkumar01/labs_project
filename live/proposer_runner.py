"""SENSEX Proposer live runner - its own always-on PA task (pa_proposer_runner.py).

Runs only connections whose strategy_version is the Proposer (live_runner skips them), and
reuses everything that protects real money in the NIFTY path: live_executor's mode machine,
gates, idempotent order ledger and pre-SELL long check; live_service trade state, P&L buckets
and daily-loss halt; the broker adapters (switched to BFO/SENSEX per connection).

Per cycle (1 s while a position is open, 2 s flat):
  1. the Predictor feed publishes a print every 5 min from the collector's SENSEX bars
     (drift + 5-class, live/engine/proposer_predictor.py; regime from the opening gap);
  2. each Proposer connection evaluates live/engine/proposer_engine.py on the latest print,
     the live SENSEX spot and the held option's live price (labs Kite data session);
  3. ENTER -> IOC BUY of ATM +/- 200 ITM, nearest weekly, priced from the ask after the gates;
     EXIT  -> marketable DAY SELL; 15:25 IST flat.
DRY_RUN never reaches a broker order call (live_executor short-circuits it).
"""
from __future__ import annotations

import io
import json
import logging
import os
import tarfile
import threading
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pandas as pd

from config.labs_config import ARCHIVE_DIR, DATA_DIR, SHARED_LIVE_DIR
from storage.live_db import get_live_conn, init_live_db
from live import live_executor as ex
from live import live_runner as lr
from live import live_service as svc
from live.engine import proposer_engine as pe
from live.engine import proposer_predictor as pp
from live.notify import notify_telegram
from market_data.expiry import expiry_code_from_symbol, select_expiry_code

log = logging.getLogger("live.proposer_runner")

UNDERLYING, SEGMENT, LOT_SIZE = "SENSEX", "BFO", 20
POLL_OPEN_S, POLL_FLAT_S = 1.0, 2.0
PRINT_EVERY_MIN = 5
ENTRY_CUTOFF = dtime(15, 20)
EOD_FLAT = dtime(15, 25)
STARTUP_ACT_SECS = 60          # a print older than this at boot is history, not a signal
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


# ═══════════════════════════════════════════════════════════════ prints table ══
def ensure_schema(conn=None) -> None:
    own = conn is None
    conn = conn or get_live_conn()
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS live_proposer_prints ("
                " asof TEXT PRIMARY KEY, trade_date TEXT NOT NULL, regime TEXT, regime_conf REAL,"
                " x5 TEXT, x5_conf REAL, probs_json TEXT, drift_state TEXT, drift_dist REAL,"
                " drift_slope REAL, source TEXT, created_at TEXT)")
    finally:
        if own:
            conn.close()


def ensure_regime_schema(conn=None) -> None:
    own = conn is None
    conn = conn or get_live_conn()
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS live_proposer_regimes ("
                " trade_date TEXT NOT NULL, source TEXT NOT NULL, regime TEXT, p_bull REAL,"
                " p_bear REAL, p_chop REAL, confidence REAL, trigger TEXT, payload_json TEXT,"
                " created_at TEXT, PRIMARY KEY (trade_date, source))")
    finally:
        if own:
            conn.close()


def save_regime(trade_date: str, source: str, out: dict, conn=None) -> None:
    own = conn is None
    conn = conn or get_live_conn()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO live_proposer_regimes VALUES (?,?,?,?,?,?,?,?,?,?)",
                (trade_date, source, out.get("regime"), out.get("p_bull"), out.get("p_bear"),
                 out.get("p_chop"), out.get("confidence"), out.get("trigger"),
                 json.dumps(out, default=str), datetime.now(timezone.utc).isoformat()))
    finally:
        if own:
            conn.close()


def get_regime(trade_date: str, source: str, conn=None) -> dict | None:
    own = conn is None
    conn = conn or get_live_conn()
    try:
        row = conn.execute("SELECT payload_json FROM live_proposer_regimes WHERE trade_date=? AND source=?",
                           (trade_date, source)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        if own:
            conn.close()


def regime_source() -> str:
    """Which daily regime drives entries: 'gap' (default) or 'deepseek' (falls back to gap)."""
    return (os.environ.get("PROPOSER_REGIME_SOURCE") or "gap").strip().lower()


def save_print(p: dict, conn=None) -> None:
    own = conn is None
    conn = conn or get_live_conn()
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO live_proposer_prints VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["x5_asof"], p["trade_date"], p["regime"], p.get("regime_conf"), p["x5"],
                 p["x5_conf"], json.dumps(p.get("probs5") or {}), p.get("drift_state"),
                 p.get("drift_dist"), p.get("drift_slope"), p.get("source", "proposer_rules_v1"),
                 datetime.now(timezone.utc).isoformat()))
    finally:
        if own:
            conn.close()


def prints_today(trade_date: str, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or get_live_conn()
    try:
        rows = conn.execute("SELECT * FROM live_proposer_prints WHERE trade_date=? ORDER BY asof",
                            (trade_date,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


# ═════════════════════════════════════════════════════════════ market reads ══
class KiteMarket:
    """Read-only SENSEX market data from the labs Kite data session (never the broker)."""

    def _kite(self):
        from auth.session_manager import get_kite
        return get_kite()

    def spot(self) -> float | None:
        try:
            return float(self._kite().ltp("BSE:SENSEX")["BSE:SENSEX"]["last_price"])
        except Exception as e:
            log.warning("SENSEX spot read failed: %s", type(e).__name__)
            return None

    def quote(self, symbol: str) -> dict | None:
        """{'ltp', 'bid', 'ask'} for a SENSEX option, or None."""
        key = f"{SEGMENT}:{symbol}"
        try:
            q = self._kite().quote(key)[key]
        except Exception as e:
            log.warning("quote failed %s: %s", symbol, type(e).__name__)
            return None
        depth = q.get("depth") or {}
        bids = [float(x.get("price") or 0) for x in depth.get("buy") or [] if float(x.get("price") or 0) > 0]
        asks = [float(x.get("price") or 0) for x in depth.get("sell") or [] if float(x.get("price") or 0) > 0]
        return {"ltp": float(q.get("last_price") or 0) or None,
                "bid": max(bids) if bids else None, "ask": min(asks) if asks else None}


# ════════════════════════════════════════════════════════════ predictor feed ══
def _read_bars(path: Path) -> list[tuple[datetime, float]]:
    f = pd.read_csv(path)
    f["ts"] = pd.to_datetime(f["timestamp"].astype(str).str.slice(0, 19))
    f = f.drop_duplicates("ts").sort_values("ts")
    return [(t.to_pydatetime(), float(c)) for t, c in zip(f["ts"], f["close"])]


def spot_bars(day: date) -> list[tuple[datetime, float]]:
    """Collector SENSEX 1-min bars for a session: live dir first, then the day's archive."""
    live = DATA_DIR / f"{day.isoformat()}_{UNDERLYING}_spot_1min.csv"
    if live.is_file():
        return _read_bars(live)
    tar = ARCHIVE_DIR / f"{day.isoformat()}.tar.gz"
    if tar.is_file():
        with tarfile.open(tar) as tf:
            m = next((x for x in tf.getmembers() if x.name.endswith(f"_{UNDERLYING}_spot_1min.csv")), None)
            if m is not None:
                tmp = pd.read_csv(io.BytesIO(tf.extractfile(m).read()))
                tmp["ts"] = pd.to_datetime(tmp["timestamp"].astype(str).str.slice(0, 19))
                tmp = tmp.drop_duplicates("ts").sort_values("ts")
                return [(t.to_pydatetime(), float(c)) for t, c in zip(tmp["ts"], tmp["close"])]
    return []


def option_chain(day: date) -> pd.DataFrame:
    """Latest minute of today's collector SENSEX option chain (nearest expiry rows only)."""
    path = SHARED_LIVE_DIR / day.isoformat() / f"{UNDERLYING}_options_1min.csv"
    if not path.is_file():
        return pd.DataFrame()
    f = pd.read_csv(path, usecols=["timestamp", "tradingsymbol", "strike", "option_type",
                                   "expiry", "oi", "spot"])
    if f.empty:
        return f
    f = f[f["timestamp"] == f["timestamp"].max()].copy()
    f["expiry"] = f["expiry"].astype(str).str.upper()
    code = select_expiry_code(f["expiry"].unique(), day, "nearest_weekly")
    return f[f["expiry"] == code] if code else pd.DataFrame()


def recent_day_closes(today: date, bars_fn=spot_bars, n: int = 60) -> list[tuple[float, float]]:
    """(first close, last close) of the last n sessions before today, for the regime base rates."""
    days, d = [], today - timedelta(days=1)
    while len(days) < n and d > today - timedelta(days=150):
        bars = bars_fn(d) if d.weekday() < 5 else []
        if bars:
            days.append((bars[0][1], bars[-1][1]))
        d -= timedelta(days=1)
    return days


class LlmRegimeJob:
    """Scores today's regime from overnight news with DeepSeek, once per weekday from 09:00.

    Runs in a background thread so a slow feed or API never stalls the trading loop; retries every
    2 minutes until 09:14. The result is stored in live_proposer_regimes (source 'deepseek'); it only
    drives trading when PROPOSER_REGIME_SOURCE=deepseek (shadow otherwise)."""
    START, LAST_TRY, RETRY_S = dtime(9, 0), dtime(9, 14), 120

    def __init__(self, fetch=None, score=None, bars_fn=spot_bars, store=save_regime,
                 lookup=get_regime, spawn: bool = True):
        from live.engine import proposer_regime_llm as rl
        self.fetch, self.score = fetch or rl.fetch_news, score or rl.daily_regime
        self.bars_fn, self.store, self.lookup, self.spawn = bars_fn, store, lookup, spawn
        self.done: set[str] = set()
        self.running = False
        self.last_try: datetime | None = None
        self._warned = False

    def step(self, now: datetime) -> None:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            if not self._warned:
                log.info("DEEPSEEK_API_KEY not set - LLM regime disabled (gap rule only)")
                self._warned = True
            return
        if now.weekday() >= 5 or not (self.START <= now.time() <= self.LAST_TRY):
            return
        day = now.date().isoformat()
        if day in self.done or self.running:
            return
        if self.last_try is not None and (now - self.last_try).total_seconds() < self.RETRY_S:
            return
        if self.lookup(day, "deepseek"):
            self.done.add(day)
            return
        self.last_try, self.running = now, True
        if self.spawn:
            threading.Thread(target=self.run_now, args=(now,), daemon=True, name="proposer-regime").start()
        else:
            self.run_now(now)

    def run_now(self, now: datetime) -> dict | None:
        day = now.date().isoformat()
        try:
            items, skipped = self.fetch(now=now)
            from live.engine import proposer_regime_llm as rl
            br = rl.base_rates_from_days(recent_day_closes(now.date(), self.bars_fn))
            out = self.score(items, br)
            out.update(_n_headlines=len(items), _skipped_feeds=skipped, _base_rates=br,
                       _headlines=[it.get("title") for it in items[:15]])
            self.store(day, "deepseek", out)
            self.done.add(day)
            log.info("deepseek regime %s (bull %.2f bear %.2f chop %.2f conf %.2f) - %s",
                     out.get("regime"), *(float(out.get(k) or 0) for k in ("p_bull", "p_bear", "p_chop", "confidence")),
                     out.get("trigger"))
            return out
        except Exception as e:
            log.warning("deepseek regime failed (%s): %s", type(e).__name__, str(e)[:160])
            return None
        finally:
            self.running = False


class PredictorFeed:
    """Publishes Proposer prints every PRINT_EVERY_MIN minutes (first once 7 bars of today exist)."""

    def __init__(self, bars_fn=spot_bars, chain_fn=option_chain, store=save_print,
                 store_regime=save_regime, lookup_regime=get_regime):
        self.bars_fn, self.chain_fn, self.store = bars_fn, chain_fn, store
        self.store_regime, self.lookup_regime = store_regime, lookup_regime
        self.day: date | None = None
        self.prior: list = []
        self.regime: pp.Regime | None = None
        self.latest: dict = {}
        self.next_due: datetime | None = None

    def _new_day(self, day: date) -> None:
        self.day, self.regime, self.latest, self.next_due = day, None, {}, None
        self.prior = []
        for back in range(1, 11):
            bars = self.bars_fn(day - timedelta(days=back))
            if bars:
                self.prior = bars
                break
        for row in prints_today(day.isoformat()):
            self.latest = {"regime": row["regime"], "x5": row["x5"], "x5_conf": row["x5_conf"],
                           "x5_asof": row["asof"], "drift_state": row["drift_state"]}

    def snapshot(self) -> dict:
        return dict(self.latest)

    def step(self, now: datetime) -> dict | None:
        """Publish a print when due. Returns the new print, else None."""
        if self.day != now.date():
            self._new_day(now.date())
        if self.next_due is not None and now < self.next_due:
            return None
        today = [b for b in self.bars_fn(now.date()) if b[0] < now.replace(second=0, microsecond=0)]
        if not today:
            return None
        if self.regime is None:
            prev_close = self.prior[-1][1] if self.prior else None
            first_open = self._first_open(now.date()) or today[0][1]
            gap = pp.regime_from_gap(prev_close, first_open)
            day = now.date().isoformat()
            self.store_regime(day, "gap_rule", {
                "regime": gap.label, "p_bull": gap.p_bull, "p_bear": gap.p_bear, "p_chop": gap.p_chop,
                "confidence": gap.confidence, "trigger": f"open {first_open} vs prev close {prev_close}"})
            self.regime = gap
            if regime_source() == "deepseek":
                from live.engine import proposer_regime_llm as rl
                out = self.lookup_regime(day, "deepseek")
                if out:
                    self.regime = rl.to_regime(out)
                else:
                    log.warning("PROPOSER_REGIME_SOURCE=deepseek but no DeepSeek regime today - using the gap rule")
            log.info("regime %s (conf %.2f, source %s); gap rule says %s (prev %s open %s)",
                     self.regime.label, self.regime.confidence, self.regime.source, gap.label,
                     prev_close, first_open)
        chain_rows = self.chain_fn(now.date())
        chain = None
        if not chain_rows.empty:
            piv = chain_rows.pivot_table(index="strike", columns="option_type", values="oi",
                                         aggfunc="last").fillna(0)
            chain = pp.chain_features(
                [{"strike": float(k), "ce_oi": float(v.get("CE", 0)), "pe_oi": float(v.get("PE", 0))}
                 for k, v in piv.iterrows()], float(chain_rows["spot"].iloc[-1]))
        x = pp.predict_x5(self.prior, today, chain, self.regime)
        if not x:
            return None
        asof = now.replace(microsecond=0)
        p = {"trade_date": now.date().isoformat(), "x5_asof": asof.isoformat(),
             "regime": self.regime.label, "regime_conf": self.regime.confidence,
             "x5": x["x5"], "x5_conf": x["x5_conf"], "probs5": x["probs5"],
             "drift_state": pp.drift_state(x["micro"]), "drift_dist": x["micro"].get("dist_pct"),
             "drift_slope": x["micro"].get("slope_pct")}
        self.store(p)
        self.latest = {k: p[k] for k in ("regime", "x5", "x5_conf", "x5_asof", "drift_state")}
        self.next_due = asof + timedelta(minutes=PRINT_EVERY_MIN)
        log.info("print %s regime=%s x5=%s conf=%.2f drift=%s", asof.time(), p["regime"],
                 p["x5"], p["x5_conf"], p["drift_state"])
        return p

    def _first_open(self, day: date) -> float | None:
        path = DATA_DIR / f"{day.isoformat()}_{UNDERLYING}_spot_1min.csv"
        try:
            f = pd.read_csv(path, nrows=3)
            return float(f["open"].iloc[0])
        except Exception:
            return None


# ═══════════════════════════════════════════════════════ per-connection logic ══
def itm_symbol(chain: pd.DataFrame, side: str, spot: float) -> tuple[str | None, int, str]:
    strike, typ = pe.itm_strike(spot, side)
    if chain.empty:
        return None, strike, typ
    hit = chain[(chain["strike"].astype(float) == strike) & (chain["option_type"] == typ)]
    return (str(hit["tradingsymbol"].iloc[0]) if len(hit) else None), strike, typ


def _today_trades(user_id: str, conn_id: str, trade_date: str, dry_run: bool) -> list[dict]:
    with get_live_conn() as c:
        rows = c.execute(
            "SELECT * FROM live_trades WHERE user_id=? AND conn_id=? AND dry_run=? "
            "AND strategy LIKE 'proposer%' ORDER BY exit_time", (user_id, conn_id, int(dry_run))).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["_ist_date"] = svc._parse_iso_to_ist_date(r.get("exit_time"))
    return [r for r in out if r["_ist_date"] == trade_date], out


class ProposerConnection:
    """Engine + restart-safe state for one (user, connection)."""

    def __init__(self, user_id: str, conn_id: str, params: pe.ProposerParams | None = None):
        self.user_id, self.conn_id = user_id, conn_id
        self.engine = pe.ProposerEngine(params or pe.ProposerParams())
        self.day: str | None = None
        self.mode: str | None = None

    def ensure_session(self, trade_date: str, dry_run: bool, feed: PredictorFeed, now: datetime) -> None:
        mode = "dry" if dry_run else "live"
        if self.day == trade_date and self.mode == mode:
            return
        self.engine.reset_session()
        today, _ = _today_trades(self.user_id, self.conn_id, trade_date, dry_run)
        prints = prints_today(trade_date)
        regime = prints[-1]["regime"] if prints else None
        stale_asof = None
        if prints:
            last = datetime.fromisoformat(prints[-1]["asof"])
            if (now - last).total_seconds() > STARTUP_ACT_SECS:
                stale_asof = prints[-1]["asof"]
        licence_key = "proposer_regime_entry_date_" + mode
        self.engine.restore(
            regime=regime, x5_today=[p["x5"] for p in prints],
            regime_entry_used=svc.get_config(self.user_id, self.conn_id, licence_key) == trade_date,
            day_target_banked=any(t.get("reason") == "daily_target" for t in today),
            last_entry_capital=(float(today[-1]["entry_price"]) * int(today[-1]["qty"])) if today else 0.0,
            last_acted_asof=stale_asof)
        self.day, self.mode = trade_date, mode

    def book(self, trade_date: str, dry_run: bool) -> tuple[float, float]:
        today, all_rows = _today_trades(self.user_id, self.conn_id, trade_date, dry_run)
        day_gross = sum(float(t.get("gross_pnl") or 0) for t in today)
        book_gross = sum(float(t.get("gross_pnl") or 0) for t in all_rows)
        return day_gross, book_gross


def publish_heartbeat(user_id: str, conn_id: str, task_id: str) -> bool:
    if not ex.is_proposer_strategy(svc.get_config(user_id, conn_id, "strategy_version")):
        return False
    if not lr.claim_runner_owner(user_id, conn_id, task_id):
        return False
    svc.set_config(user_id, conn_id, "runner_decision_abi", ex.PROPOSER_DECISION_ABI)
    svc.set_config(user_id, conn_id, "runner_decision_owner", task_id)
    return True


def _adapter(user_id, conn_id, adapters, adapter_factory):
    a = lr._ensure_connected_adapter(user_id, conn_id, adapters=adapters, adapter_factory=adapter_factory)
    if a is not None and getattr(a, "_proposer_segment", None) != SEGMENT:
        if hasattr(a, "use_segment"):
            a.use_segment(SEGMENT, UNDERLYING)
        a._proposer_segment = SEGMENT
    return a


def _reconcile(adapter, user_id: str, conn_id: str) -> bool:
    """Block entries when the broker book and the DB disagree (exits stay allowed)."""
    st = svc.get_trade_state(user_id, conn_id)
    db_open = st.get("position") == "OPEN"
    try:
        pos = adapter.get_position()
    except Exception as e:
        msg = f"broker position read failed: {type(e).__name__}"
        ok = False
    else:
        if not db_open and int(pos.qty or 0) == 0:
            ok, msg = True, "both flat"
        elif db_open and pos.symbol == st.get("symbol") and int(pos.qty or 0) == int(st.get("qty") or 0):
            ok, msg = True, "both open, agree"
        else:
            ok = False
            msg = (f"MISMATCH db={st.get('symbol')}/{st.get('qty') if db_open else 0} "
                   f"broker={pos.symbol}/{pos.qty} - new entries blocked")
    svc.set_config(user_id, conn_id, "reconcile_blocked", "0" if ok else "1")
    svc.set_config(user_id, conn_id, "reconcile_message", "" if ok else msg)
    if not ok:
        notify_telegram(f"Proposer automation blocked: {msg}")
    return ok


def _route(adapter, user_id, conn_id, *, action, side, symbol, qty, price, dry_run,
           key_ts: str, entry_rule="none", price_fn=None):
    trade_date = svc.today_ist_iso()
    strategy_version = svc.get_config(user_id, conn_id, "strategy_version")
    idem = ex.build_idem_key(conn_id=conn_id, trade_date=trade_date, strategy_version=strategy_version,
                             bar_timestamp=key_ts, action=action, side=side or "none",
                             entry_rule=entry_rule, symbol=symbol)
    return ex.place_idempotent(
        adapter, user_id=user_id, conn_id=conn_id, idem_key=idem, side=side or "", symbol=symbol,
        qty=qty, price=price, action=action, dry_run=dry_run, trade_date=trade_date,
        strategy_version=strategy_version, bar_timestamp=key_ts, entry_rule=entry_rule,
        intent_seq=lr.next_intent_seq(user_id, conn_id, trade_date), price_fn=price_fn)


def _entry_price_fn(market: KiteMarket, symbol: str, cap: float, sent: dict):
    def fn():
        q = market.quote(symbol) or {}
        ref = max([v for v in (q.get("ask"), q.get("ltp")) if v] or [0]) or None
        if ref is None or ref > cap:
            sent.update(ask=ref, limit=None)
            return None
        limit = min(lr._round_tick(ref * (1 + lr.ENTRY_LIMIT_BUFFER_PCT)), lr._round_tick(cap))
        sent.update(ask=ref, limit=limit)
        return limit
    return fn


def process_connection(user_id: str, conn_id: str, *, ctx: dict, feed: PredictorFeed,
                       market: KiteMarket, now: datetime, adapter_factory=None) -> bool:
    """One cycle for one Proposer connection. Returns True while a position is open."""
    if not publish_heartbeat(user_id, conn_id, ctx["task_id"]):
        return False
    mode = ex.get_mode(user_id, conn_id)
    st = svc.get_trade_state(user_id, conn_id)
    is_open = (st.get("position") or "NONE").upper() == "OPEN"
    if mode == ex.Mode.DISARMED:
        if is_open and not int(st.get("virtual") or 0):
            adapter = _adapter(user_id, conn_id, ctx["adapters"], adapter_factory)
            try:
                if adapter is not None and int(adapter.get_position().qty or 0) == 0:
                    svc.reset_trade_state(user_id, conn_id)
                    log.warning("disarmed manual-exit reconcile cleared DB state conn=%s", conn_id)
            except Exception as e:
                log.warning("disarmed reconcile failed conn=%s: %s", conn_id, type(e).__name__)
        return False
    if not lr.market_session_available(now.replace(tzinfo=None)):
        return False
    dry_run = mode == ex.Mode.DRY_RUN
    adapter = _adapter(user_id, conn_id, ctx["adapters"], adapter_factory)
    if adapter is None:
        return is_open
    if not dry_run and is_open and int(st.get("virtual") or 0):
        log.warning("LIVE mode clearing leftover DRY position conn=%s", conn_id)
        svc.reset_trade_state(user_id, conn_id)
        st, is_open = svc.get_trade_state(user_id, conn_id), False
    key = (conn_id, mode.value)
    if key not in ctx["reconciled"]:
        if dry_run:
            svc.set_config(user_id, conn_id, "reconcile_blocked", "0")
        else:
            _reconcile(adapter, user_id, conn_id)
        ctx["reconciled"].add(key)
    blocked = (not dry_run) and svc.get_config(user_id, conn_id, "reconcile_blocked") == "1"

    trade_date = now.date().isoformat()
    pc = ctx["conns"].setdefault(conn_id, ProposerConnection(user_id, conn_id))
    pc.ensure_session(trade_date, dry_run, feed, now)
    day_gross, book_gross = pc.book(trade_date, dry_run)
    pc.engine.set_book(day_realized=day_gross, book_net=book_gross)
    snap = feed.snapshot()

    spot = market.spot()
    quote = market.quote(st["symbol"]) if is_open and st.get("symbol") else None
    ltp = (quote or {}).get("ltp")
    pos = pe.Position()
    if is_open:
        pos = pe.Position(side=("CE" if st.get("side") == "CALL" else "PE"),
                          entry_price=float(st.get("entry_price") or 0),
                          entry_spot=float(st.get("entry_spot") or 0), qty=int(st.get("qty") or 0))

    if is_open and now.time() >= EOD_FLAT:
        sig = pe.Signal("EXIT", st.get("side"), "eod")
    elif snap and spot:
        sig = pc.engine.evaluate(now, snap, pos, option_ltp=ltp, spot=spot)
    else:
        sig = pe.Signal("HOLD")

    if sig.action == "EXIT" and is_open:
        if not ltp:
            log.warning("exit %s deferred: no quote for %s", sig.reason, st.get("symbol"))
            return True
        ref = (quote or {}).get("bid") or ltp
        # Live crosses the spread by the marketable buffer; DRY_RUN books at the bid, as the
        # NIFTY runner does, so a paper day-target bank is not shaved 0.6% short of its trigger.
        price = ref if dry_run else lr._marketable_limit("SELL", ref)
        result = _route(adapter, user_id, conn_id, action="EXIT", side=st.get("side"),
                        symbol=st["symbol"], qty=int(st.get("qty") or 0), price=price,
                        dry_run=dry_run, key_ts=now.strftime("%Y-%m-%dT%H:%M") + f"|{sig.reason}")
        if lr._freshly_applied(result, dry_run=dry_run):
            lr._record_exit_result(user_id, conn_id, st,
                                   exit_price=result.avg_fill_price or (ltp if dry_run else price),
                                   qty=int(st.get("qty") or 0), reason=sig.reason, dry_run=dry_run)
            svc.reset_trade_state(user_id, conn_id)
            return False
        return True

    if (sig.action == "ENTER" and not is_open and not blocked and spot
            and now.time() < ENTRY_CUTOFF and lr.check_daily_loss(user_id, conn_id)):
        chain = option_chain(now.date())
        symbol, strike, typ = itm_symbol(chain, sig.side, spot)
        if not symbol:
            log.warning("no nearest-weekly %s%s in today's chain; entry skipped", strike, typ)
            return False
        broker_sym = adapter.broker_symbol(symbol) if hasattr(adapter, "broker_symbol") else symbol
        configured = svc.get_lots(user_id, conn_id)
        lots = ex.freeze_capped_lots(configured, LOT_SIZE, UNDERLYING)
        q = market.quote(symbol) or {}
        price = q.get("ask") or q.get("ltp")
        if not price:
            log.warning("entry skipped: no quote for %s", symbol)
            return False
        if lots < configured:
            notify_telegram(f"Proposer ENTER {sig.side}: {configured} lots is above the SENSEX freeze "
                            f"quantity; sending {lots} lots")
        sent: dict = {}
        price_fn = None if dry_run else _entry_price_fn(market, symbol, price * (1 + lr.ENTRY_CHASE_CAP_PCT), sent)
        key_ts = snap.get("x5_asof") or "none"
        step_downs = 0
        while True:
            result = _route(adapter, user_id, conn_id, action="ENTER", side=sig.side, symbol=broker_sym,
                            qty=lots * LOT_SIZE, price=price, dry_run=dry_run,
                            key_ts=key_ts + (f"|lots{lots}" if step_downs else ""),
                            entry_rule=sig.reason or "none", price_fn=price_fn)
            smaller = None if dry_run else ex.stepped_down_lots(lots, result)
            if not smaller or step_downs >= ex.LOT_STEP_DOWN_MAX:
                break
            step_downs += 1
            reason = str(ex.rejection_reason(result))[:150]
            log.warning("ENTER %s %s %d lots refused for size (%s) -- retrying with %d lots",
                        sig.side, broker_sym, lots, reason, smaller)
            notify_telegram(f"⚠️ Proposer ENTER {sig.side} {broker_sym} {lots} lots rejected ({reason}) "
                            f"— retrying with {smaller} lots")
            lots = smaller
        qty = lots * LOT_SIZE
        got = None if dry_run else ex.filled_qty(result)
        partial = bool(got and got > 0 and str(result.status or "").upper() in lr._IOC_NO_FILL
                       and not (result.raw or {}).get("idempotent_skip"))
        if got and got > 0 and (partial or lr._order_applied(result.status, dry_run=False)):
            qty = got                   # hold what actually filled
        if partial:
            notify_telegram(f"⚠️ Proposer ENTER {sig.side} {broker_sym} part-filled: holding "
                            f"{qty // LOT_SIZE} lots ({qty} qty)")
        if partial or lr._order_accepted(result, dry_run=dry_run):
            fill = result.avg_fill_price or sent.get("limit") or price
            st.update({"position": "OPEN", "side": sig.side, "symbol": broker_sym, "entry_price": fill,
                       "entry_time": datetime.now(timezone.utc).isoformat(), "qty": qty,
                       "virtual": 1 if dry_run else 0, "entry_rule": sig.reason, "entry_spot": spot})
            svc.save_trade_state(user_id, conn_id, st)
            pc.engine.mark_entry(snap)
            if pc.engine.entry_decisive:   # the once-per-day regime licence was just used
                svc.set_config(user_id, conn_id,
                               "proposer_regime_entry_date_" + ("dry" if dry_run else "live"), trade_date)
            notify_telegram(f"ENTER {sig.side} {broker_sym} @ {fill} | {sig.reason} | spot {spot:.0f}"
                            + (" [DRY-RUN]" if dry_run else ""))
            return True
        if str(result.status or "").upper() in ("FAILED", "PRICE_SKIP", "REJECTED"):
            notify_telegram(f"Proposer ENTER {sig.side} {broker_sym} {lots} lots {result.status}: "
                            f"{str(ex.rejection_reason(result) or (result.raw or {}).get('message') or sent)[:150]}")
    return is_open


# ═════════════════════════════════════════════════════════════════ main loop ══
def _heartbeat_loop(task_id: str, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            for user_id, conn_id in svc.readiness_connections():
                publish_heartbeat(user_id, conn_id, task_id)
        except Exception as e:
            log.error("proposer heartbeat error: %s", e)
        stop.wait(5)


def run(task_id: str | None = None, max_cycles: int | None = None, adapter_factory=None,
        market: KiteMarket | None = None, feed: PredictorFeed | None = None, clock=now_ist) -> None:
    task_id = task_id or f"proposer_runner:{uuid.uuid4().hex[:8]}"
    init_live_db()
    ensure_schema()
    ensure_regime_schema()
    log.info("proposer_runner boot | task=%s abi=%s regime_source=%s", task_id,
             ex.PROPOSER_DECISION_ABI, regime_source())
    regime_job = LlmRegimeJob()
    stop = threading.Event()
    hb = None
    if max_cycles is None:
        hb = threading.Thread(target=_heartbeat_loop, args=(task_id, stop), daemon=True,
                              name="proposer-heartbeat")
        hb.start()
    ctx = {"task_id": task_id, "adapters": {}, "reconciled": set(), "conns": {}}
    feed = feed or PredictorFeed()
    market = market or KiteMarket()
    cycles = 0
    try:
        while True:
            any_open = False
            now = clock()
            try:
                regime_job.step(now)
            except Exception as e:
                log.error("regime job error: %s", e)
            try:
                if lr.market_session_available(now) and now.time() <= dtime(15, 21):
                    feed.step(now)
            except Exception as e:
                log.error("predictor feed error: %s", e)
            try:
                for user_id, conn_id in svc.runner_connections():
                    if not ex.is_proposer_strategy(svc.get_config(user_id, conn_id, "strategy_version")):
                        continue
                    try:
                        any_open |= bool(process_connection(user_id, conn_id, ctx=ctx, feed=feed,
                                                            market=market, now=now,
                                                            adapter_factory=adapter_factory))
                    except Exception as e:
                        log.error("proposer conn %s cycle error: %s", conn_id, e)
            except Exception as e:
                log.error("proposer loop error: %s", e)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            time.sleep(POLL_OPEN_S if any_open else POLL_FLAT_S)
    finally:
        stop.set()
        if hb is not None:
            hb.join(timeout=6)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
