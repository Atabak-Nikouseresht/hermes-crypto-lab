from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from run_paper import (
    _experiment_start,
    _project_paths,
    _verify_research_lock,
    load_paper_configuration,
)
from src.config import load_settings
from src.forward_monthly import generate_monthly_forward_report
from src.forward_governance import bootstrap_forward_experiment
from src.forward_operations import InterProcessLock, record_missed_windows
from src.paper_broker import PaperTradingSystem
from src.paper_notifications import HermesTelegramSender


def main() -> None:
    settings = load_settings()
    config, values = load_paper_configuration(settings.project_root)
    database_path, _weekly_reports = _project_paths(settings.project_root, values)
    system = PaperTradingSystem(database_path, config)
    _verify_research_lock(settings.project_root, config)
    bootstrap_forward_experiment(system.store, settings.project_root, config)
    governance = json.loads(
        (settings.project_root / "forward_experiment" / "governance.json").read_text(
            encoding="utf-8"
        )
    )
    now = datetime.now(timezone.utc)
    with InterProcessLock(settings.project_root / "runtime" / "paper_monthly.lock"):
        record_missed_windows(
            system.store,
            start=_experiment_start(settings.project_root),
            now=now,
            config=config,
        )
        output_dir = settings.project_root / "reports" / "forward_monthly"
        month_start = (
            pd.Timestamp(now).tz_convert("UTC").normalize().replace(day=1)
            - pd.offsets.MonthBegin(1)
        )
        existing = output_dir / f"forward_monthly_{month_start.strftime('%Y-%m')}.md"
        if existing.exists():
            report_path = existing
        else:
            result = generate_monthly_forward_report(
                system.store,
                experiment_id=governance["experiment_id"],
                report_date=now,
                output_dir=output_dir,
                assets=config.assets,
                slippage_rate=config.slippage_rate,
            )
            report_path = result["report_path"]
        HermesTelegramSender()(str(values["telegram_target"]), Path(report_path))
        print(f"Forward-only monthly report delivered: {report_path}")


if __name__ == "__main__":
    main()
