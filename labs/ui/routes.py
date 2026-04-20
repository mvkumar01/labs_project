"""
Flask Blueprint for all /labs routes.
"""
import json

from flask import Blueprint, render_template, request, redirect, url_for, jsonify

from config.labs_config import UNDERLYINGS, STRATEGY_TYPES, EXPIRY_MODES, STRIKE_MODES
from labs.services.bot_service import (
    list_bots, get_bot, create_bot, update_bot, update_status, clone_bot,
    save_legs, get_legs, LEG_CODES,
)
from labs.services.metrics_service import (
    get_all_bots_summary, get_trade_log, get_signal_log,
    get_equity_curve, get_performance_stats, get_open_position,
)

labs_bp = Blueprint("labs", __name__, url_prefix="/labs")

_FORM_DEFAULTS = dict(
    underlyings=list(UNDERLYINGS.keys()),
    strategy_types=STRATEGY_TYPES,
    expiry_modes=EXPIRY_MODES,
    strike_modes=STRIKE_MODES,
    leg_codes=LEG_CODES,
)


# ── Dashboard ────────────────────────────────────────────────────────────────

@labs_bp.route("/")
def dashboard():
    return render_template("labs.html", bots=get_all_bots_summary())


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
