# Operations and recovery

All schedule decisions and stored timestamps are UTC. These commands use public market data only and never place an exchange order.

Commands below use Windows virtual-environment paths. On Linux/macOS, replace `.venv/Scripts/python.exe` with `.venv/bin/python`. The PowerShell watchdog is Windows-specific.

## Inspect without trading

```bash
.venv/Scripts/python.exe run_paper.py --status
.venv/Scripts/python.exe run_paper.py --reconcile
.venv/Scripts/python.exe run_paper.py --kill-switch-status
.venv/Scripts/python.exe -c "from pathlib import Path; from run_paper import load_paper_configuration; from src.forward_governance import verify_trust_anchors; root=Path('.').resolve(); config,_=load_paper_configuration(root); print(verify_trust_anchors(root,config))"
.venv/Scripts/python.exe -c "from pathlib import Path; from src.hardening_manifest import verify_hardening_manifest; print(verify_hardening_manifest(Path('.'), Path('forward_experiment/hardening_manifest.json')))"
```

## Scheduled execution protocol

- Weekly paper wrapper: Monday 00:10 UTC; the governed decision window is 00:05–00:20 UTC.
- Missed-window audit: Monday 00:21 UTC and watchdog startup/resume checks.
- Monthly report: day 1 at 09:00 UTC.
- Future-order protocol: `paper-exec-v3-ask-bid-minspread-utc0010`.
- Existing v2 records remain unchanged.
- Historical and forward execution remain explicitly different; see [Execution model](execution-model.md).

Persistent `--paper` execution is scheduler-owned. Do not run it manually outside the declared window. A missed window is audited and never backfilled.

The weekly wrapper uses no durable daily filesystem claim. A transient or
pre-execution subprocess failure may therefore retry inside the same governed
window. The global writer lock serializes attempts, while the unique database
schedule key prevents replay after any committed run. On restart, committed
fills/equity are never replayed. Missing post-commit evidence is finalized as
`RECOVERED_COMMITTED_INCOMPLETE_EVIDENCE` when exact market observations cannot
be reconstructed without fabrication.

Recovery also writes a report from committed database evidence and records the
notification as `PENDING`; `--resend RUN_ID` retries delivery only and never
invokes market fetch, signal generation, or execution. Because Telegram does not
offer a transaction shared with DuckDB, interruption after an external send but
before the local `DELIVERED` update remains an auditable at-least-once delivery
ambiguity; the pending record is retained rather than falsely claiming success.

`forward_experiment/scheduler_manifest.json` is the portable static contract:
job names, UTC triggers, safety settings, wrapper paths, and wrapper hashes.
Volatile deployment observations—including local job IDs, installation status,
last/next run times, and live state—remain local runtime metadata and are not
source-controlled.

## Dry run

```bash
.venv/Scripts/python.exe run_paper.py --dry-run
```

A dry run may retrieve public market evidence and calculate a proposal. It
persists operational run/equity metadata for auditability, but creates no orders
or fills and performs no portfolio trade-state mutation. It does not require
`HCL_TELEGRAM_TARGET`.

The inspection commands above also work without a Telegram target. Sample
delivery, missed-window/startup notification, and a genuine scheduled paper
operation resolve the target only when notification delivery is required. A
notification-only retry uses the destination stored with the original committed
notification record.

## Audit a missed/startup window

```bash
.venv/Scripts/python.exe run_paper.py --startup-audit
```

This may create operational `MISSED_SCHEDULE` evidence. It must not fetch market data, calculate a signal, write an equity snapshot, or create an order/fill. Repeated invocation after the same closed window must also perform zero market fetches.

## Notification-only retry

```bash
.venv/Scripts/python.exe run_paper.py --resend RUN_ID
```

This retries only an already-committed report. It cannot rerun strategy or paper execution.

## Monthly reporting

`run_monthly_report.py` is scheduler-owned and has no `--help`/argument parser. Direct invocation generates or reuses the prior-month forward-only report and attempts delivery through `HCL_TELEGRAM_TARGET`:

```bash
.venv/Scripts/python.exe run_monthly_report.py
```

Do not invoke it merely to inspect usage. Use source documentation and scheduler status instead.

## Backup and temporary restore verification

```bash
.venv/Scripts/python.exe scripts/backup_forward.py create --output-root backups
.venv/Scripts/python.exe scripts/backup_forward.py verify backups/TIMESTAMP
```

Backups are timestamped and non-overwriting, acquire the global writer lock, checkpoint DuckDB, include manifests/reports but not `.env`, and remain local. Verification restores only under a temporary directory and never replaces production.

## Gateway and scheduler recovery

```bash
hermes gateway status
hermes gateway restart
hermes cron list
C:/Windows/System32/schtasks.exe /Query /TN Hermes_Gateway /V /FO LIST
C:/Windows/System32/schtasks.exe /Query /TN Hermes_Crypto_Lab_Watchdog /V /FO LIST
```

The watchdog checks Gateway, attempts restart when unavailable, and invokes startup audit only. It cannot bypass the weekly paper wrapper. On failure it retains only compact, sanitized tail output in its local log; credential- or destination-shaped lines are redacted. Scheduling still depends on the host being powered on and the Windows task environment being available.

## Dependency update and rollback

Dependency maintenance is an operational change and should not occur immediately before a scheduled window.

```bash
# Recompile only after reviewing version changes.
uv pip compile requirements.txt --generate-hashes --output-file requirements.lock
uv pip install --python .venv/Scripts/python.exe --require-hashes -r requirements.lock
.venv/Scripts/python.exe -m pip_audit -r requirements.lock
.venv/Scripts/python.exe -m pytest tests -q

# Restore known-good dependency files without rewriting history.
git restore --source KNOWN_GOOD_COMMIT -- requirements.txt requirements.lock
uv pip install --python .venv/Scripts/python.exe --require-hashes -r requirements.lock
.venv/Scripts/python.exe -m pytest tests -q
```

## Incident response

1. Do not delete lock-owner evidence while a process is active.
2. Run `--status`, `--reconcile`, and `--kill-switch-status`.
3. Preserve local logs, database, reports, and runtime incident metadata outside Git.
4. Create and verify a backup.
5. If reconciliation fails, keep the kill switch active.
6. Repair only the operational defect; do not alter strategy economics.
7. Reset the kill switch only after reconciliation and explicit review.
8. Never replace production with a backup as part of a verification drill.

Detailed bounded drill evidence is in [Failure drills](failure-drills.md).