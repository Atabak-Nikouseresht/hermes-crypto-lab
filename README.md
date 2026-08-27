# Hermes Crypto Lab

Research-only cryptocurrency quantitative lab. It downloads and validates **public Binance spot OHLCV data**, runs deterministic event-driven backtests and controlled experiments, and operates a public-data-only forward paper broker. At this commit, automated checks and source inspection found no configured private endpoint or real-order execution path. No live trading is implemented.

## Scientific status and locked result

The locked candidate is `mw120_sw00_ma150_n2_r07_v30`. Its sealed final test (2025-02-02 through 2026-08-20) produced approximately **−0.08% CAGR, 0.122 Sharpe, and 20.80% maximum drawdown**. This defensive result appears before any favorable historical baseline because it is the relevant post-selection evidence. Profitability is **unproven**; neither historical benchmark outperformance nor any fixed forward duration proves a durable edge.

The controlled research evaluated 96 training configurations and disclosed 117 candidate backtests. The final period was accessed by two deterministic runs of the same locked candidate, so only the first access is treated as canonical and the period is not described as globally untouched thereafter.

## Universe

- BTC/USDT
- ETH/USDT
- BNB/USDT
- XRP/USDT
- TRX/USDT

All timestamps are UTC. Only finalized daily candles are persisted; the current incomplete UTC day is excluded.

## Quick start (Windows)

```bash
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
cp .env.example .env                 # optional; edit only non-secret settings
.venv/Scripts/python.exe run_weekly.py
.venv/Scripts/python.exe run_backtest.py
.venv/Scripts/python.exe run_experiments.py
.venv/Scripts/python.exe run_paper.py --dry-run
.venv/Scripts/python.exe -m pytest tests -q
```

No Binance API key or secret is required. The exchange is instantiated by CCXT with `enableRateLimit=True`, a configurable timeout, and no credentials.

## Configuration

Runtime settings come from environment variables; `.env` is loaded when present and is git-ignored.

| Variable | Default | Purpose |
|---|---|---|
| `HCL_EXCHANGE` | `binance` | CCXT public exchange class |
| `HCL_TIMEFRAME` | `1d` | OHLCV timeframe |
| `HCL_SINCE` | `2017-01-01T00:00:00Z` | Earliest requested UTC timestamp |
| `HCL_FETCH_LIMIT` | `1000` | Candles requested per page |
| `HCL_MAX_RETRIES` | `5` | Retries after retryable CCXT/network errors |
| `HCL_BACKOFF_BASE_SECONDS` | `1.0` | Exponential backoff base; delays are base × 2^attempt |
| `HCL_REQUEST_TIMEOUT_MS` | `30000` | CCXT request timeout |
| `HCL_ASSETS_CONFIG` | `config/assets.yaml` | Asset-list path |
| `HCL_DATABASE_PATH` | `database/trading.duckdb` | DuckDB metadata path |
| `HCL_LOG_LEVEL` | `INFO` | Python logging level |
| `HCL_INITIAL_CASH` | `2000.0` | Starting capital measured as 2,000 USDT; no EUR equivalence is assumed |
| `HCL_FEE_RATE` | `0.001` | Proportional fee per fill |
| `HCL_SLIPPAGE_RATE` | `0.0005` | Adverse proportional execution slippage |
| `HCL_TELEGRAM_TARGET` | empty | Local notification target; never committed to the repository |

## Backtest methodology

- Common evaluation period: the first next-week bar after all five assets have enough history for the 200-day BTC filter through the latest common finalized bar.
- Weekly signal: generated on the last available bar of each ISO week.
- Execution: orders submitted from close information at time `t` are queued and filled only on the next available close at `t+1`.
- Momentum ranking: `close[t-7] / close[t-90] - 1`, so the latest seven days are excluded.
- Eligibility: positive 30-day momentum, positive skip-7-day 90-day momentum, and BTC above its trailing 200-day moving average.
- Selection: maximum two eligible assets, ranked deterministically by momentum and then symbol for ties.
- Allocation: inverse trailing 30-day annualized realized volatility, followed by BTC/ETH/individual-alt/combined-alt caps. Capped allocation is not redistributed; it remains cash.
- Execution ledger: orders, fills, positions, and cash are independent artifacts. Sells execute before buys. If fees and adverse slippage would overdraw cash, all buys are scaled proportionally.
- Constraints: long-only, no short selling, no borrowing, no leverage, and a runtime negative-cash invariant.
- Costs: fee and slippage rates are fixed from YAML/environment configuration; no parameter search is performed.
- Benchmarks: BTC buy-and-hold, five-asset equal-weight static allocation, 50% BTC/50% ETH static allocation, and 100% cash. Static benchmark orders are submitted at the common analysis start, filled on the next available bar, and then held.
- Metrics: CAGR, 365-day annualized volatility, Sharpe, Sortino, maximum drawdown, Calmar, 95% daily CVaR, cumulative turnover, maximum recovery duration, fees, and ending equity.

