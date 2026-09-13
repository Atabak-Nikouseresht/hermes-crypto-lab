from datetime import datetime, timezone

import ccxt
import pandas as pd
import pytest

from src.paper_broker import PaperConfig, PaperTradingSystem
from src.paper_market import TransientPublicMarketError, fetch_public_market_snapshot


def test_dry_run_is_ineligible_for_forward_baseline(tmp_path):
    assets = ("BTC/USDT", "ETH/USDT")
    specification = '{"locked_strategy":{"allocation":{"asset_caps":{"BTC/USDT":0.7,"ETH/USDT":0.6}}}}'
    system = PaperTradingSystem(tmp_path / "paper.duckdb", PaperConfig(assets=assets))
    now = datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('experiment', ?, 'locked', 'hash', 'governance', ?, 'ACTIVE')",
            [now, specification],
        )
        connection.execute(
            "INSERT INTO paper_runs VALUES (?, ?, ?, 'DRY_RUN', 'DRY_RUN', FALSE, FALSE, NULL, NULL, NULL, NULL, '', '{}', 1)",
            ["dry", now, now],
        )
        connection.execute(
            "INSERT INTO equity_snapshots VALUES ('equity', 'dry', ?, 100, 0, 100, ?)",
            [system.store.account_id, now],
        )
        connection.execute(
            "INSERT INTO forward_market_observations VALUES ('dry', ?, 'BTC/USDT', 100)",
            [now],
        )

    with pytest.raises(ValueError, match="ineligible forward run"):
        system.store.ensure_forward_baseline(run_id="dry")
    assert not system.store.forward_baseline_eligible(run_id="dry")
    with system.store.connect(read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM forward_baselines").fetchone()[0] == 0

    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs VALUES (?, ?, ?, 'RECOVERED_ABORTED', 'PAPER', TRUE, FALSE, NULL, NULL, NULL, NULL, '', '{}', 1)",
            ["recovered", now, now],
        )
        connection.execute(
            "INSERT INTO equity_snapshots VALUES ('recovered-equity', 'recovered', ?, 100, 0, 100, ?)",
            [system.store.account_id, now],
        )
    with pytest.raises(ValueError, match="recovered run"):
        system.store.ensure_recovered_forward_baseline(run_id="recovered")

    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_runs VALUES (?, ?, ?, 'EXECUTED', 'PAPER', TRUE, FALSE, ?, NULL, NULL, NULL, '', '{\"valid\":true}', 1)",
            ["official", now, now, "2026-01-05T00:05Z"],
        )
        connection.execute(
            "INSERT INTO equity_snapshots VALUES ('official-equity', 'official', ?, 100, 0, 100, ?)",
            [system.store.account_id, now],
        )
        connection.execute(
            "INSERT INTO forward_market_observations VALUES ('official', ?, 'BTC/USDT', 100)",
            [now],
        )
    system.store.record_forward_details(
        run_id="official", outcome="PAPER_TRADE_COMPLETED", diagnostics={},
        observed_prices={}, observed_at=now, kill_switch_active=False, reconciliation_valid=True,
    )
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO paper_execution_outcomes VALUES ('official', 'FULL_EXECUTION', ?)", [now]
        )
    assert not system.store.forward_baseline_eligible(run_id="official")
    with pytest.raises(ValueError, match="complete governed market observations"):
        system.store.ensure_forward_baseline(run_id="official")
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_market_observations VALUES ('official', ?, 'ETH/USDT', 100)",
            [now],
        )
        connection.execute(
            "INSERT INTO forward_market_observations VALUES ('official', ?, 'DOGE/USDT', 100)",
            [now],
        )
    assert not system.store.forward_baseline_eligible(run_id="official")
    with pytest.raises(ValueError, match="complete governed market observations"):
        system.store.ensure_forward_baseline(run_id="official")
    with system.store.connect() as connection:
        connection.execute(
            "DELETE FROM forward_market_observations WHERE run_id='official' AND symbol='DOGE/USDT'"
        )
    assert system.store.forward_baseline_eligible(run_id="official")
    system.store.ensure_forward_baseline(run_id="official")


