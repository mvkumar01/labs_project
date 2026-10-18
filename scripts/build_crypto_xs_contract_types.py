"""Snapshot Binance's contract labels for the crypto-only twin book (run from a host the Binance API answers;
the labs server gets HTTP 451). Writes data/crypto_xs_contract_types.json: {as_of, source, symbols:
{symbol: {underlyingType, contractType, status, onboardDate}}}. Re-run when Binance lists new TradFi
perpetuals; until then the twin judges unlabelled contracts by their weekend volume.

    python scripts/build_crypto_xs_contract_types.py [exchangeInfo.json]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


def main() -> None:
    if len(sys.argv) > 1:
        info = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
            info = json.loads(r.read())
    symbols = {s["symbol"]: {k: s.get(k) for k in ("underlyingType", "contractType", "status", "onboardDate")}
               for s in info["symbols"] if s.get("quoteAsset") == "USDT"}
    as_of = datetime.fromtimestamp(info["serverTime"] / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    out = {"as_of": as_of, "source": URL, "symbols": dict(sorted(symbols.items()))}
    path = ROOT / "data" / "crypto_xs_contract_types.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    kinds: dict = {}
    for v in symbols.values():
        kinds[v["underlyingType"]] = kinds.get(v["underlyingType"], 0) + 1
    print(f"{len(symbols)} USDT contracts as of {as_of}: {kinds}")


if __name__ == "__main__":
    main()
