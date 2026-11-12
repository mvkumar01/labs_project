"""Asset navigation and the allowlist for Spotlight and Watch List tiles."""
from labs.services.book_overview import BOOKS
from labs.services.book_overview_more import MORE_BOOKS

# Shared selections until an administrator saves a custom list.
DEFAULT_SPOTLIGHT = ("nifty_omega", "bn_selective_guard", "bn_selective", "proposer_nifty_scalper_v5")

GROUPS = {
    "crypto": ("Crypto", "Bitcoin, Ethereum and crypto strategies"),
    "commodities": ("Commodities", "Gold and crude oil strategies"),
    "nifty": ("Nifty", "NIFTY options and index strategies"),
    "banknifty": ("Bank Nifty", "BANKNIFTY options and index strategies"),
    "sensex": ("Sensex", "SENSEX options and index strategies"),
    "other": ("Other assets", "Shares and mixed-asset strategies"),
}


def asset_for(card):
    name = str(card.get("instrument", "")).upper().replace("BANK NIFTY", "BANKNIFTY")
    key = card["key"]
    if key in {"risk_core", "expiry_strangle"} or ("NIFTY" in name and "SENSEX" in name):
        return "other"
    if "BANKNIFTY" in name:
        return "banknifty"
    if key.startswith(("crypto_", "btc_")) or "BINANCE" in name:
        return "crypto"
    if "CRUDE" in name or "GOLD" in name:
        return "commodities"
    if "NIFTY" in name:
        return "nifty"
    if "SENSEX" in name:
        return "sensex"
    return "other"


def build_navigation(cards, asset=None, spotlight_keys=DEFAULT_SPOTLIGHT):
    selected = asset if asset in GROUPS else None
    groups = []
    for key, (label, description) in GROUPS.items():
        members = [c for c in cards if asset_for(c) == key]
        # Dedicated asset tiles stay visible before their books are ready.
        if members or key != "other":
            groups.append(dict(key=key, label=label, description=description, cards=members,
                               count=len(members)))
    by_key = {c["key"]: c for c in cards}
    return dict(groups=groups, asset=selected, label=GROUPS[selected][0] if selected else "Overview",
                cards=[c for c in cards if asset_for(c) == selected] if selected else cards,
                spotlight=[by_key[k] for k in spotlight_keys if k in by_key])


def strategy_keys():
    # Dry/live status cards are part of the public catalog too; they contain no account figures.
    return set(BOOKS) | set(MORE_BOOKS) | {"dry_gold", "dry_crudem", "live_nifty", "live_proposer", "live_gold"}


def validate_keys(value):
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(k, str) for k in value):
        raise ValueError("Choose existing strategy tiles (at most 100).")
    allowed = strategy_keys()
    if any(k not in allowed for k in value):
        raise ValueError("One or more strategies are no longer available. Refresh the page and try again.")
    return list(dict.fromkeys(value))