@pytest.mark.parametrize(
    "invalid",
    [
        None, "FAILED", "KILL_SWITCH", "KILL_SWITCH_ACTIVATED", "RECONCILIATION_FAILURE",
        "DATA_QUALITY_FAILURE", "DATA_HALT", "ABORTED", "RECOVERED_ABORTED",
        "EXECUTION_ERROR", "ERROR", "RELEASE_PROVENANCE_FAILURE", "RUNNING", "UNKNOWN",
        "dry_run", "unofficial", "unscheduled", "incomplete", "bad_reconciliation",
        "missing_reconciliation", "bad_execution", "missing_execution", "bad_diagnostics",
        "kill_switch", "bad_diagnostic_reconciliation", "missing_diagnostics",
        "missing_equity", "missing_observation",
    ],
)
def test_forward_baseline_requires_healthy_completed_observation(tmp_path, invalid):
    system = PaperTradingSystem(tmp_path / "paper.duckdb", PaperConfig(assets=("BTC/USDT",)))
    now = datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc)
    with system.store.connect() as connection:
        connection.execute(
            "INSERT INTO forward_experiments VALUES "
            "('experiment', ?, 'locked', 'hash', 'governance', ?, 'ACTIVE')",
            [now, '{"locked_strategy":{"allocation":{"asset_caps":{"BTC/USDT":0.7}}}}'],
        )
        connection.execute(
            "INSERT INTO paper_runs "
            "(run_id, started_at_utc, completed_at_utc, status, mode, official_scheduled, "
            "schedule_key, reconciliation) VALUES "
            "('official', ?, ?, 'EXECUTED', 'PAPER', TRUE, '2026-01-05T00:05Z', '{\"valid\":true}')",
            [now, now],
        )
        connection.execute(
            "INSERT INTO paper_execution_outcomes VALUES ('official', 'FULL_EXECUTION', ?)", [now]
        )
        connection.execute(
            "INSERT INTO equity_snapshots VALUES ('equity', 'official', ?, 100, 0, 100, ?)",
            [system.store.account_id, now],
        )
    system.store.record_forward_details(
        run_id="official", outcome="PAPER_TRADE_COMPLETED", diagnostics={},
        observed_prices={"BTC/USDT": 100}, observed_at=now,
        kill_switch_active=False, reconciliation_valid=True,
    )
    mutations = {
        "dry_run": "UPDATE paper_runs SET mode='DRY_RUN'",
        "unofficial": "UPDATE paper_runs SET official_scheduled=FALSE",
        "unscheduled": "UPDATE paper_runs SET schedule_key=NULL",
        "incomplete": "UPDATE paper_runs SET completed_at_utc=NULL",
        "bad_reconciliation": "UPDATE paper_runs SET reconciliation='{\"valid\":false}'",
        "missing_reconciliation": "UPDATE paper_runs SET reconciliation='{}'",
        "bad_execution": "UPDATE paper_execution_outcomes SET execution_outcome='ERROR'",
        "missing_execution": "DELETE FROM paper_execution_outcomes",
        "bad_diagnostics": "UPDATE paper_run_diagnostics SET outcome='EXECUTION_ERROR'",
        "kill_switch": "UPDATE paper_run_diagnostics SET kill_switch_active=TRUE",
        "bad_diagnostic_reconciliation": "UPDATE paper_run_diagnostics SET reconciliation_valid=FALSE",
        "missing_diagnostics": "DELETE FROM paper_run_diagnostics",
        "missing_equity": "DELETE FROM equity_snapshots",
        "missing_observation": "DELETE FROM forward_market_observations",
    }
    with system.store.connect() as connection:
        if invalid in mutations:
            connection.execute(mutations[invalid])
        elif invalid is not None:
            connection.execute("UPDATE paper_runs SET status=?", [invalid])
    assert system.store.forward_baseline_eligible(run_id="official") is (invalid is None)
    if invalid is not None:
        with pytest.raises(ValueError):
            system.store.ensure_forward_baseline(run_id="official")
        with system.store.connect(read_only=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM forward_baselines").fetchone()[0] == 0
    else:
        system.store.ensure_forward_baseline(run_id="official")
        with system.store.connect(read_only=True) as connection:
            baseline = connection.execute("SELECT * FROM forward_baselines").fetchall()
        assert len(baseline) == 1
        assert baseline[0][1] == "official"
        system.store.ensure_forward_baseline(run_id="does-not-exist")
        with system.store.connect(read_only=True) as connection:
            assert connection.execute("SELECT * FROM forward_baselines").fetchall() == baseline


def test_retry_exhausted_public_transport_failure_is_typed_retryable():
    class FailingExchange:
        def load_markets(self):
            raise ccxt.NetworkError("temporary public network outage")

        def close(self):
            return None

    with pytest.raises(TransientPublicMarketError, match="temporary public network outage"):
        fetch_public_market_snapshot(
            PaperConfig(assets=("BTC/USDT",)),
            exchange=FailingExchange(),
            now=pd.Timestamp("2026-01-05T00:10:00Z").to_pydatetime(),
            max_retries=0,
        )
