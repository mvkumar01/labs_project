"""Replay-safe one-minute data providers for NIFTY and cached equities."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, time
import json
from pathlib import Path

import pandas as pd
import pytz

from config.labs_config import DATA_DIR, SHARED_ARCHIVE_DIR, SHARED_LIVE_DIR
from labs.simulation.config import (
    INSTRUMENTS,
    SIMULATION_DATA_DIR,
    SIMULATION_KITE_CONFIG,
    SIMULATION_KITE_TOKEN,
    ensure_private_dirs,
)


IST = pytz.timezone("Asia/Kolkata")
OHLCV = ["open", "high", "low", "close", "volume"]


class MarketDataUnavailable(RuntimeError):
    pass


class MarketDataProvider(ABC):
    @abstractmethod
    def load_day(self, instrument: str, trade_date: str) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def available_dates(self, instrument: str) -> list[str]:
        raise NotImplementedError


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    ts_col = next((c for c in ("timestamp", "date", "datetime", "time") if c in frame), None)
    if ts_col:
        frame["timestamp"] = pd.to_datetime(frame[ts_col], errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).set_index("timestamp")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        raise MarketDataUnavailable("Market data has no timestamp column")
    if frame.index.tz is not None:
        frame.index = frame.index.tz_convert(IST).tz_localize(None)
    missing = [c for c in ("open", "high", "low", "close") if c not in frame]
    if missing:
        raise MarketDataUnavailable(f"Market data missing columns: {', '.join(missing)}")
    if "volume" not in frame:
        frame["volume"] = 0
    for col in OHLCV:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.between_time("09:15", "15:30", inclusive="left")
    return frame[OHLCV]


class LabsNiftyProvider(MarketDataProvider):
    """Read NIFTY from the existing Labs/shared store without duplicating it."""

    def _candidates(self, trade_date: str) -> tuple[Path, ...]:
        return (
            DATA_DIR / f"{trade_date}_NIFTY_spot_1min.csv",
            DATA_DIR / trade_date / "NIFTY_spot_1min.csv",
            SHARED_LIVE_DIR / trade_date / "NIFTY_spot_1min.csv",
            SHARED_ARCHIVE_DIR / trade_date / "NIFTY_spot_1min.parquet.zst",
            SHARED_ARCHIVE_DIR / trade_date / "NIFTY_spot_1min.parquet.gz",
            SHARED_ARCHIVE_DIR / trade_date / "NIFTY_spot_1min.parquet",
        )

    def load_day(self, instrument: str, trade_date: str) -> pd.DataFrame:
        if instrument != "NIFTY":
            raise MarketDataUnavailable("LabsNiftyProvider supports NIFTY only")
        for path in self._candidates(trade_date):
            if path.is_file():
                frame = pd.read_parquet(path) if ".parquet" in "".join(path.suffixes) else pd.read_csv(path)
                result = normalize_frame(frame)
                result.attrs.update(source="existing_labs_nifty", source_path=str(path))
                return result
        raise FileNotFoundError(f"No existing NIFTY one-minute data for {trade_date}")

    def available_dates(self, instrument: str) -> list[str]:
        if instrument != "NIFTY":
            return []
        dates = set()
        for root in (DATA_DIR, SHARED_LIVE_DIR, SHARED_ARCHIVE_DIR):
            if not root.exists():
                continue
            for path in root.glob("**/*NIFTY_spot_1min*"):
                for candidate in (path.parent.name, path.name[:10]):
                    try:
                        dates.add(datetime.strptime(candidate, "%Y-%m-%d").date().isoformat())
                    except ValueError:
                        pass
        return sorted(dates, reverse=True)


class KiteEquityProvider(MarketDataProvider):
    """Fetch equities through the simulator's separate Kite app and cache CSV."""

    def __init__(self, data_dir: Path = SIMULATION_DATA_DIR):
        self.data_dir = Path(data_dir)
        ensure_private_dirs()

    def cache_path(self, instrument: str, trade_date: str) -> Path:
        return self.data_dir / instrument / f"{trade_date}.csv"

    def available_dates(self, instrument: str) -> list[str]:
        root = self.data_dir / instrument
        return sorted((p.stem for p in root.glob("*.csv")), reverse=True) if root.exists() else []

    def load_day(self, instrument: str, trade_date: str) -> pd.DataFrame:
        path = self.cache_path(instrument, trade_date)
        if not path.is_file():
            self.fetch_day(instrument, trade_date)
        result = normalize_frame(pd.read_csv(path))
        result.attrs.update(source="simulation_kite_cache", source_path=str(path))
        return result

    def fetch_day(self, instrument: str, trade_date: str) -> Path:
        meta = INSTRUMENTS.get(instrument)
        if not meta or meta["kind"] != "equity":
            raise MarketDataUnavailable(f"Unsupported equity instrument: {instrument}")
        kite = simulation_kite()
        token = self._instrument_token(kite, meta["exchange"], meta["kite_symbol"])
        day = datetime.strptime(trade_date, "%Y-%m-%d").date()
        start = IST.localize(datetime.combine(day, time(9, 15)))
        end = IST.localize(datetime.combine(day, time(15, 30)))
        bars = kite.historical_data(token, start, end, "minute", continuous=False, oi=False)
        if not bars:
            raise MarketDataUnavailable(f"Kite returned no candles for {instrument} {trade_date}")
        frame = normalize_frame(pd.DataFrame(bars)).reset_index()
        frame = frame.rename(columns={frame.columns[0]: "timestamp"})
        path = self.cache_path(instrument, trade_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False)
        return path

    @staticmethod
    def _instrument_token(kite, exchange: str, symbol: str) -> int:
        for row in kite.instruments(exchange):
            if row.get("tradingsymbol") == symbol:
                return int(row["instrument_token"])
        raise MarketDataUnavailable(f"Kite instrument not found: {exchange}:{symbol}")


