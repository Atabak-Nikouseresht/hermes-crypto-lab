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
            "INSERT INTO paper_runs VALUES (?, ?, ?, 'EXECUTED', 'PAPER', TRUE, FALSE, ?, NULL, NULL, NULL, '', '{}', 1)",
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
