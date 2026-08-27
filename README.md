# Hermes Crypto Lab

A reproducible systematic-crypto research and forward paper-trading platform built around public Binance spot market data, deterministic strategy evaluation, protocol-versioned simulated execution, and tamper-evident governance.

> **Status: Forward Paper Validation**
> The strategy is frozen at commit `ebeac389b1c309f1ef8f5a9056e96c3b28e08e01`. No live capital, private exchange API, or real-order path is used. Forward observations are collected as genuine out-of-sample evidence and are not used for retuning.

This is an independent research/portfolio project. Hermes Agent is used for local scheduling and notification automation; the repository is not an official Nous Research or Hermes product.

## What this project demonstrates

- Public-data ingestion with retries, UTC normalization, finalized-candle filtering, and raw/processed integrity checks
- Look-ahead-aware, event-driven historical simulation with explicit fees and adverse slippage
- Controlled candidate evaluation, deterministic locking, and hash-chained research ledgers
- Persistent paper execution with bid/ask evidence, market-rule validation, and protocol provenance
- Fail-closed handling of missing, stale, crossed, invalid, or future-dated quotes
- Minimum adverse spread, additional slippage, and fee accounting
- Idempotency controls, global writer locking, reconciliation, and kill-switch controls
- Missed-window auditing without backdated trades or unnecessary repeat market fetches
- Versioned governance, preserved historical v2 fills, and v3-only future execution
- Automated testing, dependency locking, vulnerability auditing, backups, and operational reporting

The project is research software. A working engineering system and a profitable backtest are not proof of a durable economic edge.

## Architecture

```text
Public Binance spot data
          │
          ▼
Download, UTC normalization, integrity and quality checks
          │
          ├──────────────► Versioned raw data + processed Parquet
          │
          ▼
Strategy and regime logic ──► Portfolio construction
          │
          ├──────────────► Historical event-driven backtest
          │                    └─► controlled experiments + sealed artifacts
          │
          ▼
Locked forward candidate
          │
          ▼
Quote/rule validation ──► paper execution protocol v3
          │
          ▼
DuckDB state + append-only ledgers + audit/reporting
```

See [Architecture](docs/architecture.md), [Methodology](docs/methodology.md), and [Execution model](docs/execution-model.md) for the detailed contracts.

## Repository layout

```text
.
├── config/                 # Asset universe, fixed baseline, locked paper settings
├── data/                   # Local market data policy; downloaded data is ignored
├── docs/                   # Architecture, methodology, governance, operations, testing
├── experiments/runs/       # Preserved controlled-research ledgers and sealed artifacts
├── forward_experiment/     # Protocols, governance, schema and integrity manifests
├── scripts/                # Backup, scheduler, watchdog, cross-check and safety tools
├── src/                    # Research, execution, persistence and reporting modules
├── tests/                  # Unit, property, subprocess and integration tests
├── run_data_pipeline.py    # Public OHLCV ingestion and validation
├── run_backtest.py         # Fixed historical baseline and benchmark evaluation
├── run_experiments.py      # Controlled staged research workflow
├── run_paper.py            # Paper status, audit, dry-run and scheduled persistence CLI
└── run_monthly_report.py   # Scheduler-owned forward-only monthly reporting
```

Generated databases, market data, reports, logs, backups, caches, and runtime lock files remain local and are excluded from Git. The preservation policy is documented in [Repository and artifact policy](docs/repository-policy.md).

## Installation

### Prerequisites

