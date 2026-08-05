"""
Flask Blueprint for all /labs routes.
"""
import json
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, jsonify, send_file

from config.labs_config import UNDERLYINGS, STRATEGY_TYPES, EXPIRY_MODES, STRIKE_MODES
from labs.services.bot_service import (
    list_bots, get_bot, create_bot, update_bot, update_status, clone_bot, delete_bot,
    save_legs, get_legs, LEG_CODES,
)
from labs.services.metrics_service import (
    get_all_bots_summary, get_trade_log, get_signal_log,
    get_equity_curve, get_performance_stats, get_open_position,
)
from labs.engine.backtest import get_backtest_bots, scan_data_ranges, run_backtest
from labs.services.calibration_service import (
    SYMBOLS as CALIBRATION_SYMBOLS,
    INTERVALS as CALIBRATION_INTERVALS,
    artifact_path,
    calibration_context,
    candidate_action,
    recalculate,
    shadow_test,
)

labs_bp = Blueprint("labs", __name__, url_prefix="/labs")

_FORM_DEFAULTS = dict(
    underlyings=list(UNDERLYINGS.keys()),
    strategy_types=STRATEGY_TYPES,
    expiry_modes=EXPIRY_MODES,
    strike_modes=STRIKE_MODES,
    leg_codes=LEG_CODES,
)


def _live_date_range() -> tuple[str | None, str | None]:
    """Return a validated inclusive date range from the live dashboard query."""
    values = []
    for key in ("date_from", "date_to"):
        raw = (request.args.get(key) or "").strip()
        try:
            values.append(datetime.strptime(raw, "%Y-%m-%d").date().isoformat())
        except ValueError:
            values.append(None)
    date_from, date_to = values
    if date_from and date_to and date_to < date_from:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def _live_date_clause(
    date_from: str | None, date_to: str | None, *, column: str = "trade_date"
) -> tuple[str, list[str]]:
    clauses = []
    params = []
    if date_from:
        clauses.append(f"{column} >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{column} <= ?")
        params.append(date_to)
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


# ── Dashboard ────────────────────────────────────────────────────────────────

@labs_bp.route("/")
def dashboard():
    return render_template("labs.html", bots=get_all_bots_summary())


@labs_bp.route("/backtest")
def backtest_page():
    return render_template(
        "backtest.html",
        bots=get_backtest_bots(),
        underlyings=list(UNDERLYINGS.keys()),
    )


# ── OI Market Read Calibration Center ────────────────────────────────────────

@labs_bp.route("/calibration")
def calibration_page():
    symbol = (request.args.get("symbol") or "NIFTY").upper()
    try:
        interval = int(request.args.get("interval") or 5)
        context = calibration_context(symbol, interval)
    except (ValueError, FileNotFoundError) as exc:
        return str(exc), 400
    return render_template(
        "calibration.html", **context,
        calibration_symbols=CALIBRATION_SYMBOLS,
        calibration_intervals=CALIBRATION_INTERVALS,
        message=request.args.get("message"),
        error=request.args.get("error"),
    )


@labs_bp.route("/calibration/recalculate", methods=["POST"])
def calibration_recalculate():
    symbol = (request.form.get("symbol") or "NIFTY").upper()
    interval = int(request.form.get("interval") or 5)
    thresholds = {
        key.removeprefix("threshold_"): value
        for key, value in request.form.items()
        if key.startswith("threshold_")
    }
    try:
        candidate = recalculate(symbol, interval, thresholds)
        message = f"{candidate['candidate_version']} recalculated: {candidate['recommendation']}"
        return redirect(url_for("labs.calibration_page", symbol=symbol, interval=interval, message=message))
    except Exception as exc:
        return redirect(url_for("labs.calibration_page", symbol=symbol, interval=interval, error=str(exc)))


