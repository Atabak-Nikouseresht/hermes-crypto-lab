from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    project_root: Path
    exchange: str
    timeframe: str
    since: str
    fetch_limit: int
    max_retries: int
    backoff_base_seconds: float
    request_timeout_ms: int
    assets_config: Path
    database_path: Path
    log_level: str

    @property
    def raw_dir(self) -> Path:
        return self.project_root / "data" / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.project_root / "data" / "processed"

    @property
    def reports_dir(self) -> Path:
        return self.project_root / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.project_root / "logs"


def load_settings(project_root: Path | None = None) -> Settings:
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    load_dotenv(root / ".env", override=False)

    def project_path(variable: str, default: str) -> Path:
        value = Path(os.getenv(variable, default))
        return value if value.is_absolute() else root / value

    return Settings(
        project_root=root,
        exchange=os.getenv("HCL_EXCHANGE", "binance"),
        timeframe=os.getenv("HCL_TIMEFRAME", "1d"),
        since=os.getenv("HCL_SINCE", "2017-01-01T00:00:00Z"),
        fetch_limit=int(os.getenv("HCL_FETCH_LIMIT", "1000")),
        max_retries=int(os.getenv("HCL_MAX_RETRIES", "5")),
        backoff_base_seconds=float(os.getenv("HCL_BACKOFF_BASE_SECONDS", "1.0")),
        request_timeout_ms=int(os.getenv("HCL_REQUEST_TIMEOUT_MS", "30000")),
        assets_config=project_path("HCL_ASSETS_CONFIG", "config/assets.yaml"),
        database_path=project_path("HCL_DATABASE_PATH", "database/trading.duckdb"),
        log_level=os.getenv("HCL_LOG_LEVEL", "INFO").upper(),
    )


def load_assets(path: Path) -> list[str]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assets = payload.get("assets", [])
    if not assets or not all(isinstance(symbol, str) for symbol in assets):
        raise ValueError(f"No valid assets configured in {path}")
    return assets
