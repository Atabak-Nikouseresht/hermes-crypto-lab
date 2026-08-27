# Local market data

Market datasets are intentionally not committed.

## Source

The ingestion pipeline uses CCXT against public Binance spot OHLCV endpoints. No API key or private exchange method is required.

## Generate

From the repository root. The command below uses a Windows virtual-environment path; on Linux/macOS, replace `.venv/Scripts/python.exe` with `.venv/bin/python`.

```bash
.venv/Scripts/python.exe run_data_pipeline.py
```

## Local layout

```text
data/
├── raw/<run_id>/<symbol>_<timeframe>.json
└── processed/<base>_<quote>_<timeframe>.parquet
```

Raw JSON contains the six-value OHLCV arrays returned by CCXT before normalization:

```text
[timestamp_ms, open, high, low, close, volume]
```

Processed Parquet contains:

| Column | Type/meaning |
|---|---|
| `timestamp` | timezone-aware UTC timestamp |
| `open` | numeric opening price |
| `high` | numeric high price |
| `low` | numeric low price |
| `close` | numeric closing price |
| `volume` | numeric base volume |

Only finalized candles are retained. Quality checks detect missing periods, duplicate timestamps, invalid OHLC relationships, null/unparseable required values, and non-positive prices. Missing periods are reported rather than synthesized.

Raw run directories are immutable evidence on the local system. Processed files are reproducible analytical snapshots. Both are ignored to avoid stale data and repository bloat.