- Python `3.11.16`
- [`uv`](https://docs.astral.sh/uv/) for environment management
- Git

### Windows

```bash
uv venv --python 3.11.16 .venv
uv pip install --python .venv/Scripts/python.exe --require-hashes -r requirements.lock
cp .env.example .env
.venv/Scripts/python.exe -m pytest tests -q
```

### Linux/macOS

```bash
uv venv --python 3.11.16 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements.lock
cp .env.example .env
.venv/bin/python -m pytest tests -q
```

`requirements.txt` contains bounded human-maintained dependency ranges. `requirements.lock` is the reproducible, hash-pinned environment used by CI.

## Configuration

The project uses environment variables and YAML configuration. `.env` is optional, local, and ignored. No Binance credentials are required or supported by the paper path.

Important variables are listed in [`.env.example`](.env.example):

| Variable | Purpose |
|---|---|
| `HCL_EXCHANGE` | Public CCXT exchange class; validated baseline is Binance |
| `HCL_TIMEFRAME` | OHLCV timeframe; validated baseline is `1d` |
| `HCL_SINCE` | Earliest requested UTC timestamp |
| `HCL_DATABASE_PATH` | Local ingestion-metadata database |
| `HCL_PAPER_DATABASE` | Local paper-state database |
| `HCL_TELEGRAM_TARGET` | Optional Hermes delivery target; never commit a real target |

Configuration boundaries:

- `config/assets.yaml` — fixed five-symbol universe
- `config/strategy.yaml` — research baseline, controlled historical grid, and locked paper configuration
- `.env` — local runtime paths and notification destination only

Do not modify locked strategy or protocol fields during forward validation. The legacy `capital_reference: EUR_2000_equivalent` field is descriptive metadata only; calculations and paper ledgers are USDT-denominated, and no EUR/USDT conversion is assumed without an explicit timestamp-aligned series.

## Running the project

Commands below are executed from the repository root and use Windows virtual-environment paths. On Linux/macOS, replace `.venv/Scripts/python.exe` with `.venv/bin/python`.

### Download and validate public market data

```bash
.venv/Scripts/python.exe run_data_pipeline.py
```

### Run the fixed historical baseline

```bash
.venv/Scripts/python.exe run_backtest.py
```

### Inspect paper state without trading

```bash
.venv/Scripts/python.exe run_paper.py --status
.venv/Scripts/python.exe run_paper.py --reconcile
.venv/Scripts/python.exe run_paper.py --kill-switch-status
```

### Produce a non-persistent paper proposal

```bash
.venv/Scripts/python.exe run_paper.py --dry-run
```

Persistent `--paper` execution is scheduler-owned and valid only inside the governed Monday UTC window. Missed windows are audited and never backfilled. Operational and recovery commands are in [Operations](docs/operations.md).

### Validate safety and integrity

```bash
.venv/Scripts/python.exe scripts/verify_safety.py
.venv/Scripts/python.exe -m pip_audit -r requirements.lock
.venv/Scripts/python.exe -c "from pathlib import Path; from src.hardening_manifest import verify_hardening_manifest; print(verify_hardening_manifest(Path('.'), Path('forward_experiment/hardening_manifest.json')))"
```

## Testing

```bash
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m pytest tests -q --cov=src --cov-branch --cov-report=term
```

The frozen functional baseline at `ebeac389` passed 70 tests. Treat that count as a recorded baseline rather than a permanently current badge; run the suite for the current count. CI runs compilation, dependency auditing, governance verification, the prohibited-private-path scan, manifest verification, and the complete suite on pushes and pull requests.

See [Testing and verification](docs/testing.md).

## Safety model

- **Paper only:** no real exchange orders, private endpoints, balances, withdrawals, leverage, margin, or derivatives
- **Fail closed:** incomplete, stale, invalid, crossed, non-positive, non-finite, or future-dated market evidence stops execution
- **Executable side:** buys use ask-side evidence; sells use bid-side evidence
- **Conservative costs:** a minimum adverse spread, additional slippage, and proportional fee are recorded separately
- **Market rules:** amount, precision, activity, step-size, and minimum-notional evidence are required
- **No backdating:** a missed window records an incident and produces no historical trade
- **Single writer:** paper execution, reporting, audit, notification state and backups share one process lock
- **Versioned provenance:** historical v2 fills remain v2; future fills use `paper-exec-v3-ask-bid-minspread-utc0010`
- **Locked strategy:** the candidate and strategy hash are verified before paper commands
- **Notification isolation:** delivery failures cannot retrigger strategy execution

## Research methodology

The system uses a long-only, weekly cross-sectional momentum approach with a BTC trend regime, deterministic ranking, inverse-volatility sizing, explicit allocation caps, and residual USDT. Historical candidate selection was controlled and candidate locking preceded final-test evaluation.

The sealed backtest fills at the next daily close. Forward paper execution uses public bid/ask quotes around Monday 00:10 UTC. These are not equivalent; the project explicitly classifies them as:

```text
EXECUTION_MODEL_MISMATCH
```

This mismatch is documented rather than retroactively “fixed.” See [Methodology](docs/methodology.md) and [Execution model](docs/execution-model.md).

## Forward validation

The current priority is:

```text
observe → record → measure → compare
```

not optimization. Forward results remain separate from training, calibration, and parameter-selection data. Only operational defects may be fixed without explicit approval; any change that could affect trade occurrence, selection, size, timing, execution assumptions, portfolio risk, or expected return requires a new version and explicit review.

See [Forward validation](docs/forward-validation.md) and [Governance](docs/governance.md).

## Limitations

- Historical performance does not guarantee future performance.
- Paper trading is not equivalent to live execution or guaranteed fillability.
- Real spread, slippage, latency, fees, outages, and exchange rules can differ.
- Cryptocurrency markets are highly volatile and exposed to exchange, stablecoin, liquidity, and regime risk.
- Statistical overfitting and parameter instability remain possible despite controlled research procedures.
- The sealed and forward execution models are materially different.
- The scheduler depends on the host being powered on and the operating-system task environment being available.
- Current forward evidence is not sufficient to claim profitability, validation, or live-trading readiness.

## Documentation

- [Architecture](docs/architecture.md)
- [Methodology](docs/methodology.md)
- [Execution model](docs/execution-model.md)
- [Governance and protocol preservation](docs/governance.md)
- [Forward validation](docs/forward-validation.md)
- [Operations and recovery](docs/operations.md)
- [Testing and verification](docs/testing.md)
- [Audit evidence index](audits/README.md)
- [Repository and artifact policy](docs/repository-policy.md)
- [Security policy](SECURITY.md)

## License status

No open-source license has been selected. Until the owner chooses one, normal copyright restrictions apply. MIT or Apache-2.0 are common options for a portfolio project, but licensing should be selected deliberately before public reuse is invited.

## Disclaimer

This repository is for research, education, and software-engineering demonstration only. It is not financial advice, an offer to trade, or a representation that any strategy is profitable or suitable for real capital.