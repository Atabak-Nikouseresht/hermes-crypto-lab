from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt

from src.config import Settings, load_assets, load_canonical_research_config
from src.database import (
    finish_run,
    initialize_database,
    record_dataset_metadata,
    start_run,
)
from src.download_data import create_exchange, download_daily_ohlcv
from src.logging_config import configure_logging
from src.report import write_quality_report
from src.storage import save_clean_parquet, save_json_atomic, save_raw_json
from src.validate_data import clean_ohlcv, rows_to_frame, validate_ohlcv

LOGGER = logging.getLogger(__name__)
Downloader = Callable[..., list[list[float]]]
GitProvenance = Callable[[Path], tuple[str, bool | None]]


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_provenance(project_root: Path) -> tuple[str, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            return "unavailable", None
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(project_root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return "unavailable", None


def _validate_canonical_publication(settings: Settings, assets: list[str]) -> None:
    governed_assets_path = settings.project_root / "config" / "assets.yaml"
    if settings.exchange != "binance":
        raise ValueError("Canonical research publication requires exchange binance")
    if settings.timeframe != "1d":
        raise ValueError("Canonical research publication requires timeframe 1d")
    if settings.assets_config.resolve() != governed_assets_path.resolve():
        raise ValueError("Canonical research publication requires config/assets.yaml")
    if assets != load_assets(governed_assets_path):
        raise ValueError("Canonical research publication assets differ from governed assets")


def run_pipeline(
    *,
    settings: Settings,
    assets: list[str],
    downloader: Downloader = download_daily_ohlcv,
    exchange: ccxt.Exchange | object | None = None,
    run_id: str | None = None,
    git_provenance: GitProvenance = _git_provenance,
) -> dict[str, Any]:
    _validate_canonical_publication(settings, assets)
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    initialize_database(settings.database_path)
    start_run(settings.database_path, run_id)
    market = exchange or create_exchange(settings.exchange, settings.request_timeout_ms)
    results: list[dict[str, Any]] = []
    ingestion_git_commit, git_dirty = git_provenance(settings.project_root)

    try:
        for symbol in assets:
            rows = downloader(
                market,
                symbol,
                since_iso=settings.since,
                timeframe=settings.timeframe,
                limit=settings.fetch_limit,
                max_retries=settings.max_retries,
                backoff_base_seconds=settings.backoff_base_seconds,
            )
            if not rows:
                raise RuntimeError(f"No finalized OHLCV rows returned for {symbol}")

            filename = f"{_safe_symbol(symbol)}_{settings.timeframe}"
            raw_path = settings.raw_dir / run_id / f"{filename}.json"
            parquet_path = settings.processed_dir / run_id / f"{filename}.parquet"
            save_raw_json(rows, raw_path)

            normalized = rows_to_frame(rows)
            quality = validate_ohlcv(normalized)
            if not quality.is_valid:
                raise ValueError(f"Canonical data quality validation failed for {symbol}: {quality.summary}")
            cleaned = clean_ohlcv(normalized)
            save_clean_parquet(cleaned, parquet_path)
            start_utc = cleaned["timestamp"].min().isoformat() if not cleaned.empty else None
            end_utc = cleaned["timestamp"].max().isoformat() if not cleaned.empty else None
            quality_payload = {
                **quality.summary,
                "missing_date_values_utc": quality.missing_dates,
            }
            result = {
                "symbol": symbol,
                "parquet_path": str(parquet_path),
                "sha256": _sha256(parquet_path),
                "raw_path": str(raw_path),
                "raw_sha256": _sha256(raw_path),
                "raw_rows": len(rows),
                "clean_rows": len(cleaned),
                "start_utc": start_utc,
                "end_utc": end_utc,
                "quality": quality_payload,
            }
            results.append(result)
            record_dataset_metadata(
                settings.database_path,
                run_id=run_id,
                symbol=symbol,
                timeframe=settings.timeframe,
                raw_path=str(raw_path.relative_to(settings.project_root)),
                parquet_path=str(parquet_path.relative_to(settings.project_root)),
                raw_rows=len(rows),
                clean_rows=len(cleaned),
                start_utc=start_utc,
                end_utc=end_utc,
                quality_summary=quality_payload,
            )
            LOGGER.info(
                "Stored %s: raw=%d clean=%d quality=%s",
                symbol,
                len(rows),
                len(cleaned),
                quality.summary,
            )

        dataset_manifest = {
            "manifest_schema_version": 2,
            "run_id": run_id,
            "timeframe": settings.timeframe,
            "version_manifest_path": f"{run_id}/dataset_manifest.json",
            "source": {
                "exchange_id": settings.exchange,
                "ccxt_version": ccxt.__version__,
                "since": settings.since,
                "fetch_limit": settings.fetch_limit,
                "max_retries": settings.max_retries,
                "backoff_base_seconds": settings.backoff_base_seconds,
                "request_timeout_ms": settings.request_timeout_ms,
            },
            "ingestion_git_commit": ingestion_git_commit,
            "git_dirty": git_dirty,
            "datasets": {
                result["symbol"]: {
                    "path": (
                        Path(result["parquet_path"])
                        .relative_to(settings.processed_dir)
                        .as_posix()
                    ),
                    "sha256": result["sha256"],
                    "raw_path": (
                        Path(result["raw_path"])
                        .relative_to(settings.processed_dir.parent)
                        .as_posix()
                    ),
                    "raw_sha256": result["raw_sha256"],
                    "rows": result["clean_rows"],
                    "raw_rows": result["raw_rows"],
                    "start_utc": result["start_utc"],
                    "end_utc": result["end_utc"],
                }
                for result in results
            },
        }
        version_manifest_path = save_json_atomic(
            dataset_manifest,
            settings.processed_dir / run_id / "dataset_manifest.json",
            immutable=True,
        )
        markdown_path, json_path = write_quality_report(results, settings.reports_dir, run_id)
        finish_run(settings.database_path, run_id, "completed")
        manifest_path = save_json_atomic(
            dataset_manifest, settings.processed_dir / "dataset_manifest.json"
        )
        LOGGER.info("Pipeline completed; report=%s", markdown_path)
        return {
            "run_id": run_id,
            "datasets": results,
            "dataset_manifest": str(manifest_path),
            "version_dataset_manifest": str(version_manifest_path),
            "markdown_report": str(markdown_path),
            "json_report": str(json_path),
        }
    except Exception as error:
        finish_run(settings.database_path, run_id, "failed", str(error))
        LOGGER.exception("Pipeline failed")
        raise
    finally:
        close = getattr(market, "close", None)
        if callable(close):
            close()


def main() -> None:
    canonical = load_canonical_research_config()
    settings = canonical.settings
    configure_logging(settings.logs_dir, settings.log_level)
    assets = list(canonical.assets)
    LOGGER.info("Starting public-data pipeline for %s", ", ".join(assets))
    result = run_pipeline(settings=settings, assets=assets)
    print(f"Run {result['run_id']} completed")
    print(f"Quality report: {result['markdown_report']}")


if __name__ == "__main__":
    main()
