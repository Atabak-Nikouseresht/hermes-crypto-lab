from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

from src.config import Settings, load_assets, load_settings
from src.execution_protocol import EXECUTION_PROTOCOL_VERSION
from src.forward_governance import bootstrap_forward_experiment, verify_trust_anchors
from src.forward_operations import (
    AlreadyRunningError,
    InterProcessLock,
    audit_missed_schedule,
    record_missed_windows,
)
from src.logging_config import configure_logging
from src.paper_broker import MarketSnapshot, PaperConfig, PaperTradingSystem
from src.paper_forward import (
    build_forward_diagnostics,
    commit_operational_failure,
    finalize_forward_run,
    recover_committed_forward_evidence,
)
from src.paper_market import fetch_public_market_snapshot
from src.paper_notifications import (
    HermesTelegramSender,
    NotificationError,
    NotificationService,
)
from src.paper_report import write_operational_failure_report, write_weekly_paper_report
from src.release_provenance import capture_release_provenance

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
    if values.get("execution_protocol_version") != EXECUTION_PROTOCOL_VERSION:
        raise PermissionError("Configured execution protocol differs from code-locked protocol")
    assets = tuple(load_assets(project_root / "config" / "assets.yaml"))
    config = PaperConfig.from_locked_candidate(
        assets=assets,
        locked_candidate_id=str(values["locked_candidate_id"]),
        initial_cash=float(values["initial_cash"]),
        accounting_currency=str(values["accounting_currency"]),
        exchange_id=str(values["exchange"]),
        lookback_days=int(values["lookback_days"]),
        fee_rate=float(values["fee_rate"]),
        minimum_spread_rate=float(values["minimum_spread_rate"]),
        slippage_rate=float(values["slippage_rate"]),
        schedule_weekday=int(values["schedule_weekday"]),
        schedule_hour=int(values["schedule_hour"]),
        schedule_minute=int(values["schedule_minute"]),
        execution_target_minute=int(values["execution_target_minute"]),
        schedule_window_minutes=int(values["schedule_window_minutes"]),
        max_data_staleness_minutes=int(values["max_data_staleness_minutes"]),
        max_quote_staleness_minutes=int(values["max_quote_staleness_minutes"]),
        require_exchange_rules=bool(values["require_exchange_rules"]),
    )
    return config, values


def fetch_configured_public_market_snapshot(
    config: PaperConfig, settings: Settings
) -> MarketSnapshot:
    """Fetch paper-market data with the typed runtime transport policy."""
    return fetch_public_market_snapshot(
        config,
        exchange_id=config.exchange_id,
        lookback_days=config.lookback_days,
        max_retries=settings.max_retries,
        backoff_base_seconds=settings.backoff_base_seconds,
        timeout_ms=settings.request_timeout_ms,
    )


def _project_paths(project_root: Path, values: dict) -> tuple[Path, Path]:
    database_value = Path(os.getenv("HCL_PAPER_DATABASE", values["database_path"]))
    database_path = (
        database_value if database_value.is_absolute() else project_root / database_value
    )
    reports_value = Path(values["reports_dir"])
    reports_dir = reports_value if reports_value.is_absolute() else project_root / reports_value
    return database_path, reports_dir


def _verify_research_lock(project_root: Path, config: PaperConfig) -> str:
    return verify_trust_anchors(project_root, config)["locked_strategy"]


def resolve_telegram_target(command_line_target: str | None) -> str:
    """Resolve the notification target without repository-stored identifiers."""
    target = command_line_target or os.getenv("HCL_TELEGRAM_TARGET")
    if not target:
        raise ValueError(
            "Telegram delivery requires HCL_TELEGRAM_TARGET or --telegram-target"
        )
    return target


@contextmanager
def open_locked_system(
    *,
    database_path: Path,
    config: PaperConfig,
    project_root: Path,
    lock_path: Path,
    command_name: str,
    bootstrap: bool = True,
):
    """Acquire the global writer lock before opening or recovering DuckDB."""
    with InterProcessLock(
        lock_path, timeout_seconds=5, command_name=command_name
    ):
        system = PaperTradingSystem(database_path, config)
        if bootstrap:
            bootstrap_forward_experiment(system.store, project_root, config)
        yield system


