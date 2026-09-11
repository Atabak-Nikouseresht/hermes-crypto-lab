"""Real HTTP adapter -> CLI D1 classification -> weekly retry policy."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

import pandas as pd
from src.paper_broker import PaperConfig
from functools import partial
from io import BytesIO, StringIO
import json
from pathlib import Path
import subprocess
from urllib.error import HTTPError

import pytest

import run_paper
from scripts import paper_forward_weekly as weekly
from src.download_data import call_with_retry
from src.paper_market import PublicMarketClient, fetch_public_market_snapshot
from tests.test_reference_integrity_closure import _exchange
from tests.test_runner_safety import _configure_real_data_halt_runner


def _http_runner(monkeypatch, tmp_path, status, retry_after=None):
    system, now = _configure_real_data_halt_runner(monkeypatch, tmp_path)
    requests, inner_sleeps, processes = [], [], []

    def request(*args, **kwargs):
        requests.append(args)
        raise HTTPError("https://api.binance.com", status, "error",
                        {} if retry_after is None else {"Retry-After": retry_after},
                        BytesIO(b'{"code":-1121}'))

    monkeypatch.setattr("src.paper_market.urlopen", request)
    monkeypatch.setattr("src.paper_market.call_with_retry", partial(call_with_retry, sleep=inner_sleeps.append))
    monkeypatch.setattr(run_paper, "fetch_configured_public_market_snapshot", lambda config, settings:
                        fetch_public_market_snapshot(config, exchange=PublicMarketClient(_exchange()),
                                                     now=now, max_retries=2, backoff_base_seconds=3))

    def process(command, **kwargs):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), pytest.raises(SystemExit) as exc:
            run_paper.main()
        completed = subprocess.CompletedProcess(command, exc.value.code, stdout.getvalue(), stderr.getvalue())
        processes.append(completed)
        return completed

    monkeypatch.setattr(weekly.subprocess, "run", process)
    return system, now, requests, inner_sleeps, processes


def _assert_unexecuted(system):
    assert system.store.account()["status"] == "ACTIVE"
    with system.store.connect(read_only=True) as connection:
        for table in ("paper_orders", "paper_fills", "forward_market_observations", "paper_incidents", "equity_snapshots"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("SELECT DISTINCT schedule_key FROM paper_runs").fetchall() == [(None,)]
        assert connection.execute("SELECT DISTINCT attempted_schedule_key FROM paper_runs").fetchall() == [("2026-01-05T00:05Z",)]


@pytest.mark.parametrize("status", [418, 429])
@pytest.mark.parametrize("header,expected", [("120", 120), ("0", 0), (None, None), ("NaN", None)])
def test_rate_limit_actual_cli_suppresses_entire_weekly_invocation(monkeypatch, tmp_path, status, header, expected):
    system, now, requests, inner_sleeps, processes = _http_runner(monkeypatch, tmp_path, status, header)
    outer_sleeps = []
    assert weekly.main(now, clock=lambda: now, sleeper=outer_sleeps.append,
                       python_resolver=lambda _: Path("test-python")) == 4
    assert len(requests) == len(processes) == 1
    assert inner_sleeps == outer_sleeps == []
    marker = next(json.loads(line) for line in processes[0].stdout.splitlines() if line.startswith('{"event":'))
    assert marker == {"event": "PUBLIC_MARKET_RATE_LIMIT_DEFER", "http_status": status,
                      "retry_after_seconds": expected, "retry_policy": "suppress_remaining_weekly_attempts"}
    _assert_unexecuted(system)

    # Suppression is invocation-local, not a new persistent scheduler lock.
    next_week = now + timedelta(days=7)
    assert weekly.main(next_week, clock=lambda: next_week, sleeper=outer_sleeps.append,
                       python_resolver=lambda _: Path("test-python")) == 4
    assert len(requests) == len(processes) == 2
    assert inner_sleeps == outer_sleeps == []


@pytest.mark.parametrize("status", [500, 502])
def test_5xx_exhaustion_actual_d1_preserves_inner_count_and_normal_outer_retry(monkeypatch, tmp_path, status):
    system, now, requests, inner_sleeps, processes = _http_runner(monkeypatch, tmp_path, status)
    outer_sleeps = []
    assert weekly.main(now, clock=lambda: now, sleeper=outer_sleeps.append,
                       python_resolver=lambda _: Path("test-python")) == 4
    assert len(processes) == 3
    assert len(requests) == 9
    assert inner_sleeps == [3, 6] * 3
    assert outer_sleeps == [60, 60]
    assert all(p.returncode == 4 and "PUBLIC_MARKET_RATE_LIMIT_DEFER" not in p.stdout for p in processes)
    _assert_unexecuted(system)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_http_actual_cli_is_terminal_without_outer_retry(monkeypatch, tmp_path, status):
    system, now, requests, inner_sleeps, processes = _http_runner(monkeypatch, tmp_path, status)
    outer_sleeps = []
    assert weekly.main(now, clock=lambda: now, sleeper=outer_sleeps.append,
                       python_resolver=lambda _: Path("test-python")) == 2
    assert len(requests) == len(processes) == 1
    assert inner_sleeps == outer_sleeps == []
    _assert_unexecuted(system)


@pytest.mark.parametrize("status", [418, 429])
@pytest.mark.parametrize("stream", ["output", "stderr"])
@pytest.mark.parametrize("as_bytes", [False, True], ids=["text", "bytes"])
def test_timeout_after_rate_limit_marker_suppresses_weekly_retries(monkeypatch, capsys, status, stream, as_bytes):
    now = datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc)
    marker = {"event": "PUBLIC_MARKET_RATE_LIMIT_DEFER", "http_status": status,
              "retry_after_seconds": 1200, "retry_policy": "suppress_remaining_weekly_attempts"}
    captured = "ordinary diagnostic\n{partial json\n[]\n" + json.dumps(marker) + "\nunfinished report"
    # TimeoutExpired captures bytes even when subprocess.run uses text=True.
    if as_bytes:
        captured = b"\xff diagnostic\n" + captured.encode("utf-8")
    calls, sleeps = [], []

    def process(command, **kwargs):
        calls.append(command)
        assert kwargs["text"] is True
        streams = {"output": "ordinary stdout", "stderr": b"ordinary stderr"}
        streams[stream] = captured
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], **streams)

    monkeypatch.setattr(weekly.subprocess, "run", process)
    assert weekly.main(now, clock=lambda: now, sleeper=sleeps.append,
                       python_resolver=lambda _: Path("test-python")) == 124
    assert len(calls) == 1
    assert sleeps == []
    assert json.dumps(marker) in capsys.readouterr().err


@pytest.mark.parametrize("captured", [None, "ordinary diagnostic", b"ordinary diagnostic",
                                     b"\xff\n{partial json\n[]\n",
                                     json.dumps({"event": "PUBLIC_MARKET_RATE_LIMIT_DEFER",
                                                 "http_status": 500,
                                                 "retry_policy": "suppress_remaining_weekly_attempts"}),
                                     json.dumps({"event": "PUBLIC_MARKET_RATE_LIMIT_DEFER",
                                                 "http_status": 429})])
def test_timeout_without_valid_rate_limit_marker_preserves_normal_retries(monkeypatch, captured):
    now = datetime(2026, 1, 5, 0, 10, tzinfo=timezone.utc)
    calls, sleeps = [], []

    def process(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=captured, stderr=captured)

    monkeypatch.setattr(weekly.subprocess, "run", process)
    assert weekly.main(now, clock=lambda: now, sleeper=sleeps.append,
                       python_resolver=lambda _: Path("test-python")) == 124
    assert len(calls) == 3
    assert sleeps == [60, 60]


def test_clock1_exact_user_reference_03_receipt_03_100(monkeypatch):
    start = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
    source = start + timedelta(seconds=3)
    receipt = source + timedelta(milliseconds=100)

    class ReceiptClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return receipt

    monkeypatch.setattr("src.paper_market.datetime", ReceiptClock)
    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=("BTC/USDT",)), exchange=_exchange(source), now=start,
    )
    evidence = snapshot.rule_reference_prices["BTC/USDT"]
    assert evidence.timestamp == pd.Timestamp("2024-08-05T00:10:03Z")
    assert evidence.acquired_at == pd.Timestamp("2024-08-05T00:10:03.100Z")
    assert snapshot.fetched_at == pd.Timestamp(start)


def test_clock5_later_asset_source_after_snapshot_start_is_valid():
    start = datetime(2024, 8, 5, 0, 10, tzinfo=timezone.utc)
    symbols = ("BTC/USDT", "ETH/USDT")
    source_times = dict(zip(symbols, (start, start + timedelta(seconds=3)), strict=True))
    exchange = _exchange()
    exchange.fetch_reference_price = lambda symbol: {
        "symbol": symbol.replace("/", ""), "referencePrice": "100",
        "timestamp": int(pd.Timestamp(source_times[symbol]).timestamp() * 1000),
    }
    receipts = iter((start + timedelta(milliseconds=100), start + timedelta(seconds=3, milliseconds=100)))
    snapshot = fetch_public_market_snapshot(
        PaperConfig(assets=symbols), exchange=exchange, now=start,
        acquisition_clock=lambda: next(receipts),
    )
    later = snapshot.rule_reference_prices["ETH/USDT"]
    assert snapshot.fetched_at < later.timestamp < later.acquired_at
    assert later.timestamp == pd.Timestamp("2024-08-05T00:10:03Z")
    assert later.acquired_at == pd.Timestamp("2024-08-05T00:10:03.100Z")