## Controlled experiments

`run_experiments.py` is the only component that consumes `parameter_grid`. It enforces the following chronology:

1. All 96 allowed configurations are evaluated on the 60% training period only.
2. Training scores penalize drawdown and annualized turnover. Finalists come from stable one-parameter neighborhoods using region median and dispersion, not the isolated maximum return.
3. Five fixed finalists receive access to the 20% validation period and three expanding walk-forward folds ending no later than validation end.
4. Validation degradation and walk-forward dispersion add parameter-instability penalties.
5. Exactly one candidate is written to the hash-chained ledger as locked.
6. Only after that lock event does the filtered Parquet reader load close values from the final 20% test period. Only the locked candidate is evaluated there.

Every candidate trial includes configured fees/slippage and comparisons with BTC buy-and-hold and equal weight. The append-only JSONL ledger uses sequence numbers, previous-record hashes, SHA-256 record hashes, flush/fsync on each append, final chain verification, and read-only finalization. The manager always records `live_promotion: false` and cannot promote a strategy to trading.

## Persistent paper trading

`run_paper.py` operates the locked `mw120_sw00_ma150_n2_r07_v30` strategy with 2,000 USDT virtual capital. It creates **no real exchange orders** and only uses CCXT public `fetch_ohlcv` and `fetch_ticker` data. The default is `--dry-run`; use `--paper` only to persist simulated fills. Protocol `paper-exec-v3-ask-bid-minspread-utc0010` permits decisions only Monday from **00:05 through 00:20 UTC**, with a 00:10 UTC target after the Sunday candle finalizes. Existing v2 records are preserved and never reinterpreted.

This forward protocol is explicitly different from the sealed historical engine. The sealed engine sizes from the Sunday signal close and fills fixed quantities at the next daily close (known Tuesday 00:00 UTC); forward paper sizes at the Monday quote midpoint and buys from ask/sells to bid near 00:10 UTC. This is an **EXECUTION_MODEL_MISMATCH**, not hidden alignment and not a new sealed out-of-sample test.

Execution uses observed bid/ask spread subject to a configured minimum spread, additional adverse slippage, and proportional fees. DuckDB stores the mutable current account/position projection separately from immutable order, fill, cash, and position ledgers. Schedule keys and deterministic per-signal idempotency keys prevent duplicate executions. Every run reconciles current cash and positions against append-only ledgers; a mismatch, negative state, missing calendar date, invalid/non-positive price, stale daily bar, stale quote, or failed public fetch activates a persistent kill switch before orders. Restart recovery marks uncommitted `RUNNING` records as `RECOVERED_ABORTED` and releases their schedule key; committed fills become `RECOVERED_COMMITTED` and retain duplicate protection. Reset is explicit and succeeds only after reconciliation:

```bash
.venv/Scripts/python.exe run_paper.py --reset-kill-switch
```

### Forward operational governance

- `forward_experiment/checkpoint_manifest.json` identifies the pre-deployment Git commit, strategy/config hashes, data cutoff, baseline schema hash, experiment-ledger hash, and 32-test checkpoint. Its SHA-256 sidecar detects modification.
- `forward_experiment/governance.json` fixes the strategy, costs, benchmarks, minimum 12-week observation rule, preferred 26–52 week evaluation, acceptance/rejection criteria, prohibited changes, and incident policy. It is create-once and hash verified before every paper command.
- `forward_experiment/governance_amendment_v2.json` additively supersedes only the forward operational schedule and review terminology while anchoring the unchanged governance and strategy hashes. Twelve weeks is an operational checkpoint only, 26 weeks is preliminary observation, and 52 weeks is the minimum substantive review; duration alone never proves profitability and any real-money consideration requires independent human review.
- Hermes jobs `b11c6b3fe2fe` (weekly execution: `10 0 * * 1`), `2d7c37751317` (missed-window audit only: `21 0 * * 1`), and `3861e40b01a7` (monthly: `0 9 1 * *`) are no-agent script jobs. Hermes is configured in UTC. The audit path performs no market fetch, signal, equity snapshot, or execution. A Windows Task Scheduler watchdog checks Gateway health and runs the idempotent startup audit, but it is interactive-user scoped: reliable scheduling still requires the laptop to be powered on and the user session available.
- Weekly Telegram delivery occurs only after the DuckDB run is terminal. A failed notification is logged separately and retried with `--resend RUN_ID`; strategy execution is never repeated.
- `run_monthly_report.py` uses only observations recorded after forward deployment and compares BTC/equal weight at identical timestamps. It never reads backtest returns.

