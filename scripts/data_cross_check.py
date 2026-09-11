"""Deterministic public-data cross-check; never replaces the Binance dataset."""

from __future__ import annotations

from datetime import datetime, timezone
import csv
import io
import json
import math
from pathlib import Path
import urllib.request
import zipfile

import pandas as pd
import yaml

from src.research_data import resolve_canonical_dataset

SAMPLES = ("2019-04-07", "2023-04-09", "2025-11-02")



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
    # Binance archives use milliseconds historically and microseconds in newer files.
    requested = pd.Timestamp(day, tz="UTC")
    if int(row[0]) not in (requested.value // 1_000_000, requested.value // 1_000):
        raise ValueError("Archive timestamp does not match requested sample")
    return float(row[4])


def run(project_root: Path, output_path: Path) -> dict:
    # Read governance independently of pointer contents and environment overrides.
    try:
        governance = yaml.safe_load((project_root / "config" / "assets.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError("Cannot read governed assets from config/assets.yaml") from error
    if not isinstance(governance, dict) or governance.get("assets") is None:
        raise ValueError("Invalid governed assets in config/assets.yaml")
    governed_assets = governance["assets"]
    canonical_paths, canonical_provenance = resolve_canonical_dataset(
        project_root / "data" / "processed", expected_assets=governed_assets
    )
    archive_checks = []
    for asset, path in canonical_paths.items():
        symbol = asset.replace("/", "")
        frame = pd.read_parquet(path)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        indexed = frame.set_index("timestamp")
        for day in SAMPLES:
            check = {
                "asset": asset,
                "symbol": symbol,
                "date_utc": day,
                "timestamp_utc": None,
                "processed_close": None,
                "binance_vision_close": None,
                "relative_error": None,
                "within_tolerance": False,
                "status": "INSUFFICIENT_DATA",
            }
            archive_checks.append(check)
            timestamp = pd.Timestamp(day, tz="UTC")
            if timestamp not in indexed.index:
                check["reason"] = "requested_sample_not_in_canonical_history"
                continue
            if indexed.index.has_duplicates:
                check.update(status="FAIL_CLOSED", reason="duplicate_canonical_timestamps")
                continue
            check["timestamp_utc"] = indexed.index[indexed.index.get_loc(timestamp)].isoformat()
            try:
                expected = float(indexed.loc[timestamp, "close"])
                if not math.isfinite(expected) or expected <= 0:
                    raise ValueError("invalid canonical close")
                check["processed_close"] = expected
                observed = _vision_close(symbol, day)
                if not math.isfinite(observed) or observed <= 0:
                    raise ValueError("invalid archive close")
                relative_error = abs(observed - expected) / expected
                if not math.isfinite(relative_error):
                    raise ValueError("invalid relative error")
            except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration, zipfile.BadZipFile):
                # Do not leak absolute local paths or transport exception payloads.
                check.update(status="FAIL_CLOSED", reason="archive_comparison_unavailable_or_invalid")
                continue
            check.update(
                binance_vision_close=observed,
                relative_error=relative_error,
                within_tolerance=relative_error <= 1e-10,
                status="PASS" if relative_error <= 1e-10 else "FAIL_CLOSED",
            )

    independent = []
    for coin, symbol in (("bitcoin", "BTCUSDT"), ("ethereum", "ETHUSDT")):
        if f"{symbol[:-4]}/USDT" not in canonical_paths:
            continue
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
            if archive_checks
            and {row["asset"] for row in archive_checks if row["within_tolerance"]} == set(governed_assets)
            and all(row["within_tolerance"] for row in archive_checks)
            and all(row["within_declared_2pct_tolerance"] for row in independent)
            else "FAIL_CLOSED"
        ),
        "replacement_performed": False,
        "governed_assets": sorted(governed_assets),
        "canonical_provenance": canonical_provenance,
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