class CompositeMarketDataProvider(MarketDataProvider):
    def __init__(self):
        self.nifty = LabsNiftyProvider()
        self.equities = KiteEquityProvider()

    def _provider(self, instrument: str) -> MarketDataProvider:
        return self.nifty if instrument == "NIFTY" else self.equities

    def load_day(self, instrument: str, trade_date: str) -> pd.DataFrame:
        return self._provider(instrument).load_day(instrument, trade_date)

    def available_dates(self, instrument: str) -> list[str]:
        return self._provider(instrument).available_dates(instrument)


def simulation_kite():
    """Build the separate simulator Kite session; never reads Labs' token."""
    if not SIMULATION_KITE_CONFIG.is_file():
        raise MarketDataUnavailable("Simulation Kite API key is not configured")
    if not SIMULATION_KITE_TOKEN.is_file():
        raise MarketDataUnavailable("Connect Simulation Kite to create today's access token")
    config = json.loads(SIMULATION_KITE_CONFIG.read_text(encoding="utf-8-sig"))
    token = json.loads(SIMULATION_KITE_TOKEN.read_text(encoding="utf-8-sig"))
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=config["api_key"])
    kite.set_access_token(token["access_token"])
    return kite


def kite_login_url() -> str:
    if not SIMULATION_KITE_CONFIG.is_file():
        raise MarketDataUnavailable("Simulation Kite API key is not configured")
    from kiteconnect import KiteConnect

    config = json.loads(SIMULATION_KITE_CONFIG.read_text(encoding="utf-8-sig"))
    return KiteConnect(api_key=config["api_key"]).login_url()


def exchange_request_token(request_token: str) -> dict:
    from kiteconnect import KiteConnect

    config = json.loads(SIMULATION_KITE_CONFIG.read_text(encoding="utf-8-sig"))
    kite = KiteConnect(api_key=config["api_key"])
    session = kite.generate_session(request_token, api_secret=config["api_secret"])
    payload = {
        "access_token": session["access_token"],
        "created_at": datetime.now(IST).isoformat(),
    }
    SIMULATION_KITE_TOKEN.parent.mkdir(parents=True, exist_ok=True)
    SIMULATION_KITE_TOKEN.write_text(json.dumps(payload), encoding="utf-8")
    return {"created_at": payload["created_at"]}
