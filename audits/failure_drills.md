# Safe failure-drill record

Date: 2026-08-27 UTC. No exchange order was created and no production database was corrupted.

| Drill | Result | Evidence / limitation |
|---|---|---|
| Binance timeout / retry exhaustion | PARTIAL | Retry/backoff implementation is exercised by data-pipeline tests; no live forced Binance outage was induced. |
| Stale quote | PASS | Broker adversarial tests reject quotes beyond the configured age. |
| Malformed candle | PASS | OHLCV validation and data-integrity tests reject invalid OHLC relationships and non-finite/zero data. |
| Incomplete daily candle | PASS | Public snapshot test verifies current-day rows are excluded. |
| Telegram unavailable after commit | PASS | Notification tests prove a failed send leaves the committed virtual transaction intact and retry is notification-only. |
| Concurrent scheduler invocation | PASS | Real subprocess lock tests show a holder excludes a contender. |
| Termination before commit | PARTIAL | Transaction rollback and abandoned-RUNNING recovery are tested; no production process was forcibly killed. |
| Termination immediately after commit | PARTIAL | Commit-before-notification behavior is tested; exact instruction-boundary process kill was not injected. |
| Stale process lock | PASS | Real subprocess crash/stale-owner tests verify OS lock release and retained stale-owner evidence. |
| Hermes Gateway restart | PASS | `hermes gateway restart && hermes gateway status` returned success and a live PID. |
| Missed weekly window | PASS | Missed-window tests create one `MISSED_SCHEDULE` terminal record and no order/fill. |
| Startup audit after missed window | PASS | Watchdog log repeatedly records `startup_audit_exit=0`; idempotency tests prevent duplicate incidents. |
| Corrupted temporary database copy | PASS | Backup tests detect corruption without touching production. |
| Backup and temporary restore | PASS | Actual backup `20260826T120438Z` passed 17 checksums, schema v5, cash/position/order consistency, and temporary restore. |
| DST transition display | PASS | UTC remains canonical; Europe/Rome is display-only and zone-aware conversion is tested. |
| System-clock discrepancy | PARTIAL | Future quote timestamps beyond 30 seconds fail closed; production system clock was not manipulated. |
| Insufficient disk space | NOT RUN | Production disk was not filled. Atomic/non-overwriting writes reduce damage, but an end-to-end disk-full injection remains uncovered. |

`PASS` means the named invariant was exercised by a test or safe operational command. `PARTIAL` and `NOT RUN` are not promoted to passes.