def _experiment_start(project_root: Path) -> pd.Timestamp:
    governance = project_root / "forward_experiment" / "governance.json"
    if not governance.exists():
        return pd.Timestamp.now(tz="UTC")
    payload = json.loads(governance.read_text(encoding="utf-8"))
    return pd.Timestamp(payload["experiment_start_utc"]).tz_convert("UTC")


def _current_schedule_window_closed(
    now: datetime | pd.Timestamp, config: PaperConfig
) -> bool:
    current = pd.Timestamp(now)
    if current.tzinfo is None:
        raise ValueError("Schedule audit time must be timezone-aware")
    current = current.tz_convert("UTC")
    window_end = current.normalize() + pd.Timedelta(
        hours=config.schedule_hour,
        minutes=config.schedule_minute + config.schedule_window_minutes,
    )
    return current.weekday() == config.schedule_weekday and current > window_end


def _schedule_window_deadline(
    schedule_key: str | None, config: PaperConfig
) -> pd.Timestamp | None:
    if schedule_key is None:
        return None
    window_start = pd.Timestamp(schedule_key)
    if window_start.tzinfo is None:
        raise ValueError("Schedule key must be timezone-aware")
    return window_start.tz_convert("UTC") + pd.Timedelta(
        minutes=config.schedule_window_minutes
    )


def _latest_schedule_key(now: datetime | pd.Timestamp, config: PaperConfig) -> str:
    current = pd.Timestamp(now)
    if current.tzinfo is None:
        raise ValueError("Schedule audit time must be timezone-aware")
    current = current.tz_convert("UTC")
    days_since_schedule = (current.weekday() - config.schedule_weekday) % 7
    window_start = current.normalize() - pd.Timedelta(days=days_since_schedule)
    window_start += pd.Timedelta(
        hours=config.schedule_hour,
        minutes=config.schedule_minute,
    )
    if window_start > current:
        window_start -= pd.Timedelta(days=7)
    return window_start.strftime("%Y-%m-%dT%H:%MZ")


def _send_sample(target: str, reports_dir: Path, config: PaperConfig) -> Path:
    now = pd.Timestamp.now(tz="UTC")
    path = reports_dir / f"telegram_sample_{now.strftime('%Y%m%dT%H%M%SZ')}.md"
    text = "\n".join(
        [
            "# SAMPLE — Hermes Crypto Lab Forward Paper Report",
            "",
            f"- Timestamp UTC: `{now.isoformat()}`",
            f"- Timestamp Europe/Rome: `{now.tz_convert('Europe/Rome').isoformat()}`",
            f"- Locked candidate: `{config.locked_candidate_id}`",
            "- Outcome: **SAMPLE_NOTIFICATION_ONLY**",
            "- Portfolio state changed: **no**",
            "- Strategy executed: **no**",
            "- All transactions are virtual; this message contains no trade.",
            "",
        ]
    )
    reports_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    HermesTelegramSender()(target, path)
    return path