Operational commands:

```bash
.venv/Scripts/python.exe run_paper.py --dry-run
.venv/Scripts/python.exe run_paper.py --paper
.venv/Scripts/python.exe run_paper.py --status
.venv/Scripts/python.exe run_paper.py --reconcile
.venv/Scripts/python.exe run_paper.py --resend RUN_ID
.venv/Scripts/python.exe run_paper.py --sample-telegram
```

## Data lifecycle

1. CCXT calls Binance's public spot OHLCV endpoint with built-in rate limiting.
2. Retryable rate-limit, DDoS, timeout, exchange-unavailable, and network errors use exponential backoff.
3. Each run writes CCXT's returned OHLCV row arrays directly to an immutable run-specific JSON file before cleaning. No timestamps, values, or ordering are changed in that raw artifact.
4. Raw rows are normalized to typed columns, validated, sorted, deduplicated by timestamp, and stripped of invalid/non-positive-price rows.
5. Clean data is saved as Parquet with timezone-aware UTC timestamps.
6. DuckDB records run status, paths, row counts, date ranges, and JSON quality metadata.
7. Markdown and JSON quality reports are written. Missing dates are reported but never synthesized or forward-filled.

The raw contract is CCXT's canonical six-value OHLCV array: `[timestamp_ms, open, high, low, close, volume]`. It is not Binance's lower-level REST payload with additional quote-volume/trade-count fields.

## Data-quality rules

- **Missing dates:** absent UTC calendar days between a dataset's first and last candle.
- **Duplicates:** all rows sharing a timestamp (`keep=False`, so both copies count).
- **Invalid OHLC:** `high` below any of open/close/low, `low` above any of open/close/high, or null/unparseable required values.
- **Non-positive price:** any open, high, low, or close value less than or equal to zero.

Quality checks run before cleaning. Processed data removes duplicate timestamps and invalid/non-positive rows; missing dates remain visible and are not imputed.

## File-by-file map

### Root

- `README.md` — scope, setup, data contract, validation rules, and this file inventory.
- `requirements.txt` — bounded runtime and test dependencies: CCXT, NumPy, pandas, PyArrow, DuckDB, PyYAML, python-dotenv, and pytest.
- `.env.example` — safe environment-variable template; contains no secrets.
- `.gitignore` — excludes local environment files, virtualenv/cache files, logs, databases, downloaded data, and generated reports.
- `run_weekly.py` — end-to-end orchestration: configuration, download, raw preservation, validation, cleaning, Parquet output, metadata, reporting, run status, and exchange shutdown. Despite the name, scheduling is external.
- `run_backtest.py` — loads aligned Parquet closes, establishes the common lookback-safe analysis period, runs the primary strategy and all benchmarks, and persists comparison artifacts.
- `run_experiments.py` — staged training/validation/walk-forward/final-test orchestration, stable-region selection, candidate locking, and sealed holdout access.
- `run_paper.py` — public-data fetch, persistent recovery/reconciliation, scheduled dry-run or virtual execution, kill-switch handling, and weekly reporting CLI.
- `run_monthly_report.py` — prior-month forward-only metrics and identical-timestamp benchmark report.
- `scripts/paper_forward_weekly.py` / `paper_forward_monthly.py` — version-controlled UTC gates that invoke the absolute project Python and script paths with a 600-second timeout.

### `config/`

- `assets.yaml` — the five-symbol research universe.
- `strategy.yaml` — fixed baseline configuration plus the exact controlled grid and chronological experiment settings. `run_backtest.py` ignores the grid; only `run_experiments.py` consumes it. Live trading remains disabled.

### `src/`

