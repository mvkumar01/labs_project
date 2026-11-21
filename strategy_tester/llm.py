"""Optional Claude integration: prompt understanding + written commentary.

Enabled automatically when ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is set.
Set USE_CLAUDE=0 to force the offline rule-based parser.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

from .strategies import STRATEGIES

log = logging.getLogger("strategy-tester.llm")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

_client = None
_disabled_reason: str | None = None


def enabled() -> bool:
    if os.environ.get("USE_CLAUDE", "1") == "0":
        return False
    if _disabled_reason:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def status() -> dict:
    return {"enabled": enabled(), "model": MODEL if enabled() else None, "reason": _disabled_reason}


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(max_retries=2, timeout=90.0)
    return _client


def _call(system: str, user: str, effort: str, schema: dict | None = None, max_tokens: int = 16000) -> str | None:
    global _disabled_reason
    import anthropic
    client = _get_client()
    output_config: dict = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs = dict(model=MODEL, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}], output_config=output_config)
    try:
        try:
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except TypeError:  # older SDK without the fallbacks parameter
            resp = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        _disabled_reason = "Invalid Anthropic API key, so the app is using the rule-based parser."
        log.warning(_disabled_reason)
        return None
    except anthropic.PermissionDeniedError as e:
        _disabled_reason = f"Anthropic API permission denied: {e.message}"
        return None
    except anthropic.RateLimitError:
        log.warning("Claude rate limited, so this request used the rule-based parser.")
        return None
    except anthropic.APIStatusError as e:
        log.warning("Claude API error %s: %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError:
        log.warning("Could not reach the Claude API.")
        return None
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return text or None


# ---------------------------------------------------------------------------------- parse
def _nullable(t: str) -> dict:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


SPEC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "evaluate", "symbols", "strategies", "start", "end", "interval", "config", "notes"],
    "properties": {
        "intent": {"type": "string", "enum": ["backtest", "compare", "analyze"]},
        "evaluate": {"type": "boolean"},
        "symbols": {"type": "array", "items": {"type": "string"}},
        "strategies": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "params"],
            "properties": {
                "key": {"type": "string", "enum": list(STRATEGIES)},
                "params": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["name", "value"],
                    "properties": {"name": {"type": "string"}, "value": {"type": "number"}}}},
            }}},
        "start": _nullable("string"),
        "end": _nullable("string"),
        "interval": {"type": "string", "enum": ["1d", "1wk", "1mo", "1h"]},
        "config": {
            "type": "object", "additionalProperties": False,
            "required": ["capital", "allow_short", "commission_bps", "slippage_bps", "stop_loss_pct",
                         "take_profit_pct", "trailing_stop_pct", "position_size"],
            "properties": {
                "capital": _nullable("number"), "allow_short": _nullable("boolean"),
                "commission_bps": _nullable("number"), "slippage_bps": _nullable("number"),
                "stop_loss_pct": _nullable("number"), "take_profit_pct": _nullable("number"),
                "trailing_stop_pct": _nullable("number"), "position_size": _nullable("number"),
            }},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}


def _strategy_doc() -> str:
    lines = []
    for s in STRATEGIES.values():
        ps = ", ".join(f"{p.name} (default {p.default})" for p in s.params) or "no params"
        lines.append(f"- {s.key}: {s.label}. {s.description} Params: {ps}")
    return "\n".join(lines)


PARSE_SYSTEM = f"""You convert a trader's natural-language request into a JSON spec for a backtesting engine.
The engine supports equities, indexes, commodities (futures), forex and crypto using Yahoo Finance data.

Strategies available (use exactly these keys and param names):
{_strategy_doc()}

Rules:
- symbols: Yahoo Finance tickers. Equities "AAPL", Indian NSE stocks "RELIANCE.NS", indexes "^GSPC" (S&P 500), "^NSEI" (Nifty 50), "^NSEBANK", "^BSESN", "^IXIC", "^NDX", "^DJI", "^FTSE", "^GDAXI", "^N225"; commodity futures "GC=F" (gold), "SI=F", "CL=F" (WTI crude), "BZ=F", "NG=F", "HG=F", "ZC=F", "ZW=F", "KC=F"; forex "EURUSD=X", "USDINR=X", "GBPUSD=X", "USDJPY=X"; crypto "BTC-USD".
- intent: "backtest" for one strategy on one symbol; "compare" for several strategies and/or symbols, or when the user asks which strategy works best (then include every strategy except buy_hold); "analyze" when the user asks whether an instrument is a good investment without naming a strategy.
- evaluate: true when the user asks whether something is good, worth it, or advisable.
- If no strategy is named and the request is not an investment question, use intent "compare" with every strategy.
- Translate trading jargon: golden cross = sma_cross 50/200; turtle = donchian 20/10; "200 DMA filter" = price_sma 200; "12-month momentum" = momentum lookback 252.
- Only include params the user specified; the engine fills defaults.
- start/end: ISO dates (YYYY-MM-DD) or null. Today is {{today}}. If no period is given use 10 years before today (2 years for 1h).
- config values are null unless the user specified them. Percentages stay as percent numbers (2% stop -> 2). Commission/slippage in basis points (0.1% -> 10). position_size is a fraction 0-1.
- notes: short statements of any assumptions you made, written for the user.
"""


def parse(prompt: str) -> dict | None:
    if not enabled():
        return None
    sys_prompt = PARSE_SYSTEM.replace("{today}", date.today().isoformat())
    raw = _call(sys_prompt, prompt, effort="low", schema=SPEC_SCHEMA, max_tokens=4000)
    if not raw:
        return None
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        return None
    spec["strategies"] = [{"key": s["key"], "params": {p["name"]: p["value"] for p in s.get("params", [])}}
                          for s in spec.get("strategies", []) if s.get("key") in STRATEGIES]
    spec["config"] = {k: v for k, v in (spec.get("config") or {}).items() if v is not None}
    spec["parser"] = "claude"
    return spec


# ---------------------------------------------------------------------------------- commentary
COMMENTARY_SYSTEM = """You are a quantitative research analyst writing for a retail trader.
You receive computed backtest and market statistics in JSON. Write a concise, balanced assessment in Markdown:
- Lead with a one-sentence bottom line.
- Then "What the data shows": 3-5 bullets citing the specific numbers.
- Then "Risks and caveats": 2-4 bullets (overfitting, regime dependence, costs, drawdowns, sample size, data limits).
- Then "What would make this stronger": 1-3 bullets.
Rules: rely only on the numbers provided; never invent data. Be candid about weak results. Do not promise future returns.
Avoid personalised financial advice wording, but do give a clear evidence-based view. Keep it under 300 words."""


def commentary(question: str, payload: dict) -> str | None:
    if not enabled():
        return None
    user = f"User question: {question}\n\nComputed results (JSON):\n{json.dumps(payload, default=str)[:60000]}"
    return _call(COMMENTARY_SYSTEM, user, effort="medium", max_tokens=8000)
