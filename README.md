# Hermes Crypto Lab

[![Core CI](https://github.com/Atabak-Nikouseresht/hermes-crypto-lab/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Atabak-Nikouseresht/hermes-crypto-lab/actions/workflows/ci.yml)

Hermes Crypto Lab is an independent systematic-crypto research and **forward paper-trading** project. It ingests public Binance spot data, evaluates a locked strategy, and records versioned simulated execution and audit evidence. It is software-engineering and research work—not a live-trading service or a claim of profitable trading.

> **Current status: frozen forward paper validation.** The strategy baseline is frozen at historical commit `ebeac389b1c309f1ef8f5a9056e96c3b28e08e01` (rewritten public equivalent `1ae75af22c1cf09cf3179823647f7f5a40f845c7`). Forward observations are out-of-sample and are not used to retune it. No live capital, private exchange API, or real-order path is supported.

## Review in 60 seconds

- [Architecture](docs/architecture.md) — data, research, paper execution, persistence, and governance boundaries.
- [Testing and verification](docs/testing.md) — CI checks, coverage gates, dependency audits, and integrity verification.
- [Research methodology](docs/methodology.md) — candidate selection, locked strategy, and historical evidence limits.
- [Execution model](docs/execution-model.md) — the sealed-backtest/forward-paper mismatch and protocol details.
- [Governance](docs/governance.md) and [forward validation](docs/forward-validation.md) — frozen-strategy rules and evidence separation.
- Start with `run_data_pipeline.py`, `run_backtest.py`, and `run_paper.py` for the main entry points; the [repository layout](#repository-layout) maps the rest.

## What the project demonstrates

- Public market-data ingestion with UTC normalization, finalized-candle filtering, and raw/processed integrity checks.
- Look-ahead-aware event-driven historical simulation with explicit fees and adverse slippage.
- Controlled candidate evaluation, deterministic strategy locking, and hash-chained research ledgers.
- Versioned paper execution with bid/ask evidence, market-rule validation, and protocol provenance.
- Fail-closed validation for missing, stale, crossed, invalid, or future-dated quotes.
- Idempotent execution, process-wide writer locking, reconciliation, recovery, and kill-switch controls.
- Reproducible dependency locks, automated tests, security auditing, backups, and operational reporting.

## Why the controls are deliberate

Governance hashes, sealed artifacts, protocol versions, process locks, append-only ledgers, and fail-closed checks address specific research and operational risks: accidental strategy drift, retrospective rewriting, look-ahead contamination, duplicate execution, silent protocol changes, and untraceable changes during forward validation. They make the process auditable; they do **not** make this research system production-grade or suitable for real capital.

## Architecture

```text
Public Binance spot data
          │
          ▼
Acquire → validate → preserve raw/processed data
          │
          ├──────────────► Historical research → controlled evaluation → sealed evidence
          │                                      │
          │                                      ▼
          └──────────────────────────────► locked forward candidate
                                                 │
                                                 ▼
                                  quote/rule validation → paper protocol v3
                                                 │
                                                 ▼
                                  DuckDB state → ledgers → audit/reporting
```

The [architecture guide](docs/architecture.md) describes component contracts and the [repository layout](#repository-layout) points to the entry points.

## Research boundary and safety

- **Paper only:** no real exchange orders, private endpoints, account balances, withdrawals, leverage, margin, or derivatives.
- **Public data only:** the supported Binance path is unauthenticated and does not require exchange credentials.
- **Fail closed:** incomplete, stale, invalid, crossed, non-positive, non-finite, or future-dated market evidence stops execution.
- **Conservative simulated costs:** buys use ask-side evidence and sells use bid-side evidence; minimum adverse spread, additional slippage, and fees are recorded separately.
- **No backdating:** a missed execution window is audited and never creates a historical trade.
- **Versioned provenance:** historical v2 fills remain unchanged; future fills use `paper-exec-v3-ask-bid-minspread-utc0010`.
- **Frozen forward candidate:** changes that could affect trade occurrence, selection, size, timing, execution assumptions, risk, or expected return require explicit approval and a new strategy/protocol version.

Persistent paper execution is scheduler-owned and valid only inside the governed Monday UTC window. Missed windows are audited, not backfilled. See [operations and recovery](docs/operations.md).

## Engineering evidence is not research evidence

**Engineering evidence** includes the locked environment, deterministic tests, cross-platform CI, branch-coverage gates, static checks, dependency audits, public-only boundary checks, governance hashes, sealed-artifact verification, and execution/recovery tests. These tests assess specified software behavior.

**Research evidence** consists of historical backtests, preserved sealed evaluation artifacts, and the still-accumulating forward paper observations. Historical results depend on their documented assumptions; forward paper records do not establish live fillability. The forward sample is currently insufficient to claim profitability, strategy validation, or live-trading readiness. A passing test suite demonstrates neither an economic edge nor future performance.

## CI and external API canary

The primary [Core CI workflow](.github/workflows/ci.yml) runs on Linux and Windows. It installs hash-locked dependencies, checks compatibility, compiles code, runs Ruff and scoped MyPy, applies security lint and public-only checks, verifies scheduler/governance/sealed-artifact/hardening manifests and Markdown links, audits dependencies, and runs the full test suite with branch coverage and stricter critical-path floors.

The separate [Scheduled Security Assurance workflow](.github/workflows/scheduled-assurance.yml) scans the full Git history, audits runtime and quality dependencies, verifies trust anchors and preserved artifacts, and runs bounded mutation testing on Linux. Its mutation-score gate is not a live-market test.

The [Binance Public API Canary](.github/workflows/binance-public-api-canary.yml) is an external, read-only check of the public `referencePrice` and `executionRules` response contracts—not a deterministic core-CI gate. Exchange/network access restrictions (including HTTP 451) are reported as access-restricted/unavailable conditions, separately from schema incompatibility. Such a result means the live endpoint could not be validated from that environment; it does not, by itself, indicate a core-CI regression. A genuine schema incompatibility remains a canary failure and is not suppressed.

## Installation

### Prerequisites

- Python `3.11.16` (the Windows CI runner uses its available Python 3.11 patch release)
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

`requirements.txt` contains bounded, human-maintained dependency ranges. `requirements.lock` is the reproducible, hash-pinned environment used by CI.

## Repository layout

```text
.
├── config/                 # Asset universe, fixed baseline, locked paper settings
├── data/                   # Local market-data policy; downloaded data is ignored
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

Market data, databases, reports, logs, backups, caches, and runtime lock files are local and excluded from Git. See [repository and artifact policy](docs/repository-policy.md).

## Configuration and commands

The project uses environment variables and YAML configuration. `.env` is optional, local, and ignored. No Binance credentials are required or supported by the paper path. See [`.env.example`](.env.example) for the variable list.

Configuration boundaries:

- `config/assets.yaml` — fixed five-symbol universe
- `config/strategy.yaml` — research baseline, controlled historical grid, and locked paper configuration
- `.env` — local runtime paths and optional notification destination only

The legacy `capital_reference: EUR_2000_equivalent` field is descriptive metadata; calculations and paper ledgers are USDT-denominated. No EUR/USDT conversion is assumed without an explicit timestamp-aligned series.

Commands below are shown for Windows; on Linux/macOS replace `.venv/Scripts/python.exe` with `.venv/bin/python`.

```bash
# Download and validate public market data
.venv/Scripts/python.exe run_data_pipeline.py

# Run the fixed historical baseline (not the canonical evaluation of the locked forward candidate)
.venv/Scripts/python.exe run_backtest.py

# Inspect state without virtual execution
.venv/Scripts/python.exe run_paper.py --status
.venv/Scripts/python.exe run_paper.py --reconcile
.venv/Scripts/python.exe run_paper.py --kill-switch-status

# Produce a local proposal without orders or fills
.venv/Scripts/python.exe run_paper.py --dry-run
```

Dry-run records operational run/equity metadata but creates no orders or fills and does not mutate portfolio trade state. Status, reconciliation, kill-switch-status, and dry-run commands do not require a Telegram destination. Operational and recovery commands are documented in [operations and recovery](docs/operations.md).

## Testing and verification

Run the local suite and core integrity checks with the locked environment:

```bash
.venv/Scripts/python.exe -m pytest tests -q --cov=src --cov=run_paper --cov-branch --cov-report=term
.venv/Scripts/python.exe scripts/verify_safety.py
.venv/Scripts/python.exe scripts/verify_scheduler_manifest.py
.venv/Scripts/python.exe scripts/check_markdown_links.py
.venv/Scripts/python.exe -m pip_audit -r requirements.lock
```

CI runs the suite on Linux and Windows and enforces an overall branch-coverage floor plus stricter module-level floors on critical execution, persistence, governance, data, and operational paths. It also runs compile checks, Ruff, scoped MyPy, security lint, dependency auditing, and the integrity checks described above. Exact test and coverage results are run-specific; see the live workflow rather than relying on a README count.

See [Testing and verification](docs/testing.md) for the complete commands, covered test layers, dependency controls, mutation assurance, and manifest requirements.

## Research methodology and forward validation

The research family is long-only weekly cross-sectional momentum with a BTC trend regime, deterministic ranking, inverse-volatility sizing, allocation caps, and residual USDT. Historical candidate selection was controlled, and candidate locking preceded final-test evaluation. The [methodology guide](docs/methodology.md) documents the research lifecycle and limitations.

The sealed backtest fills at the next daily close. Forward paper execution uses public bid/ask quotes around Monday 00:10 UTC. The models are materially different and are explicitly classified as `EXECUTION_MODEL_MISMATCH`; this is disclosed rather than retroactively reconciled. See the [execution model](docs/execution-model.md).

Forward validation follows:

```text
observe → record → measure → compare
```

Forward observations remain separate from training, calibration, and parameter-selection data. Operational defect fixes may preserve the existing economics; any change capable of affecting trading behavior requires explicit approval and a new version. See [forward validation](docs/forward-validation.md) and [governance](docs/governance.md).

## Limitations

- Historical performance does not guarantee future performance.
- Paper trading is not equivalent to live execution or guaranteed fillability.
- Real spreads, slippage, latency, fees, outages, and exchange rules may differ.
- Cryptocurrency markets are volatile and exposed to exchange, stablecoin, liquidity, and regime risk.
- Statistical overfitting and parameter instability remain possible despite controlled research procedures.
- The sealed and forward execution models differ materially.
- The scheduler depends on the host being powered on and its operating-system task environment being available.
- Current forward evidence is insufficient to claim profitability, validation, or live-trading readiness.

## Development process and AI assistance

This repository is owned and maintained by Atabak Nikouseresht. AI-assisted development tools, including Hermes Agent, have been used for parts of implementation, maintenance, review, documentation, and local scheduling/notification automation. Recent commit history includes Hermes Agent co-author attribution; no human/AI work split is claimed.

AI-assisted output is not independent validation. Changes remain subject to the repository's applicable tests, CI, governance and integrity checks, and review where performed. Tool assistance does not turn historical results or forward observations into independent empirical evidence, nor does it authorize changes across the frozen research boundary. This is an independent project, not an official Nous Research or Hermes product.

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
- [Future research version requirements](docs/future-research-v2.md)
- [Public history rewrite and commit mapping](docs/public-history-rewrite.md)
- [Security policy](SECURITY.md)
- [Contribution policy](CONTRIBUTING.md)

## Author

**Atabak Nikouseresht**\
MSc Applied Economics and Markets — University of Bologna\
Quantitative Research · Financial Risk · Data Analytics

GitHub: [Atabak-Nikouseresht](https://github.com/Atabak-Nikouseresht)\
LinkedIn: [Atabak Nikouseresht](https://linkedin.com/in/atabak-nikouseresht)

## License and reuse

No open-source license has been selected or included. Public visibility should not be read as a grant of permission to reuse, modify, or redistribute the source.

## Disclaimer

This repository is for research, education, and software-engineering demonstration only. It is not financial advice, an offer to trade, or a representation that any strategy is profitable or suitable for real capital.