- `__init__.py` — marks the data-ingestion package.
- `config.py` — typed environment settings and YAML asset loading.
- `logging_config.py` — UTC console and file logging setup.
- `download_data.py` — credential-free CCXT exchange creation, pagination, finalized-candle cutoff, retry/backoff, and rate-limit handling.
- `validate_data.py` — raw-row normalization, UTC conversion, data-quality detection, deduplication, and invalid-row filtering.
- `storage.py` — unchanged raw JSON and processed Parquet writers.
- `database.py` — DuckDB schema plus ingestion-run and dataset-metadata writes.
- `report.py` — machine-readable JSON and human-readable Markdown quality reports.
- `indicators.py` — reserved placeholder; no indicator implementation.
- `strategy.py` — look-ahead-safe momentum, realized-volatility, eligibility, BTC regime filter, and deterministic ranking logic.
- `portfolio.py` — inverse-volatility allocation with per-asset and combined-altcoin caps plus explicit residual cash.
- `costs.py` — deterministic proportional fees and adverse buy/sell slippage.
- `backtest.py` — event-driven weekly order queue, next-bar execution, sell-before-buy processing, and separate order/fill/position/cash ledgers.
- `benchmarks.py` — the four static benchmark definitions and runners.
- `metrics.py` — return, risk, drawdown, recovery, CVaR, turnover, fee, and terminal-value calculations.
- `backtest_report.py` — Markdown/CSV/JSON benchmark comparison and Parquet ledger/equity persistence.
- `experiment_manager.py` — exact-grid validation, chronological splits, train/validation/test access gate, turnover/drawdown penalties, and stable-neighborhood finalist selection.
- `experiment_runner.py` — stage-bounded Parquet reads, candidate evaluation with costs, and per-candidate BTC/equal-weight comparisons.
- `experiment_ledger.py` — append-only, fsync-backed, SHA-256 hash-chained experiment ledger with verification and read-only finalization.
- `experiment_report.py` — walk-forward validation, robustness, candidate-lock, trial-table, and final-test reports.
- `paper_market.py` — credential-free CCXT adapter limited to public OHLCV and ticker methods.
- `paper_store.py` — DuckDB schema, current state, append-only ledgers, restart recovery, reconciliation, incidents, and kill-switch reset.
- `paper_broker.py` — locked-strategy schedule gate, data validation, idempotent virtual orders/fills, spread/slippage/fee simulation, and negative-cash protection.
- `paper_report.py` — concise immutable weekly status, account, cost, and position report.
- `paper_forward.py` — deterministic diagnostics, outcome classification, schedule-window audit, and post-transaction forward persistence.
- `paper_notifications.py` — Hermes Telegram delivery after commit plus notification-only retry.
- `forward_operations.py` — UTC/Rome conversion, cross-process locking, missed-window recording, and manifest verification.
- `forward_governance.py` — locked-strategy identity and immutable governance verification.
- `forward_monthly.py` — forward-only monthly statistics and exact-timestamp benchmarks.
- `scheduler_contract.py` — installed Hermes job read-back verification.
- `walk_forward.py` — reserved placeholder; no optimization/walk-forward implementation.
- `risk_engine.py` — reserved placeholder; no trading risk implementation.

### `tests/`

- `test_downloader.py` — verifies retry count and exponential backoff timing for a CCXT rate-limit error.
- `test_validate_data.py` — verifies missing-date, duplicate, invalid-OHLC, and non-positive-price detection plus cleaning behavior and UTC dtype.
- `test_persistence.py` — verifies raw JSON equality, Parquet round-trip, and DuckDB metadata storage.
- `test_report.py` — verifies Markdown and JSON quality-report generation.
- `test_pipeline.py` — verifies the complete pipeline with an injected deterministic public-data substitute, including immutable raw path, Parquet, report, and DuckDB completion state.
- `test_portfolio.py` — verifies inverse-volatility construction and individual/combined allocation caps.
- `test_strategy.py` — verifies top-two selection, BTC regime behavior, and invariance to future-data changes.
- `test_backtest_engine.py` — verifies week-end-to-next-bar signal timing, fee/slippage calculations, and negative-cash prevention.
- `test_benchmarks.py` — verifies all four benchmark definitions and static buy-and-hold execution.
- `test_metrics.py` — verifies required metrics, drawdown, CVaR, turnover, and recovery duration.
- `test_backtest_report.py` — verifies comparison outputs and separate strategy ledgers.
- `test_experiment_manager.py` — verifies the 96-case allowed grid, chronological split, holdout gate, scoring penalties, and stable-region preference.
- `test_experiment_runner.py` — verifies cost-inclusive candidate evaluation and benchmark comparisons.
- `test_experiment_ledger.py` — verifies hash chaining, chain validation, and finalized-ledger write refusal.
- `test_experiment_report.py` — verifies walk-forward/robustness reports, trial count, and disabled live promotion.
- `test_paper_trading.py` — verifies duplicate-execution prevention, corrupted-state kill switch, dry-run non-persistence, stale-data halt, and restart recovery.
- `test_paper_market.py` — verifies that the market adapter uses only public OHLCV/ticker methods.
- `test_paper_report.py` — verifies concise virtual-only weekly reporting and 2,000 USDT accounting.
- `test_forward_operations.py` — UTC window, DST display, process overlap, missed-window, and immutable-manifest tests.
- `test_paper_notifications.py` — post-commit Telegram failure and notification-only resend without duplicate trade.
- `test_forward_monthly.py` — forward/backtest separation and identical benchmark timestamps.
- `test_forward_governance.py` / `test_forward_safety.py` — immutable governance, unchanged locked hash, and continued absence of real/private API methods.
- `test_scheduler_contract.py` — Hermes job read-back and deterministic UTC wrapper gate.

