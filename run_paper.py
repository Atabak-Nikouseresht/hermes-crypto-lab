from __future__ import annotations

import argparse
from datetime import datetime, timezone
import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.config import load_assets, load_settings
from src.logging_config import configure_logging
from src.paper_broker import PaperConfig, PaperTradingSystem
from src.paper_market import fetch_public_market_snapshot
from src.paper_report import write_weekly_paper_report

LOGGER = logging.getLogger(__name__)


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean: {value}")


def load_paper_configuration(project_root: Path) -> tuple[PaperConfig, dict]:
    load_dotenv(project_root / ".env", override=False)
    payload = yaml.safe_load(
        (project_root / "config" / "strategy.yaml").read_text(encoding="utf-8")
    )
    values = payload["paper_trading"]
    if not values.get("enabled"):
        raise PermissionError("Paper trading is disabled")
    if payload.get("live_trading_enabled"):
        raise PermissionError("Live trading must remain disabled")
    assets = tuple(load_assets(project_root / "config" / "assets.yaml"))
    config = PaperConfig(
        assets=assets,
        initial_cash=float(values["initial_cash"]),
        fee_rate=float(values["fee_rate"]),
        minimum_spread_rate=float(values["minimum_spread_rate"]),
        slippage_rate=float(values["slippage_rate"]),
        schedule_weekday=int(values["schedule_weekday"]),
        schedule_hour=int(values["schedule_hour"]),
        schedule_minute=int(values["schedule_minute"]),
        schedule_window_minutes=int(values["schedule_window_minutes"]),
        max_data_staleness_minutes=int(values["max_data_staleness_minutes"]),
        max_quote_staleness_minutes=int(values["max_quote_staleness_minutes"]),
        locked_candidate_id=str(values["locked_candidate_id"]),
    )
    return config, values


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Public-data-only persistent paper trading; never sends exchange orders"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Propose only; persist no trades")
    mode.add_argument("--paper", action="store_true", help="Persist virtual paper fills")
    parser.add_argument(
        "--reset-kill-switch",
        action="store_true",
        help="Reset only after persistent state reconciles",
    )
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.logs_dir, settings.log_level)
    config, values = load_paper_configuration(settings.project_root)
    database_value = Path(os.getenv("HCL_PAPER_DATABASE", values["database_path"]))
    database_path = (
        database_value if database_value.is_absolute() else settings.project_root / database_value
    )
    reports_value = Path(values["reports_dir"])
    reports_dir = (
        reports_value if reports_value.is_absolute() else settings.project_root / reports_value
    )
    system = PaperTradingSystem(database_path, config)
    now = datetime.now(timezone.utc)

    if args.reset_kill_switch:
        system.store.reset_kill_switch(now=now)
        print("Paper-trading kill switch reset after successful reconciliation")
        return

    if args.dry_run:
        dry_run = True
    elif args.paper:
        dry_run = False
    else:
        dry_run = _boolean(
            os.getenv("HCL_PAPER_DRY_RUN", str(values["default_dry_run"]))
        )

    LOGGER.info(
        "Fetching public market data only; mode=%s locked=%s",
        "DRY_RUN" if dry_run else "PAPER",
        config.locked_candidate_id,
    )
    try:
        snapshot = fetch_public_market_snapshot(
            config,
            exchange_id=str(values["exchange"]),
            now=now,
            lookback_days=int(values["lookback_days"]),
        )
    except Exception as error:
        reason = f"Public market-data fetch failed: {error}"
        system.store.activate_kill_switch(reason, run_id=None, now=now)
        LOGGER.exception(reason)
        raise SystemExit(2) from error

    result = system.run(snapshot, now=now, dry_run=dry_run)
    report_path = write_weekly_paper_report(
        system.store, result, snapshot, reports_dir, now=snapshot.fetched_at
    )
    print(f"Status: {result.status}")
    print(f"Message: {result.message}")
    print(f"Weekly report: {report_path}")
    if result.status == "KILL_SWITCH":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
