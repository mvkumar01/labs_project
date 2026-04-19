"""
Flask Blueprint for all /labs routes.
"""
import json

from flask import Blueprint, render_template, request, redirect, url_for, jsonify

from config.labs_config import UNDERLYINGS, STRATEGY_TYPES, ENTRY_RULE_TOKENS, EXIT_RULE_TOKENS, EXPIRY_MODES, STRIKE_MODES
from labs.services.bot_service import (
    list_bots, get_bot, create_bot, update_bot, update_status, clone_bot,
)
from labs.services.metrics_service import (
    get_all_bots_summary, get_trade_log, get_signal_log,
    get_equity_curve, get_performance_stats, get_open_position,
)

labs_bp = Blueprint("labs", __name__, url_prefix="/labs")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@labs_bp.route("/")
def dashboard():
    summary = get_all_bots_summary()
    return render_template("labs.html", bots=summary)


# ---------------------------------------------------------------------------
# Create bot
# ---------------------------------------------------------------------------

@labs_bp.route("/new", methods=["GET", "POST"])
def new_bot():
    if request.method == "POST":
        data = _form_to_dict(request.form)
        bot  = create_bot(data)
        return redirect(url_for("labs.bot_detail", bot_id=bot["bot_id"]))

    return render_template(
        "bot_form.html",
        bot=None,
        underlyings=list(UNDERLYINGS.keys()),
        strategy_types=STRATEGY_TYPES,
        entry_rule_tokens=ENTRY_RULE_TOKENS,
        exit_rule_tokens=EXIT_RULE_TOKENS,
        expiry_modes=EXPIRY_MODES,
        strike_modes=STRIKE_MODES,
        action_url=url_for("labs.new_bot"),
        title="Create Bot",
    )


# ---------------------------------------------------------------------------
# Bot detail
# ---------------------------------------------------------------------------

@labs_bp.route("/<bot_id>")
def bot_detail(bot_id):
    bot   = get_bot(bot_id)
    if bot is None:
        return "Bot not found", 404
    stats = get_performance_stats(bot_id)
    pos   = get_open_position(bot_id)
    bot["entry_rules"] = json.loads(bot.get("entry_rules_json", "[]"))
    bot["exit_rules"]  = json.loads(bot.get("exit_rules_json",  "[]"))
    return render_template("bot_detail.html", bot=bot, stats=stats, position=pos)


# ---------------------------------------------------------------------------
# Edit bot
# ---------------------------------------------------------------------------

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
        return redirect(url_for("labs.bot_detail", bot_id=bot_id))

    bot["entry_rules"] = json.loads(bot.get("entry_rules_json", "[]"))
    bot["exit_rules"]  = json.loads(bot.get("exit_rules_json",  "[]"))
    return render_template(
        "bot_form.html",
        bot=bot,
        underlyings=list(UNDERLYINGS.keys()),
        strategy_types=STRATEGY_TYPES,
        entry_rule_tokens=ENTRY_RULE_TOKENS,
        exit_rule_tokens=EXIT_RULE_TOKENS,
        expiry_modes=EXPIRY_MODES,
        strike_modes=STRIKE_MODES,
        action_url=url_for("labs.edit_bot", bot_id=bot_id),
        title=f"Edit — {bot['name']}",
    )


# ---------------------------------------------------------------------------
# Clone / status / archive
# ---------------------------------------------------------------------------

@labs_bp.route("/<bot_id>/clone", methods=["POST"])
def clone(bot_id):
    new_name = request.form.get("new_name") or None
    new_bot  = clone_bot(bot_id, new_name=new_name)
    return redirect(url_for("labs.edit_bot", bot_id=new_bot["bot_id"]))


@labs_bp.route("/<bot_id>/status", methods=["POST"])
def toggle_status(bot_id):
    bot    = get_bot(bot_id)
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


# ---------------------------------------------------------------------------
# JSON APIs
# ---------------------------------------------------------------------------

@labs_bp.route("/api/summary")
def api_summary():
    return jsonify(get_all_bots_summary())


@labs_bp.route("/api/<bot_id>/trades")
def api_trades(bot_id):
    limit = int(request.args.get("limit", 200))
    return jsonify(get_trade_log(bot_id, limit=limit))


@labs_bp.route("/api/<bot_id>/signals")
def api_signals(bot_id):
    limit = int(request.args.get("limit", 200))
    return jsonify(get_signal_log(bot_id, limit=limit))


@labs_bp.route("/api/<bot_id>/equity")
def api_equity(bot_id):
    return jsonify(get_equity_curve(bot_id))


@labs_bp.route("/api/<bot_id>/stats")
def api_stats(bot_id):
    return jsonify(get_performance_stats(bot_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _form_to_dict(form) -> dict:
    d = dict(form)
    # Multi-select fields come as lists; convert others to scalar
    for key, val in d.items():
        if isinstance(val, list) and key not in ("entry_rules_json", "exit_rules_json"):
            d[key] = val[0] if val else ""
    # Convert checkbox multi-select lists to JSON strings
    entry_rules = form.getlist("entry_rules")
    exit_rules  = form.getlist("exit_rules")
    d["entry_rules_json"] = json.dumps(entry_rules)
    d["exit_rules_json"]  = json.dumps(exit_rules)
    return d