def _status(system: PaperTradingSystem) -> dict:
    account = system.store.account()
    positions = system.store.positions()
    reconciliation = system.store.reconcile()
    with system.store.connect(read_only=True) as connection:
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM paper_orders), (SELECT COUNT(*) FROM paper_fills), "
            "(SELECT COUNT(*) FROM forward_incidents WHERE resolved_at_utc IS NULL), "
            "(SELECT COUNT(*) FROM paper_notifications WHERE status='FAILED')"
        ).fetchone()
    return {
        "account": account,
        "positions": positions,
        "orders": int(counts[0]),
        "fills": int(counts[1]),
        "open_forward_incidents": int(counts[2]),
        "failed_notifications": int(counts[3]),
        "reconciliation": {
            "valid": reconciliation.valid,
            "message": reconciliation.message,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Public-data-only persistent paper trading; never sends exchange orders"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Propose only; persist no trades")
    mode.add_argument("--paper", action="store_true", help="Persist virtual paper fills")
    mode.add_argument("--reset-kill-switch", action="store_true")
    mode.add_argument(
        "--audit-missed",
        action="store_true",
        help="Audit missed schedule only; no market fetch or strategy execution",
    )
    mode.add_argument(
        "--startup-audit",
        action="store_true",
        help="Record every missing window since forward start; no market fetch",
    )
    mode.add_argument("--resend", metavar="RUN_ID", help="Retry Telegram only; never execute strategy")
    mode.add_argument("--status", action="store_true", help="Print persistent forward status as JSON")
    mode.add_argument("--reconcile", action="store_true", help="Run reconciliation only")
    mode.add_argument(
        "--kill-switch-status",
        action="store_true",
        help="Inspect kill-switch state and incidents without resetting it",
    )
    mode.add_argument(
        "--sample-telegram",
        action="store_true",
        help="Send a sample report without fetching data or changing paper state",
    )
    parser.add_argument("--telegram-target", default=None)
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings.logs_dir, settings.log_level)
    config, values = load_paper_configuration(settings.project_root)
    database_path, reports_dir = _project_paths(settings.project_root, values)
    _verify_research_lock(settings.project_root, config)
    writer_lock = settings.project_root / "runtime" / "forward_writer.lock"

    if args.status:
        with open_locked_system(
            database_path=database_path,
            config=config,
            project_root=settings.project_root,
            lock_path=writer_lock,
            command_name="status",
        ) as system:
            print(json.dumps(_status(system), indent=2, sort_keys=True))
        return
    if args.reconcile:
        with open_locked_system(
            database_path=database_path,
            config=config,
            project_root=settings.project_root,
            lock_path=writer_lock,
            command_name="reconcile",
        ) as system:
            result = system.store.reconcile()
            print(json.dumps({"valid": result.valid, "message": result.message}, indent=2))
            if not result.valid:
                raise SystemExit(2)
        return
    if args.kill_switch_status:
        with open_locked_system(
            database_path=database_path,
            config=config,
            project_root=settings.project_root,
            lock_path=writer_lock,
            command_name="kill-switch-status",
        ) as system:
            with system.store.connect(read_only=True) as connection:
                incidents = connection.execute(
                    """
                    SELECT incident_id, reason, created_at_utc, cleared_at_utc
                    FROM paper_incidents ORDER BY created_at_utc DESC
                    """
                ).fetchall()
            print(
                json.dumps(
                    {
                        "account_status": system.store.account()["status"],
                        "automatic_reset": False,
                        "incidents": [
                            {
                                "incident_id": row[0],
                                "reason": row[1],
                                "created_at_utc": str(row[2]),
                                "cleared_at_utc": str(row[3]) if row[3] else None,
                            }
                            for row in incidents
                        ],
                    },
                    indent=2,
                )
            )
        return
    if args.reset_kill_switch:
        with open_locked_system(
            database_path=database_path,
            config=config,
            project_root=settings.project_root,
            lock_path=writer_lock,
            command_name="kill-switch-reset",
        ) as system:
            system.store.reset_kill_switch(now=datetime.now(timezone.utc))
        print("Paper-trading kill switch reset after successful reconciliation")
        return
    if args.resend:
        with open_locked_system(
            database_path=database_path,
            config=config,
            project_root=settings.project_root,
            lock_path=writer_lock,
            command_name="telegram-resend",
        ) as system:
            service = NotificationService(
                system.store, target="", sender=HermesTelegramSender()
            )
            service.resend(args.resend)
        print(f"Notification resent for run {args.resend}; strategy was not executed")
        return
    if args.sample_telegram:
        telegram_target = resolve_telegram_target(args.telegram_target)
        path = _send_sample(telegram_target, reports_dir, config)
        print(f"Sample Telegram report delivered: {path}")
        return
    if args.audit_missed or args.startup_audit:
        telegram_target = resolve_telegram_target(args.telegram_target)
        with open_locked_system(
            database_path=database_path,
            config=config,
            project_root=settings.project_root,
            lock_path=writer_lock,
            command_name="missed-window-audit",
        ) as system:
            now = datetime.now(timezone.utc)
            result = audit_missed_schedule(
                system.store,
                start=_experiment_start(settings.project_root),
                now=now,
                config=config,
            )
            if result is None:
                print("No missed schedule required recording")
                return
            report_path = write_operational_failure_report(
                system.store,
                result,
                reports_dir,
                now=pd.Timestamp(now),
                locked_candidate_id=config.locked_candidate_id,
            )
            NotificationService(
                system.store, target=telegram_target, sender=HermesTelegramSender()
            ).send_committed_run(result.run_id, report_path)
            print(f"MISSED_SCHEDULE recorded and delivered: {result.run_id}")
        return

    if args.dry_run:
        dry_run = True
    elif args.paper:
        dry_run = False
    else:
        dry_run = _boolean(
            os.getenv("HCL_PAPER_DRY_RUN", str(values["default_dry_run"]))
        )

    try:
        telegram_target: str | None = None
        with open_locked_system(
            database_path=database_path,
            config=config,
            project_root=settings.project_root,
            lock_path=writer_lock,
            command_name="scheduled-paper-run",
        ) as system:
            now = datetime.now(timezone.utc)
            start = _experiment_start(settings.project_root)
            if not dry_run:
                telegram_target = resolve_telegram_target(args.telegram_target)
            recovered = recover_committed_forward_evidence(
                system,
                now=now,
                reports_dir=reports_dir,
                notification_target=telegram_target,
            )
            if recovered:
                LOGGER.warning(
                    "Recovered %s committed run(s) without replaying paper fills",
                    recovered,
                )
            record_missed_windows(system.store, start=start, now=now, config=config)
            schedule_key = system._scheduled_key(pd.Timestamp(now))
            schedule_deadline = _schedule_window_deadline(schedule_key, config)
            current_window_missed = _current_schedule_window_closed(now, config)
            if current_window_missed:
                print(
                    "Status: MISSED_SCHEDULE — current UTC window is closed; "
                    "no market fetch, signal calculation, equity snapshot, or trade was performed"
                )
                return
            if schedule_key is None:
                latest_schedule_key = _latest_schedule_key(now, config)
                if system.store.forward_window_exists(
                    latest_schedule_key
                ) or system.store.schedule_exists(latest_schedule_key):
                    print(
                        "Status: DUPLICATE_SCHEDULE — latest governed UTC window "
                        f"{latest_schedule_key} is already finalized"
                    )
                    return
            if (
                not dry_run
                and schedule_key is not None
                and (
                    system.store.schedule_exists(schedule_key)
                    or system.store.forward_window_exists(schedule_key)
                )
            ):
                print(f"Status: DUPLICATE_SCHEDULE — {schedule_key} is already finalized")
                return

            release_provenance = None
            official_scheduled = not dry_run and schedule_key is not None
            if official_scheduled:
                try:
                    release_provenance = capture_release_provenance(settings.project_root)
                except RuntimeError as error:
                    result = commit_operational_failure(
                        system,
                        outcome="RELEASE_PROVENANCE_FAILURE",
                        message=str(error),
                        now=now,
                        official_scheduled=True,
                    )
                    report_path = write_operational_failure_report(
                        system.store,
                        result,
                        reports_dir,
                        now=pd.Timestamp(now),
                        locked_candidate_id=config.locked_candidate_id,
                    )
                    if telegram_target is not None:
                        NotificationService(
                            system.store,
                            target=telegram_target,
                            sender=HermesTelegramSender(),
                        ).send_committed_run(result.run_id, report_path)
                    raise SystemExit(2) from error

            if system.store.account()["status"] != "ACTIVE":
                result = commit_operational_failure(
                    system,
                    outcome="KILL_SWITCH_ACTIVATED",
                    message="Persistent kill switch is active; no trade attempted",
                    now=now,
                    release_provenance=release_provenance,
                    official_scheduled=official_scheduled,
                )
                report_path = write_operational_failure_report(
                    system.store,
                    result,
                    reports_dir,
                    now=pd.Timestamp(now),
                    locked_candidate_id=config.locked_candidate_id,
                )
                if telegram_target is not None:
                    NotificationService(
                        system.store,
                        target=telegram_target,
                        sender=HermesTelegramSender(),
                    ).send_committed_run(result.run_id, report_path)
                raise SystemExit(2)

            LOGGER.info(
                "Fetching public market data only; mode=%s locked=%s",
                "DRY_RUN" if dry_run else "PAPER",
                config.locked_candidate_id,
            )
            try:
                snapshot = fetch_configured_public_market_snapshot(config, settings)
            except Exception as error:
                reason = f"Public market-data fetch failed: {error}"
                result = commit_operational_failure(
                    system,
                    outcome="DATA_QUALITY_FAILURE",
                    message=reason,
                    now=now,
                    release_provenance=release_provenance,
                    official_scheduled=official_scheduled,
                )
                report_path = write_operational_failure_report(
                    system.store,
                    result,
                    reports_dir,
                    now=pd.Timestamp(now),
                    locked_candidate_id=config.locked_candidate_id,
                )
                if telegram_target is not None:
                    try:
                        NotificationService(
                            system.store,
                            target=telegram_target,
                            sender=HermesTelegramSender(),
                        ).send_committed_run(result.run_id, report_path)
                    except NotificationError:
                        LOGGER.exception("Telegram failed after committed data-quality failure")
                LOGGER.exception(reason)
                raise SystemExit(2) from error

            execution_now = datetime.now(timezone.utc)
            if schedule_deadline is not None and pd.Timestamp(execution_now) > schedule_deadline:
                record_missed_windows(
                    system.store,
                    start=start,
                    now=execution_now,
                    config=config,
                )
                print(
                    "Status: MISSED_SCHEDULE — UTC window closed during market-data "
                    "retrieval; no signal calculation, equity snapshot, or trade was performed"
                )
                return
            snapshot_is_valid = system._validate_snapshot(
                snapshot, pd.Timestamp(execution_now).tz_convert("UTC")
            ) is None
            diagnostics = (
                build_forward_diagnostics(system, snapshot)
                if schedule_key and snapshot_is_valid
                else {}
            )
            result = system.run(
                snapshot,
                now=execution_now,
                dry_run=dry_run,
                forward_diagnostics=diagnostics if schedule_key else None,
                release_provenance=release_provenance,
                require_release_provenance=not dry_run and schedule_key is not None,
                official_scheduled=official_scheduled,
            )
            result = finalize_forward_run(
                system,
                result,
                snapshot,
                now=execution_now,
                diagnostics=diagnostics,
                outcome_override=None,
            )
            report_path = write_weekly_paper_report(
                system.store, result, snapshot, reports_dir, now=snapshot.fetched_at
            )
            if telegram_target is not None:
                try:
                    NotificationService(
                        system.store,
                        target=telegram_target,
                        sender=HermesTelegramSender(),
                    ).send_committed_run(result.run_id, report_path)
                except NotificationError as error:
                    LOGGER.exception(
                        "Telegram failed after committed run %s; retry with --resend %s",
                        result.run_id,
                        result.run_id,
                    )
                    print(f"Committed run {result.run_id}; Telegram failed: {error}")
                    print(f"Retry only: run_paper.py --resend {result.run_id}")
                    raise SystemExit(3) from error
            print(f"Status: {result.status}")
            print(f"Outcome: {result.outcome}")
            print(f"Message: {result.message}")
            print(f"Weekly report: {report_path}")
            if result.outcome in {
                "KILL_SWITCH_ACTIVATED",
                "DATA_QUALITY_FAILURE",
                "RECONCILIATION_FAILURE",
                "EXECUTION_ERROR",
            }:
                raise SystemExit(2)
    except AlreadyRunningError as error:
        LOGGER.error("Overlapping paper execution refused: %s", error)
        raise SystemExit(4) from error


if __name__ == "__main__":
    main()
