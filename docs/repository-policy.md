# Repository and artifact policy

## Purpose

This policy separates durable source and research evidence from machine-local, reproducible, or sensitive runtime state.

## Version controlled

The repository intentionally tracks:

- source code and entry points;
- tests and deterministic fixtures;
- non-secret configuration examples;
- bounded dependencies and the hash-pinned lock file;
- focused documentation;
- controlled-research ledgers, candidate locks, and sealed-result artifacts;
- v2/v3 protocol and governance records;
- integrity manifests and captured schema;
- compact, non-sensitive audit evidence that is not cheaply reproduced from CI alone;
- GitHub Actions configuration.

## Local and ignored

The following remain outside Git:

- `.env` and environment-specific overrides;
- virtual environments and Python/tool caches;
- local DuckDB/SQLite databases and WAL files;
- downloaded raw and processed market data;
- runtime locks and owner metadata;
- operational logs;
- paper, monthly, backtest, notification, and data-quality reports;
- backups and temporary restore directories;
- coverage databases/reports;
- generated SBOM and vulnerability-scan output;
- current paper-state snapshots;
- IDE, OS, editor-swap, and build artifacts.

## Historical research artifacts

Both directories under `experiments/runs/` are retained. They are not interchangeable duplicate exports: together they disclose the actual sequence of deterministic final-period accesses. Deleting one would weaken provenance.

Historical protocol-v2 records, original governance, sidecar hashes, candidate locks, and experiment ledgers must never be rewritten to simplify presentation.

## Generated evidence

Generated outputs are committed only when they provide durable research or governance evidence that cannot be represented adequately by source plus CI. Large, noisy, or time-sensitive outputs such as coverage JSON, SBOMs, vulnerability database responses, and current state snapshots are regenerated locally or in CI and are ignored.

## Data policy

Market data originates from public Binance spot endpoints through CCXT. Raw and processed datasets are excluded to avoid repository bloat and stale snapshots. The expected schema and generation command are documented in `data/README.md`.

No licensed or proprietary dataset is distributed by this repository.

## Deletion standard

A tracked file may be deleted only after confirming that it is not imported, referenced by runtime code, required by tests, needed by governance, or part of preserved historical provenance. Unclear artifacts are retained until classified.