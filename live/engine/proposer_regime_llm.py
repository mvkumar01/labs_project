"""Daily SENSEX regime from overnight news via an LLM (DeepSeek by default).

Port of the Pramanaa market-predictor daily layer (feeds.py, news_fetch.py, scorer.daily_regime and
its base-rate anchor, commit 62ef7d4), with the Anthropic call swapped for an OpenAI-compatible
chat-completions call. The prompt, JSON schema, feed list and per-feed depth are unchanged so the
output is comparable with Pramanaa's Sonnet regime.

The API key comes from the DEEPSEEK_API_KEY environment variable (PA: config/live_env.json) and is
never logged. Any failure raises; callers fall back to the opening-gap rule.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from email.utils import parsedate_to_datetime
from typing import Callable, Optional
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from live.engine.proposer_predictor import Regime

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("PROPOSER_REGIME_MODEL", "deepseek-chat")
INDEX_NAME = "BSE SENSEX (Indian broad-market equity index)"
DAY_THR = 0.0015                     # day label threshold used for the base rates (validator's)

FEEDS = {
    "INDIA_MARKETS": [
        ("ET Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
        ("Moneycontrol Mkts", "https://www.moneycontrol.com/rss/marketreports.xml"),
        ("Moneycontrol Biz", "https://www.moneycontrol.com/rss/business.xml"),
        ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
        ("LiveMint Markets", "https://www.livemint.com/rss/markets"),
    ],
    "GLOBAL_MARKETS": [
        ("CNBC World", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362"),
        ("MarketWatch Top", "http://feeds.marketwatch.com/marketwatch/topstories/"),
        ("Investing News", "https://www.investing.com/rss/news.rss"),
    ],
    "CENTRAL_BANK": [
        ("ET Economy", "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms"),
        ("CNBC Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ],
    "MACRO_DATA": [
        ("CNBC Finance", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    ],
    "ENERGY": [
        ("OilPrice", "https://oilprice.com/rss/main"),
        ("CNBC Energy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19836768"),
    ],
    "GEOPOLITICAL": [
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("CNBC World Politics", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000113"),
    ],
    "BANKING": [
        ("ET Banking/Finance", "https://economictimes.indiatimes.com/industry/banking/finance/rssfeeds/13358259.cms"),
        ("ET Banking", "https://economictimes.indiatimes.com/industry/banking/finance/banking/rssfeeds/13358319.cms"),
    ],
}
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, */*",
    "Accept-Language": "en-IN,en;q=0.9",
}


# ─────────────────────────────────────────────────────────────── news fetch ──
def _parse_time(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            d = dt.datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _text(el, *tags):
    for t in tags:
        c = el.find(t)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_feed(raw: bytes) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    items = root.findall(".//item")
    if items:
        return [{"title": _text(i, "title"), "link": _text(i, "link"),
                 "published": _parse_time(_text(i, "pubDate", "{http://purl.org/dc/elements/1.1/}date"))}
                for i in items]
    ns = "{http://www.w3.org/2005/Atom}"
    out = []
    for e in root.findall(f".//{ns}entry"):
        le = e.find(f"{ns}link")
        out.append({"title": _text(e, f"{ns}title"), "link": le.get("href", "") if le is not None else "",
                    "published": _parse_time(_text(e, f"{ns}updated", f"{ns}published"))})
    return out


def fetch_news(limit_per_feed: Optional[int] = None, now: Optional[dt.datetime] = None,
               get=None) -> tuple[list[dict], list[str]]:
    """All feeds, newest first, de-duplicated. Monday widens the depth to cover the weekend.
    Returns (items, skipped feed names)."""
    now = now or dt.datetime.now()
    limit = limit_per_feed or (50 if now.weekday() == 0 else 25)
    get = get or (lambda url: urlopen(Request(url, headers=_HEADERS), timeout=8).read())
    seen, items, skipped = set(), [], []
    for cat, feeds in FEEDS.items():
        for name, url in feeds:
            try:
                raw = get(url)
            except Exception:
                skipped.append(name)
                continue
            for it in parse_feed(raw)[:limit]:
                title = it.get("title", "")
                key = title.lower()[:120]
                if not title or key in seen:
                    continue
                seen.add(key)
                items.append({**it, "category": cat, "source": name})
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    items.sort(key=lambda x: x.get("published") or floor, reverse=True)
    return items, skipped


# ────────────────────────────────────────────────────────────────── prompt ──
def _headlines(items, n=40) -> str:
    lines = []
    for it in items[:n]:
        p = it["published"].strftime("%m-%d %H:%M") if it.get("published") else "  ?  "
        lines.append(f"- [{it.get('category', '')}] {p}  {it.get('title', '')}")
    return "\n".join(lines)


SYSTEM = (
    f"You are a markets analyst scoring the directional REGIME for the {INDEX_NAME} "
    "for the upcoming/current session, from news. Output a multi-day directional PRIOR, not a precise "
    "forecast. Weigh: global/US/Asia overnight moves, GIFT-Nifty cues, crude/energy, geopolitics/war, "
    "central-bank signals, India macro. A regime is a BIAS that raises the base rate of one side — it is "
    "NOT a guarantee; intraday consolidation can still occur. Be calibrated and willing to say 'neutral' "
    "when cues are mixed. Respond with ONLY a JSON object, no prose."
)
SCHEMA = (
    '{"regime":"bullish|bearish|neutral|risk_off","p_bull":0-1,"p_bear":0-1,"p_chop":0-1,'
    '"confidence":0-1,"trigger":"<short HUMAN-READABLE phrase for the dominant driver, plain '
    'words with spaces (e.g. Crude spike vs Asia rally) — never snake_case>",'
    '"key_drivers":[{"factor":"<e.g. crude, US-Iran, Fed>","value":"<what/how much>","impact":"bull|bear|neutral"}],'
    '"rationale":"<2-3 sentences>"}'
)


def base_rates_text(base_rates: Optional[dict]) -> str:
    if not base_rates:
        return ""
    return (f"\n\nREALIZED BASE RATES for this index (last {base_rates['n']} sessions): "
            f"{round(base_rates['bull'] * 100)}% of days closed bull, "
            f"{round(base_rates['bear'] * 100)}% bear, {round(base_rates['chop'] * 100)}% chop. "
            "Anchor your probabilities near these unless today's evidence is SPECIFIC and NEW. "
            "Genuine risk_off regimes are RARE: ambient/standing geopolitical stories (an ongoing "
            "conflict, yesterday's crude move, re-reported tensions) are ALREADY PRICED and do NOT "
            "justify risk_off or a heavy bearish tilt — reserve risk_off for a fresh, extraordinary "
            "shock that clearly escalated within the last session. When cues are mixed, say neutral.")


def base_rates_from_days(days: list[tuple[float, float]], thr: float = DAY_THR) -> Optional[dict]:
    """days: [(first close, last close)] for the recent sessions (Pramanaa uses the last 60)."""
    rets = [c1 / c0 - 1 for c0, c1 in days if c0]
    if not rets:
        return None
    n = len(rets)
    return {"n": n, "bull": sum(r > thr for r in rets) / n, "bear": sum(r < -thr for r in rets) / n,
            "chop": sum(abs(r) <= thr for r in rets) / n}


def user_prompt(items, base_rates=None) -> str:
    return (f"Today's news (newest first):\n{_headlines(items)}{base_rates_text(base_rates)}\n\n"
            f"Return the {INDEX_NAME} regime prior as JSON exactly matching:\n{SCHEMA}\n"
            "p_bull+p_bear+p_chop must sum to ~1.")


# ──────────────────────────────────────────────────────────────── providers ──
def deepseek_call(system: str, user: str, *, key: Optional[str] = None, model: str = DEEPSEEK_MODEL,
                  max_tokens: int = 1200, timeout: int = 60) -> str:
    key = key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    body = json.dumps({"model": model, "max_tokens": max_tokens, "temperature": 0.0,
                       "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = Request(DEEPSEEK_URL, data=body, headers={"Authorization": f"Bearer {key}",
                                                    "Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    return resp["choices"][0]["message"]["content"]


def _extract_json(text: str) -> dict:
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise ValueError(f"no JSON in model output: {text[:200]}")
    return json.loads(text[s:e + 1])


def daily_regime(items: list[dict], base_rates: Optional[dict] = None,
                 call: Callable[[str, str], str] = deepseek_call) -> dict:
    """The model's regime JSON (regime, p_bull/p_bear/p_chop, confidence, trigger, drivers...)."""
    if not items:
        raise ValueError("no headlines to score")
    out = _extract_json(call(SYSTEM, user_prompt(items, base_rates)))
    regime = str(out.get("regime") or "").lower()
    if regime not in ("bullish", "bearish", "neutral", "risk_off"):
        raise ValueError(f"unexpected regime {regime!r}")
    out["regime"] = regime
    return out


def to_regime(out: dict, source: str = "deepseek") -> Regime:
    def num(k, default):
        try:
            return float(out.get(k))
        except (TypeError, ValueError):
            return default
    return Regime(out["regime"], num("p_bull", 1 / 3), num("p_bear", 1 / 3), num("p_chop", 1 / 3),
                  num("confidence", 0.0), source)
