# Architecture

## Scope

Hermes Crypto Lab separates public-data ingestion, historical research, locked forward paper execution, persistent state, and governance. It intentionally contains no private exchange or live-order path.

## Component map

```text
                 ┌────────────────────────────┐
                 │ Public Binance spot market │
                 └──────────────┬─────────────┘
                                │ OHLCV / ticker
                                ▼
┌────────────────────────────────────────────────────────┐
│ Data layer                                             │
│ download_data → validate_data → storage → database     │
│ public-only, UTC, finalized bars, raw before clean     │
└──────────────┬───────────────────────────────┬─────────┘
               │ processed Parquet             │ quote/rule snapshot
               ▼                               ▼
┌───────────────────────────────┐   ┌──────────────────────────────┐
│ Historical research           │   │ Forward paper system         │
│ strategy → portfolio          │   │ paper_market → paper_broker  │
│ → backtest → metrics          │   │ → paper_store                │
│ → experiment manager/ledger   │   │ → report/notification        │
└──────────────┬────────────────┘   └──────────────┬───────────────┘
               │ candidate lock                    │ virtual ledgers
               └──────────────────┬─────────────────┘
                                  ▼
                  ┌─────────────────────────────┐
                  │ Governance and operations   │
                  │ hashes, manifests, locking, │
                  │ missed-window audit, backup │
                  └─────────────────────────────┘
```

## Data ingestion

`run_data_pipeline.py` orchestrates:

1. typed runtime configuration from `src/config.py`;
2. credential-free CCXT public market access in `src/download_data.py`;
3. immutable run-specific raw JSON persistence before normalization;
4. UTC conversion and quality checks in `src/validate_data.py`;
5. atomically written, run-versioned processed Parquet plus an immutable
   per-version manifest and atomic current-manifest pointer in `src/storage.py`;
6. ingestion metadata in a local DuckDB database;
7. Markdown and JSON quality reports.

Canonical research accepts only complete, aligned `1d` UTC calendars. It rejects
missing/duplicate/misaligned candles and invalid OHLCV before publication; it
never repairs calendars with forward filling or inner-join compression. New run
artifacts persist the exact manifest and dataset hashes they consumed.

Downloaded market data, ingestion databases, logs, and generated reports are local artifacts and are ignored by Git.

## Historical research

- `src/strategy.py` computes eligible assets and deterministic ranking using only data available through the signal timestamp.
- `src/portfolio.py` converts selections into capped inverse-volatility target weights with explicit residual cash.
- `src/backtest.py` queues decisions and fills them on the next available daily row under the sealed historical convention.
- `src/benchmarks.py` and `src/metrics.py` provide consistent comparisons and risk/return metrics.
- `run_experiments.py` controls train, validation, fixed-finalist rolling pre-test evaluation (historically labeled `walk_forward`), lock, and final-test access.
- `src/experiment_ledger.py` records append-only, hash-chained stage evidence.

The two tracked experiment directories are preserved historical records, not current optimization inputs.

## Forward paper execution

`run_paper.py` is the main forward CLI. Its layers are:

- `src/paper_market.py` — public daily bars, bid/ask quotes, quote timestamps, and public market rules;
- `src/paper_broker.py` — schedule, causality, freshness, spread, slippage, fee, rule, and cash checks;
- `src/paper_store.py` — DuckDB projections, immutable ledgers, incidents, kill switch, reconciliation, and recovery;
- `src/paper_forward.py` — diagnostics and forward observation persistence;
- `src/paper_report.py` — paper-only report generation;
- `src/paper_notifications.py` — post-commit delivery and notification-only retry.

Paper execution never calls exchange order, account-balance, leverage, withdrawal, margin, or derivatives methods.

## Concurrency and recovery

`src/forward_operations.py` provides the repository-wide writer lock and missed-window logic. The lock is acquired before writable DuckDB initialization, schema migration, or recovery. Schedule and idempotency keys prevent duplicate virtual execution.

`src/backup_restore.py` and `scripts/backup_forward.py` create non-overwriting backups and verify restoration only in a temporary location.

## Why the package remains `src/`

The repository already imports modules through the existing `src` package across production scripts, scheduler wrappers, tests, manifests, and the frozen runtime. Migrating to `src/hermes_crypto_lab/` would create broad import and deployment churn without improving the validated behavior. The publication cleanup therefore retains the proven package boundary and documents it instead of performing an aesthetic architecture rewrite.