@labs_bp.route("/calibration/action", methods=["POST"])
def calibration_action():
    symbol = (request.form.get("symbol") or "NIFTY").upper()
    interval = int(request.form.get("interval") or 5)
    action = request.form.get("action") or ""
    try:
        candidate = shadow_test(symbol, interval) if action == "shadow" else candidate_action(symbol, interval, action)
        message = f"{action.replace('_', ' ').title()}: {candidate.get('status', candidate.get('shadow_status'))}"
        return redirect(url_for("labs.calibration_page", symbol=symbol, interval=interval, message=message))
    except Exception as exc:
        return redirect(url_for("labs.calibration_page", symbol=symbol, interval=interval, error=str(exc)))


@labs_bp.route("/calibration/download/<artifact>")
def calibration_download(artifact):
    symbol = (request.args.get("symbol") or "NIFTY").upper()
    interval = int(request.args.get("interval") or 5)
    try:
        path = artifact_path(symbol, interval, artifact)
    except (ValueError, FileNotFoundError) as exc:
        return str(exc), 404
    if not path.is_file():
        return "Artifact not found", 404
    return send_file(path, as_attachment=artifact != "markdown", download_name=path.name)


@labs_bp.route("/api/calibration")
def api_calibration():
    symbol = (request.args.get("symbol") or "NIFTY").upper()
    interval = int(request.args.get("interval") or 5)
    try:
        return jsonify(calibration_context(symbol, interval))
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ── Create ───────────────────────────────────────────────────────────────────

@labs_bp.route("/new", methods=["GET", "POST"])
def new_bot():
    if request.method == "POST":
        data  = _form_to_dict(request.form)
        bot   = create_bot(data)
        legs  = _parse_legs(request.form)
        if legs:
            save_legs(bot["bot_id"], legs)
        return redirect(url_for("labs.bot_detail", bot_id=bot["bot_id"]))

    return render_template("bot_form.html", bot=None, legs={},
                           action_url=url_for("labs.new_bot"),
                           title="Create Bot", **_FORM_DEFAULTS)


# ── Detail ───────────────────────────────────────────────────────────────────

@labs_bp.route("/<bot_id>")
def bot_detail(bot_id):
    bot = get_bot(bot_id)
    if bot is None:
        return "Bot not found", 404
    stats = get_performance_stats(bot_id)
    pos   = get_open_position(bot_id)
    legs  = {lg["leg_code"]: lg for lg in get_legs(bot_id)}
    return render_template("bot_detail.html", bot=bot, stats=stats, position=pos, legs=legs)


# ── Edit ─────────────────────────────────────────────────────────────────────

@labs_bp.route("/<bot_id>/edit", methods=["GET", "POST"])
def edit_bot(bot_id):
    # Always re-read bot status from SQLite for edit/save permission checks.
    bot = get_bot(bot_id)
    if bot is None:
        return "Bot not found", 404
    if bot["status"] == "active":
        return "Pause the bot before editing.", 400

    if request.method == "POST":
        data = _form_to_dict(request.form)
        update_bot(bot_id, data)
        legs = _parse_legs(request.form)
        save_legs(bot_id, legs)
        return redirect(url_for("labs.bot_detail", bot_id=bot_id))

    legs = {lg["leg_code"]: lg for lg in get_legs(bot_id)}
    return render_template("bot_form.html", bot=bot, legs=legs,
                           action_url=url_for("labs.edit_bot", bot_id=bot_id),
                           title=f"Edit — {bot['name']}", **_FORM_DEFAULTS)


# ── Clone / status / archive ─────────────────────────────────────────────────

@labs_bp.route("/<bot_id>/clone", methods=["POST"])
def clone(bot_id):
    new_name = request.form.get("new_name") or None
    new_bot  = clone_bot(bot_id, new_name=new_name)
    return redirect(url_for("labs.edit_bot", bot_id=new_bot["bot_id"]))


@labs_bp.route("/<bot_id>/status", methods=["POST"])
def toggle_status(bot_id):
    bot = get_bot(bot_id)
    if bot is None:
        return "Not found", 404
    if bot["status"] == "archived":
        return "Archived bots cannot change status.", 400
    new_status = "active" if bot["status"] == "paused" else "paused"
    update_status(bot_id, new_status)
    return redirect(url_for("labs.bot_detail", bot_id=bot_id))


