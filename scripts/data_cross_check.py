"""Deterministic public-data cross-check; never replaces the Binance dataset."""

from __future__ import annotations

from datetime import datetime, timezone
import csv
import io
import json
from pathlib import Path
import urllib.request
import zipfile

import pandas as pd

SAMPLES = ("2019-04-07", "2023-04-09", "2025-11-02")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "TRXUSDT")


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-crypto-lab-audit/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _vision_close(symbol: str, day: str) -> float:
    url = (
        "https://data.binance.vision/data/spot/daily/klines/"
        f"{symbol}/1d/{symbol}-1d-{day}.zip"
    )
    payload = _get(url)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = archive.namelist()[0]
        row = next(csv.reader(io.TextIOWrapper(archive.open(name), encoding="utf-8")))
    return float(row[4])


def run(project_root: Path, output_path: Path) -> dict:
    archive_checks = []
    for symbol in SYMBOLS:
        frame = pd.read_parquet(
            project_root / "data" / "processed" / f"{symbol[:-4]}_USDT_1d.parquet"
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        indexed = frame.set_index("timestamp")
        for day in SAMPLES:
            expected = float(indexed.loc[pd.Timestamp(day, tz="UTC"), "close"])
            observed = _vision_close(symbol, day)
            relative_error = abs(observed - expected) / expected
            archive_checks.append(
                {
                    "symbol": symbol,
                    "date_utc": day,
                    "processed_close": expected,
                    "binance_vision_close": observed,
                    "relative_error": relative_error,
                    "within_tolerance": relative_error <= 1e-10,
                }
            )

    independent = []
    for coin, symbol in (("bitcoin", "BTCUSDT"), ("ethereum", "ETHUSDT")):
        payload = json.loads(
            _get(
                "https://api.coingecko.com/api/v3/coins/"
                f"{coin}/market_chart?vs_currency=usd&days=3&interval=daily"
            )
        )
        cg_timestamp_ms, cg_price = payload["prices"][-2]
        cg_time = pd.to_datetime(cg_timestamp_ms, unit="ms", utc=True)
        day_start = cg_time.normalize()
        params = int(day_start.timestamp() * 1000)
        binance = json.loads(
            _get(
                "https://api.binance.com/api/v3/klines?"
                f"symbol={symbol}&interval=1d&startTime={params}&limit=1"
            )
        )
        binance_close = float(binance[0][4])
        relative_error = abs(binance_close - float(cg_price)) / float(cg_price)
        independent.append(
            {
                "symbol": symbol,
                "timestamp_utc": day_start.isoformat(),
                "binance_usdt_close": binance_close,
                "coingecko_usd_midnight_price": float(cg_price),
                "relative_error": relative_error,
                "within_declared_2pct_tolerance": relative_error <= 0.02,
                "measurement_note": "USDT daily close versus independent USD midnight observation; diagnostic only",
            }
        )

    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": (
            "PASS"
            if all(row["within_tolerance"] for row in archive_checks)
            and all(row["within_declared_2pct_tolerance"] for row in independent)
            else "FAIL_CLOSED"
        ),
        "replacement_performed": False,
        "archive_checks": archive_checks,
        "independent_checks": independent,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    result = run(root, root / "audits" / "data_cross_check.json")
    print(result["status"])
    raise SystemExit(0 if result["status"] == "PASS" else 2)