### Generated/runtime directories

- `data/raw/<run_id>/*.json` — immutable, run-versioned CCXT OHLCV arrays saved before normalization or cleaning.
- `data/processed/*.parquet` — latest cleaned datasets, one per symbol/timeframe.
- `database/trading.duckdb` — `ingestion_runs` and `dataset_metadata` tables; it stores metadata, not the OHLCV fact table.
- `reports/data_quality_<run_id>.md` — readable quality summary.
- `reports/data_quality_<run_id>.json` — complete machine-readable quality payload, including exact missing UTC dates.
- `reports/backtest_<run_id>/comparison.{md,csv,json}` — human- and machine-readable strategy-versus-benchmark metrics.
- `reports/backtest_<run_id>/equity_curves.parquet` — aligned equity curves for the primary strategy and four benchmarks.
- `reports/backtest_<run_id>/strategy_{orders,fills,positions,cash}.parquet` — separate event and state ledgers.
- `reports/backtest_<run_id>/run_metadata.json` — exact fixed parameters, UTC period, execution convention, and explicit no-optimization flag.
- `experiments/runs/<run_id>/experiment_ledger.jsonl` — immutable hash-chained record of stage access, every candidate trial, selection, lock, and sealed test.
- `experiments/runs/<run_id>/training_trials.parquet` — all 96 training evaluations and benchmark comparisons.
- `experiments/runs/<run_id>/validation_trials.parquet` — fixed-finalist validation evaluations.
- `experiments/runs/<run_id>/walk_forward_trials.parquet` — finalist results across expanding pre-test folds.
- `experiments/runs/<run_id>/walk_forward_validation.md` — walk-forward metrics and period disclosure.
- `experiments/runs/<run_id>/robustness_report.md` — stable-region, instability-penalty, lock, trial-count, benchmark, and final-test summary.
- `experiments/runs/<run_id>/candidate_lock.json` and `final_test.json` — pre-test lock decision and the later sealed-holdout result.
- `database/paper_trading.duckdb` — persistent virtual account, positions, immutable orders/fills/cash/position ledgers, run audit, equity snapshots, and kill-switch incidents.
- `reports/paper/paper_weekly_<date>_<run_id>.md` — never-overwritten weekly health, execution, cost, cash, equity, and position summary.
- `logs/data_pipeline.log` — UTC operational logs for downloads, retries, row counts, persistence, and failures.
- `experiments/.gitkeep` — reserves an empty directory for future research notebooks/scripts.

## Currency and cash limitations

Residual capital is labeled **USDT defensive allocation**, not risk-free cash. Results are **USDT-denominated portfolio returns** unless an externally supplied, timestamp-aligned conversion series is passed to `src/currency_reporting.py`, which then reports **EUR-converted portfolio returns** and FX contribution without forward filling. No EUR/USDT source is silently assumed. USDT depeg and counterparty risk are non-zero accepted limitations; no staking, lending, or yield is introduced.

Forward-review terminology is deliberately conservative: 12 weeks is an operational checkpoint, 26 weeks is a preliminary performance observation, and 52 weeks is the minimum substantive forward review. No duration alone proves profitability, and any real-money consideration requires explicit independent human review.

## Explicitly out of scope

No live trading, private endpoints, API-secret handling, exchange order placement, leverage, margin, withdrawals, staking, lending, or unattended strategy promotion has been implemented. Backtest and paper fills are research simulations, not broker orders.