@labs_bp.route("/<bot_id>/archive", methods=["POST"])
def archive(bot_id):
    update_status(bot_id, "archived")
    return redirect(url_for("labs.dashboard"))


@labs_bp.route("/<bot_id>/delete", methods=["POST"])
def delete(bot_id):
    bot = get_bot(bot_id)
    if bot is None:
        return "Not found", 404
    if bot["status"] == "active":
        return "Pause the bot before deleting.", 400
    delete_bot(bot_id)
    return redirect(url_for("labs.dashboard"))


# ── JSON APIs ────────────────────────────────────────────────────────────────

@labs_bp.route("/api/summary")
def api_summary():
    return jsonify(get_all_bots_summary())

@labs_bp.route("/api/<bot_id>/trades")
def api_trades(bot_id):
    return jsonify(get_trade_log(bot_id, limit=int(request.args.get("limit", 200))))

@labs_bp.route("/api/<bot_id>/signals")
def api_signals(bot_id):
    return jsonify(get_signal_log(bot_id, limit=int(request.args.get("limit", 200))))

@labs_bp.route("/api/<bot_id>/equity")
def api_equity(bot_id):
    return jsonify(get_equity_curve(bot_id))

@labs_bp.route("/api/<bot_id>/stats")
def api_stats(bot_id):
    return jsonify(get_performance_stats(bot_id))

@labs_bp.route("/api/<bot_id>/legs")
def api_legs(bot_id):
    return jsonify(get_legs(bot_id))


@labs_bp.route("/api/backtest/data-ranges")
def api_backtest_data_ranges():
    return jsonify(scan_data_ranges())


