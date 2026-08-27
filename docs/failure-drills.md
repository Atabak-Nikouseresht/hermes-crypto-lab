# Failure-drill evidence

These drills exercise operational safeguards without changing locked strategy logic or genuine historical paper fills.

## Verified drills

| Scenario | Method | Verified result |
|---|---|---|
| Network/public-fetch failure | Injected malformed/missing market evidence | Run failed closed; no virtual fill committed |
| Malformed quote | Missing/non-positive/crossed/non-finite bid/ask tests | Kill switch or rejected proposal; no fill |
| Future quote | Quote timestamp moved beyond decision time | Kill switch for every configured asset |
| Stale quote | Quote older than configured maximum | Kill switch; no fill |
| Duplicate invocation | Reused schedule/idempotency keys | No duplicate order/fill |
| Lock contention | Separate holder and contender processes | Contender blocked before writable DuckDB access |
| Stale owner metadata | Holder process exited | OS lock released; owner evidence retained |
| Corrupt state | Ledger/projection mismatch or corrupt temporary DB | Reconciliation/verification failed closed |
| Notification failure | Sender failed after terminal paper run | Trading state stayed committed once; notification retry only |
| Missed window | First and repeated late invocation | `MISSED_SCHEDULE`; zero market fetches and no backdated trade |
| Backup/restore | Production backup plus temporary restore | Checksums/schema/reconciliation passed; production not replaced |
| DST display | UTC↔Europe/Rome tests | Stored decisions remained UTC; display conversion correct |
| Gateway watchdog | Status/restart/startup-audit path | Startup audit only; no wrapper bypass |

## Partially verified or not induced

- The exact process-instruction immediately after a production commit was not forcibly terminated; transaction/recovery branches were exercised with controlled state.
- A real Binance outage was not induced; failure behavior was injected at the public-client boundary.
- Production system clock was not manipulated; future quote/system-time discrepancy was tested deterministically.
- Disk-full behavior was not induced on the production volume.
- Physical disk loss, laptop shutdown during a window, and Windows Task Scheduler behavior across every power/sleep transition remain operational risks.

## Interpretation

A passing drill demonstrates the bounded behavior exercised. It does not establish resilience to every host, exchange, filesystem, or operating-system failure.