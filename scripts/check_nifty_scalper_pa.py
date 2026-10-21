"""Read-only PA preflight. Never print the API token or mutate PA state."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "live/engine/proposer_engine.py", "live/engine/proposer_strike.py",
    "live/proposer_runner.py", "live/live_executor.py", "labs/ui/live_routes.py",
    "labs/engine/proposer_nifty_tracker.py", "templates/live_configure.html",
    "labs/engine/proposer_px_tracker.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", nargs="+", default=["2026-10-13", "2026-10-16", "2026-10-19", "2026-10-21"])
    parser.add_argument("--spots-only", action="store_true", help="Fetch prior-session spot warmup only")
    parser.add_argument("--output", type=Path, default=ROOT / "storage/state/nifty_scalper_preflight")
    args = parser.parse_args()
    secret = Path.home().joinpath("Downloads/PA API Key.txt").read_text(encoding="utf-8-sig")
    match = re.search(r"\b[a-fA-F0-9]{32,80}\b", secret)
    if match is None:
        raise SystemExit("API token format not recognized")
    base = "https://www.pythonanywhere.com/api/v0/user/mvkumar01"

    def get(path):
        req = urllib.request.Request(base + "/files/path" + path,
                                     headers={"Authorization": "Token " + match.group(0)})
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()

    report = {}
    for rel in FILES:
        data = get("/home/mvkumar01/labs_project/" + rel)
        dest = args.output / "remote" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        head = subprocess.run(["git", "show", "HEAD:" + rel], cwd=ROOT,
                              capture_output=True, check=True).stdout
        equal = data.replace(b"\r\n", b"\n") == head.replace(b"\r\n", b"\n")
        report[rel] = {"equals_head": equal, "sha256": hashlib.sha256(data).hexdigest()}
        print(rel, "PA_EQUALS_HEAD" if equal else "PA_DIFFERS_FROM_HEAD")
    try:
        get("/home/mvkumar01/labs_project/config/telegram.json")
        report["telegram_present"] = True
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        report["telegram_present"] = False
    print("Telegram config present:", report["telegram_present"])
    import pandas as pd
    import io
    for day in args.days:
        for und in (() if args.spots_only else ("NIFTY", "SENSEX")):
            stem = f"{und}_options_1min"
            candidates = [f"live/{day}/{stem}.csv"] + [f"archive/{day}/{stem}.{ext}"
                for ext in ("parquet.zst", "parquet.gz", "parquet")]
            for rel in candidates:
                try:
                    data = get("/home/mvkumar01/shared_market_data/" + rel)
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
            else:
                raise RuntimeError(f"No live or archive options for {und} {day}")
            dest = args.output / "shared" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            frame = pd.read_csv(io.BytesIO(data)) if rel.endswith(".csv") else pd.read_parquet(io.BytesIO(data))
            latest = frame[frame.timestamp == frame.timestamp.max()]
            report[f"{day}_{und}"] = {"latest": str(latest.timestamp.max()),
                "expiries": sorted(latest.expiry.astype(str).unique().tolist())}
            print(day, und, report[f"{day}_{und}"], flush=True)
        try:
            data = get(f"/home/mvkumar01/labs_project/data/archive/{day}.tar.gz")
            dest = args.output / "spots/archive" / f"{day}.tar.gz"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            for und in ("NIFTY", "SENSEX"):
                name = f"{day}_{und}_spot_1min.csv"
                data = get("/home/mvkumar01/labs_project/data/live/" + name)
                dest = args.output / "spots/live" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