@labs_bp.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    data = request.get_json(silent=True) or {}
    required = ["bot_id", "underlying", "start_date", "end_date"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        return jsonify({"ok": False, "error": f"Missing fields: {', '.join(missing)}"}), 400
    try:
        result = run_backtest(
            data["bot_id"],
            data["underlying"],
            data["start_date"],
            data["end_date"],
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Backtest failed: {exc}"}), 500
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


# ── Helpers ───────────────────────────────────────────────────────────────────

def _form_to_dict(form) -> dict:
    d = {}
    for key in form.keys():
        if key.startswith("leg_"):
            continue                   # legs handled separately
        vals = form.getlist(key)
        d[key] = vals[0] if len(vals) == 1 else vals
    # Keep old-style rule fields as JSON for backward compat
    d["entry_rules_json"] = json.dumps(form.getlist("entry_rules"))
    d["exit_rules_json"]  = json.dumps(form.getlist("exit_rules"))
    return d


def _parse_legs(form) -> list[dict]:
    """Extract per-leg config from form fields."""
    legs = []
    for code in LEG_CODES:
        if not form.get(f"leg_{code}_enabled"):
            continue
        legs.append({
            "leg_code":                code,
            "entry_logic":             form.get(f"leg_{code}_entry_logic", "AND"),
            "entry_conditions_json":   form.get(f"leg_{code}_entry_conditions", "[]"),
            "entry_gates_json":        form.get(f"leg_{code}_entry_gates", "[]"),
            "exit_conditions_json":    form.get(f"leg_{code}_exit_conditions", "[]"),
            "stoploss_conditions_json": form.get(f"leg_{code}_stoploss_conditions", "[]"),
        })
    return legs


# ── Live (persistent paper strategy tracker) ─────────────────────────────────
@labs_bp.route("/live")
def live_strategy():
    """Daily PAPER performance of Alpha champion books. Read-only,
    no login — populated EOD by pa_paper_tracker.py. Net PnL includes charges."""
    from storage.db import get_conn
    from labs.engine.paper_strategy_tracker import CONTRACT_VARIANTS, PRIMARY_VARIANT
    date_from, date_to = _live_date_range()
    date_clause, date_params = _live_date_clause(date_from, date_to)
    active_live_tab = request.args.get("tab", "nifty")
    if active_live_tab not in {
        "nifty", "alpha_v211a", "alpha_v212", "alpha_v213",
        "sensex_alpha", "sensex_alpha_inverted",
        "sensex_v211", "sensex_v211_inverted", "baskets",
    }:
        active_live_tab = "nifty"
    rows, trades, stats = [], [], {}
    comparison_variant_totals = {}
    comparison_by_date = {}
    sensex_rows, sensex_trades, sensex_stats = [], [], {}
    sensex_v211_rows, sensex_v211_trades, sensex_v211_stats = [], [], {}
    overlay_rows, overlay_trades, overlay_stats = [], [], {}
    overlay_version = {
        "alpha_v211a": "v2.11A",
        "alpha_v212": "v2.12",
        "alpha_v213": "v2.13",
    }.get(active_live_tab, "")
    basket_defs, basket_totals, basket_by_date = {}, {}, {}
    basket_pending, basket_error = 0, None
    try:
        conn = get_conn()
        cur = conn.execute(
            "SELECT trade_date, status, tier, gap_dir, n_trades, pnl_pts, gross_rs, "
            "charges_rs, net_rs FROM paper_strategy_daily WHERE 1=1 "
            f"{date_clause} ORDER BY trade_date DESC LIMIT 120",
            date_params,
        )
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if rows:
            net = [float(r["net_rs"] or 0) for r in rows]
            traded = [r for r in rows if r["status"] == "traded"]
            wins = [n for n in net if n > 0]
            stats = {
                "net_total": round(sum(net), 2),
                "charges_total": round(sum(float(r["charges_rs"] or 0) for r in rows), 2),
                "days": len(rows),
                "traded_days": len(traded),
                "win_days": len(wins),
                "win_pct": round(100 * len(wins) / max(len([n for n in net if n != 0]), 1)),
                "best": round(max(net), 2) if net else 0,
                "worst": round(min(net), 2) if net else 0,
                "gross_profit": round(sum(n for n in net if n > 0), 2),
                "gross_loss": round(sum(n for n in net if n < 0), 2),
            }
            # cumulative (oldest -> newest) for the equity curve
            cum = 0.0
            curve = []
            for r in sorted(rows, key=lambda x: x["trade_date"]):
                cum += float(r["net_rs"] or 0)
                curve.append({"date": r["trade_date"], "cum": round(cum, 2)})
            stats["curve"] = curve
        latest = rows[0]["trade_date"] if rows else None
        if latest:
            cur2 = conn.execute(
                "SELECT seq, side, strike, entry_ts, exit_ts, entry_spot, exit_spot, "
                "entry_prem, exit_prem, pnl_pts, gross_rs, charges_rs, net_rs, entry_rule, "
                "exit_reason FROM paper_strategy_trades WHERE trade_date=? ORDER BY seq", (latest,))
            tcols = [c[0] for c in cur2.description]
            trades = [dict(zip(tcols, r)) for r in cur2.fetchall()]
            stats["latest_date"] = latest

        # ── Comparison variants (graceful: missing table = show placeholder) ──
        try:
            date_filter = rows[0]["trade_date"] if rows else "0000-00-00"
            date_cutoff = rows[-1]["trade_date"] if rows else "9999-99-99"
            cmp_cur = conn.execute(
                "SELECT trade_date, variant, expiry_mode, expiry_code, strike_offset, "
                "status, n_trades, gross_rs, charges_rs, net_rs, error "
                "FROM paper_contract_daily "
                "WHERE trade_date >= ? AND trade_date <= ? "
                "ORDER BY trade_date DESC",
                (date_cutoff, date_filter))
            ccols = [c[0] for c in cmp_cur.description]
            cmp_rows = [dict(zip(ccols, r)) for r in cmp_cur.fetchall()]

            # Per-variant running totals
            for vkey in CONTRACT_VARIANTS:
                vrows = [r for r in cmp_rows if r["variant"] == vkey]
                priced = [r for r in vrows if r["status"] in ("priced", "open")]
                unavail = [r for r in vrows if r["status"] == "unavailable"]
                net_vals = [float(r["net_rs"]) for r in priced if r["net_rs"] is not None]
                charges_vals = [float(r["charges_rs"]) for r in priced if r["charges_rs"] is not None]
                latest_code = next(
                    (r["expiry_code"] for r in sorted(vrows, key=lambda x: x["trade_date"], reverse=True)
                     if r.get("expiry_code")), None)
                comparison_variant_totals[vkey] = {
                    "label": CONTRACT_VARIANTS[vkey]["label"],
                    "net_total": round(sum(net_vals), 2),
                    "charges_total": round(sum(charges_vals), 2),
                    "priced_days": len(priced),
                    "unavailable_days": len(unavail),
                    "latest_expiry": latest_code,
                }

            # Pivot by date for the daily comparison table
            for r in cmp_rows:
                d = r["trade_date"]
                if d not in comparison_by_date:
                    comparison_by_date[d] = {}
                comparison_by_date[d][r["variant"]] = (
                    float(r["net_rs"]) if r["net_rs"] is not None else None
                )
        except Exception:
            # Comparison tables don't exist yet (pre-backfill rollout)
            comparison_variant_totals = {}
            comparison_by_date = {}

        # v2.11A, v2.12 and v2.13 are separate paper ledgers backed by their respective
        # canonical replay engines. Only load the selected tab's tables.
        if active_live_tab in {"alpha_v211a", "alpha_v212", "alpha_v213"}:
            overlay_prefix = active_live_tab
            try:
                overlay_cur = conn.execute(
                    "SELECT trade_date,status,tier,gap_dir,expiry_code,n_segments,"
                    "priced_segments,unavailable_segments,spot_pnl_pts,gross_rs,"
                    "charges_rs,net_rs,strategy_version,updated_at "
                    f"FROM {overlay_prefix}_daily WHERE trade_date >= '2026-06-01' "
                    f"{date_clause} ORDER BY trade_date DESC",
                    date_params,
                )
                overlay_cols = [column[0] for column in overlay_cur.description]
                overlay_rows = [
                    dict(zip(overlay_cols, row)) for row in overlay_cur.fetchall()
                ]
                if overlay_rows:
                    active_days = [
                        row for row in overlay_rows
                        if int(row["n_segments"] or 0) > 0
                        and row["status"] != "open"
                    ]
                    wins = [
                        row for row in active_days
                        if float(row["net_rs"] or 0) > 0
                    ]
                    total_segments = sum(
                        int(row["n_segments"] or 0) for row in overlay_rows
                    )
                    priced_segments = sum(
                        int(row["priced_segments"] or 0) for row in overlay_rows
                    )
                    overlay_stats = {
                        "days": len(overlay_rows),
                        "active_days": len(active_days),
                        "win_days": len(wins),
                        "win_pct": round(
                            100 * len(wins) / max(len(active_days), 1)
                        ),
                        "segments": total_segments,
                        "priced_segments": priced_segments,
                        "unavailable_segments": sum(
                            int(row["unavailable_segments"] or 0)
                            for row in overlay_rows
                        ),
                        "coverage_pct": round(
                            100 * priced_segments / max(total_segments, 1), 1
                        ),
                        "spot_total": round(
                            sum(
                                float(row["spot_pnl_pts"] or 0)
                                for row in overlay_rows
                            ),
                            2,
                        ),
                        "gross_total": round(
                            sum(
                                float(row["gross_rs"] or 0)
                                for row in overlay_rows
                            ),
                            2,
                        ),
                        "charges_total": round(
                            sum(
                                float(row["charges_rs"] or 0)
                                for row in overlay_rows
                            ),
                            2,
                        ),
                        "net_total": round(
                            sum(
                                float(row["net_rs"] or 0)
                                for row in overlay_rows
                            ),
                            2,
                        ),
                        "first_date": overlay_rows[-1]["trade_date"],
                        "last_date": overlay_rows[0]["trade_date"],
                        "latest": overlay_rows[0],
                    }
                    overlay_trade_cur = conn.execute(
                        "SELECT seq,status,side,strike,expiry_code,tradingsymbol,"
                        "entry_ts,exit_ts,entry_spot,exit_spot,spot_pnl_pts,entry_bid,"
                        "entry_ask,exit_bid,exit_ask,option_pnl_pts,gross_rs,charges_rs,"
                        "net_rs,quote_status,entry_rule,exit_reason "
                        f"FROM {overlay_prefix}_trades WHERE trade_date=? ORDER BY seq",
                        (overlay_rows[0]["trade_date"],),
                    )
                    overlay_trade_cols = [
                        column[0] for column in overlay_trade_cur.description
                    ]
                    overlay_trades = [
                        dict(zip(overlay_trade_cols, row))
                        for row in overlay_trade_cur.fetchall()
                    ]
            except Exception as exc:
                if "no such table" not in str(exc):
                    overlay_stats = {"error": str(exc)}

        # SENSEX-own Alpha is a separate paper book and never changes the
        # NIFTY v2.11 rows above. Missing tables degrade to an empty tab during
        # first deployment; the paper loop creates them on its first valid run.
        try:
            sx_inverted = active_live_tab == "sensex_alpha_inverted"
            # Names interchanged per user request: each tab reads the OTHER
            # table, so "Sensex_alpha" now shows the formerly-mislabeled inverted
            # data and "Sensex_alpha inverted" shows the normal book.
            sx_daily_table = (
                "sensex_alpha_daily" if sx_inverted
                else "sensex_alpha_inverted_daily"
            )
            sx_trades_table = (
                "sensex_alpha_trades" if sx_inverted
                else "sensex_alpha_inverted_trades"
            )
            sx_cur = conn.execute(
                "SELECT trade_date,status,prev_close,range_lower,range_upper,latest_mark,"
                "latest_spot,latest_alpha,position_side,n_trades,spot_pnl_pts,"
                "option_gross_rs,option_priced_trades,option_unavailable_trades,"
                "expiry_code,baseline_date,liquidity_mode,liquidity_mark,"
                "selected_expiry_type,weekly_expiry_code,monthly_expiry_code,"
                f"weekly_in_band_oi,monthly_in_band_oi FROM {sx_daily_table} "
                f"WHERE 1=1 {date_clause} ORDER BY trade_date DESC LIMIT 120",
                date_params,
            )
            sx_cols = [column[0] for column in sx_cur.description]
            sensex_rows = [dict(zip(sx_cols, row)) for row in sx_cur.fetchall()]
            if sensex_rows:
                latest_sx = sensex_rows[0]
                completed_option_days = [
                    float(row["option_gross_rs"] or 0)
                    for row in sensex_rows
                    if row["status"] != "open"
                    and float(row["option_gross_rs"] or 0) != 0
                ]
                win_days = sum(value > 0 for value in completed_option_days)
                sensex_stats = {
                    "days": len(sensex_rows),
                    "trades": sum(int(row["n_trades"] or 0) for row in sensex_rows),
                    "spot_total": round(
                        sum(float(row["spot_pnl_pts"] or 0) for row in sensex_rows), 2
                    ),
                    "option_total": round(
                        sum(float(row["option_gross_rs"] or 0) for row in sensex_rows), 2
                    ),
                    "priced_trades": sum(
                        int(row["option_priced_trades"] or 0) for row in sensex_rows
                    ),
                    "unavailable_trades": sum(
                        int(row["option_unavailable_trades"] or 0) for row in sensex_rows
                    ),
                    "win_days": win_days,
                    "win_pct": round(
                        100 * win_days / max(len(completed_option_days), 1)
                    ),
                    "latest": latest_sx,
                }
                sx_trade_cur = conn.execute(
                    "SELECT seq,status,side,strike,tradingsymbol,expiry_code,entry_ts,"
                    "exit_ts,entry_alpha,exit_alpha,entry_spot,exit_spot,spot_pnl_pts,"
                    "entry_bid,entry_ask,exit_bid,exit_ask,option_pnl_pts,option_gross_rs,"
                    "quote_status,entry_reason,exit_reason "
                    f"FROM {sx_trades_table} "
                    "WHERE trade_date=? ORDER BY seq",
                    (latest_sx["trade_date"],),
                )
                sx_trade_cols = [column[0] for column in sx_trade_cur.description]
                sensex_trades = [
                    dict(zip(sx_trade_cols, row)) for row in sx_trade_cur.fetchall()
                ]
        except Exception as exc:
            if active_live_tab in {
                "sensex_alpha", "sensex_alpha_inverted"
            } and "no such table" not in str(exc):
                sensex_stats = {"error": str(exc)}

        # Same v2.11 NIFTY signals, separately executed in SENSEX ATM options.
        try:
            sv_inverted = active_live_tab == "sensex_v211_inverted"
            sv_daily_table = (
                "sensex_v211_inverted_daily" if sv_inverted
                else "sensex_v211_daily"
            )
            sv_trades_table = (
                "sensex_v211_inverted_trades" if sv_inverted
                else "sensex_v211_trades"
            )
            sv_cur = conn.execute(
                "SELECT trade_date,status,tier,gap_dir,expiry_code,n_trades,"
                "option_gross_rs,option_priced_trades,option_unavailable_trades "
                f"FROM {sv_daily_table} WHERE 1=1 {date_clause} "
                "ORDER BY trade_date DESC LIMIT 120",
                date_params,
            )
            sv_cols = [column[0] for column in sv_cur.description]
            sensex_v211_rows = [dict(zip(sv_cols, row)) for row in sv_cur.fetchall()]
            if sensex_v211_rows:
                latest_sv = sensex_v211_rows[0]
                sensex_v211_stats = {
                    "days": len(sensex_v211_rows),
                    "trades": sum(
                        int(row["n_trades"] or 0) for row in sensex_v211_rows
                    ),
                    "option_total": round(
                        sum(
                            float(row["option_gross_rs"] or 0)
                            for row in sensex_v211_rows
                        ),
                        2,
                    ),
                    "priced_trades": sum(
                        int(row["option_priced_trades"] or 0)
                        for row in sensex_v211_rows
                    ),
                    "unavailable_trades": sum(
                        int(row["option_unavailable_trades"] or 0)
                        for row in sensex_v211_rows
                    ),
                    "latest": latest_sv,
                }
                sv_trade_cur = conn.execute(
                    "SELECT seq,status,side,strike,tradingsymbol,expiry_code,entry_ts,"
                    "exit_ts,entry_sensex,exit_sensex,entry_bid,entry_ask,exit_bid,"
                    "exit_ask,option_pnl_pts,option_gross_rs,quote_status,entry_rule,"
                    f"exit_reason FROM {sv_trades_table} "
                    "WHERE trade_date=? ORDER BY seq",
                    (latest_sv["trade_date"],),
                )
                sv_trade_cols = [column[0] for column in sv_trade_cur.description]
                sensex_v211_trades = [
                    dict(zip(sv_trade_cols, row)) for row in sv_trade_cur.fetchall()
                ]
        except Exception as exc:
            if active_live_tab in {
                "sensex_v211", "sensex_v211_inverted"
            } and "no such table" not in str(exc):
                sensex_v211_stats = {"error": str(exc)}

        # ── Basket replay (v2.11 signals re-priced as multi-leg structures) ──
        try:
            from labs.engine.basket_replay import BASKETS, pending_dates
            basket_defs = BASKETS
            # pending_dates() first — it CREATEs the basket tables, so the
            # SELECT below cannot die with "no such table" on a fresh deploy
            # (which silently zeroed the pending count and hid the button).
            basket_pending = len(pending_dates(conn))
            bk_cur = conn.execute(
                "SELECT trade_date,side,basket,expiry_code,n_trades,priced,"
                "unavailable,gross_rs,charges_rs,net_rs FROM basket_daily "
                f"WHERE 1=1 {date_clause} ORDER BY trade_date DESC",
                date_params,
            )
            bk_cols = [column[0] for column in bk_cur.description]
            bk_rows = [dict(zip(bk_cols, row)) for row in bk_cur.fetchall()]
            for row in bk_rows:
                bdef = BASKETS.get(row["side"], {}).get(row["basket"])
                if bdef is None:
                    continue  # stale row from a basket no longer defined
                key = f"{row['side']}:{row['basket']}"
                d = row["trade_date"]
                basket_by_date.setdefault(d, {})[key] = (
                    float(row["net_rs"]) if row["priced"] else None
                )
                tot = basket_totals.setdefault(key, {
                    "side": row["side"], "basket": row["basket"],
                    "label": bdef["label"],
                    "net_total": 0.0, "charges_total": 0.0, "trades": 0,
                    "priced": 0, "unavailable": 0, "win_days": 0,
                    "loss_days": 0, "worst_day": 0.0, "best_day": 0.0,
                })
                net = float(row["net_rs"] or 0)
                tot["net_total"] = round(tot["net_total"] + net, 2)
                tot["charges_total"] = round(
                    tot["charges_total"] + float(row["charges_rs"] or 0), 2)
                tot["trades"] += int(row["n_trades"] or 0)
                tot["priced"] += int(row["priced"] or 0)
                tot["unavailable"] += int(row["unavailable"] or 0)
                if row["priced"]:
                    if net > 0:
                        tot["win_days"] += 1
                    elif net < 0:
                        tot["loss_days"] += 1
                    tot["worst_day"] = round(min(tot["worst_day"], net), 2)
                    tot["best_day"] = round(max(tot["best_day"], net), 2)
        except Exception as exc:
            if active_live_tab == "baskets" and "no such table" not in str(exc):
                basket_error = str(exc)

    except Exception as exc:  # never 500 the page if the table is empty/new
        stats = {"error": str(exc)}
    return render_template(
        "live_strategy.html",
        basket_defs=basket_defs,
        basket_totals=basket_totals,
        basket_by_date=basket_by_date,
        basket_pending=basket_pending,
        basket_error=basket_error,
        rows=rows, trades=trades, stats=stats,
        contract_variants=CONTRACT_VARIANTS,
        comparison_variant_totals=comparison_variant_totals,
        comparison_by_date=comparison_by_date,
        active_live_tab=active_live_tab,
        sensex_rows=sensex_rows,
        sensex_trades=sensex_trades,
        sensex_stats=sensex_stats,
        sensex_v211_rows=sensex_v211_rows,
        sensex_v211_trades=sensex_v211_trades,
        sensex_v211_stats=sensex_v211_stats,
        overlay_rows=overlay_rows,
        overlay_trades=overlay_trades,
        overlay_stats=overlay_stats,
        overlay_version=overlay_version,
        date_from=date_from,
        date_to=date_to,
    )


@labs_bp.route("/api/baskets/refresh", methods=["POST"])
def baskets_refresh():
    """Replay up to `limit` pending v2.11 days into the basket tables. Bounded
    per call so a PA web request never runs long; the tab's Refresh button
    keeps calling while `remaining` > 0. Paper data only — no orders."""
    from labs.engine.basket_replay import run_backfill
    try:
        limit = min(int(request.args.get("limit", 5)), 10)
    except (TypeError, ValueError):
        limit = 5
    try:
        result = run_backfill(limit=limit)
        return jsonify({
            "ok": True,
            "done": len(result["done"]),
            "remaining": result["remaining"],
            "errors": result["errors"],
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@labs_bp.route("/api/alpha_v213/backfill", methods=["POST"])
def alpha_v213_backfill():
    """Replay up to `limit` pending Alpha v2.13 days (default from 2026-06-01)
    into alpha_v213_daily/_trades, reusing v2.12's per-day champion ranges.
    Bounded per call so a PA web request never runs long; keep calling while
    `remaining` > 0. Paper data only — no orders."""
    from labs.engine.alpha_v213_backfill import DEFAULT_START, run_backfill
    try:
        limit = min(int(request.args.get("limit", 5)), 10)
    except (TypeError, ValueError):
        limit = 5
    start = request.args.get("start") or DEFAULT_START
    end = request.args.get("end") or None
    try:
        result = run_backfill(
            start_date=start,
            end_date=end,
            limit=limit,
            rebuild=request.args.get("rebuild") == "1",
        )
        return jsonify({
            "ok": True,
            "done": len(result["done"]),
            "dates": result["done"],
            "remaining": result["remaining"],
            "errors": result["errors"],
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
