# Forward-paper operations and recovery runbook

All timestamps and schedule decisions are UTC. These commands use public market data only and never place an exchange order.

## Inspect without trading

```bash
.venv/Scripts/python.exe run_paper.py --status
.venv/Scripts/python.exe run_paper.py --reconcile
.venv/Scripts/python.exe run_paper.py --kill-switch-status
.venv/Scripts/python.exe -c "from pathlib import Path; from run_paper import load_paper_configuration; from src.forward_governance import verify_trust_anchors; r=Path('.').resolve(); c,_=load_paper_configuration(r); print(verify_trust_anchors(r,c))"
.venv/Scripts/python.exe -c "from pathlib import Path; from src.hardening_manifest import verify_hardening_manifest; print(verify_hardening_manifest(Path('.'), Path('forward_experiment/hardening_manifest.json'), Path('forward_experiment/hardening_manifest.json.sha256')))"
```

## Scheduled execution protocol

- Weekly paper wrapper: Monday 00:10 UTC; wrapper accepts 00:10–00:20 UTC.
- Missed-window audit: Monday 00:21 UTC and on watchdog startup/resume checks.
- Monthly report: day 1 at 09:00 UTC.
- Execution protocol for future orders: `paper-exec-v3-ask-bid-minspread-utc0010`; existing v2 records remain unchanged.
- Backtest and forward execution remain explicitly different; see `audits/execution_timing.md`.

The normal weekly execution is scheduler-owned. Do not manually run `--paper` outside the declared window. A missed window is audited and never backfilled.

## Audit a missed/startup window

```bash
.venv/Scripts/python.exe run_paper.py --startup-audit
```

This may create operational `MISSED_SCHEDULE` evidence, but it must not fetch market data, calculate a signal, write an equity snapshot, or create an order/fill.

## Notification-only retry

```bash
.venv/Scripts/python.exe run_paper.py --resend RUN_ID
```

This retries only an already-committed report. It cannot rerun the strategy or trading path.

## Backup and temporary restore verification

```bash
.venv/Scripts/python.exe scripts/backup_forward.py create --output-root backups
.venv/Scripts/python.exe scripts/backup_forward.py verify backups/TIMESTAMP
```

Backups are timestamped and non-overwriting, acquire the global writer lock, checkpoint DuckDB, include manifests/reports but not `.env`, and retain data until a human explicitly approves deletion. Verification restores only under a temporary directory and never replaces production.

## Gateway and scheduler recovery

```bash
hermes gateway status
hermes gateway restart
hermes cron list
C:/Windows/System32/schtasks.exe /Query /TN Hermes_Gateway /V /FO LIST
C:/Windows/System32/schtasks.exe /Query /TN Hermes_Crypto_Lab_Watchdog /V /FO LIST
```

The watchdog checks Gateway, attempts a restart when unavailable, and invokes startup audit only. It cannot execute paper trading outside the weekly wrapper window. The host still must be powered on and capable of running Windows Task Scheduler; long-term scheduling is not guaranteed while the laptop is shut down.

## Safe dependency update and rollback

```bash
# Review updates; do not edit locked strategy parameters.
uv pip compile requirements.txt --generate-hashes --output-file requirements.lock
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
uvx pip-audit -r requirements.lock
.venv/Scripts/python.exe -m pytest tests -q

# Roll back files from a known Git commit without rewriting history:
git checkout KNOWN_GOOD_COMMIT -- requirements.txt requirements.lock
uv pip install --python .venv/Scripts/python.exe -r requirements.lock
.venv/Scripts/python.exe -m pytest tests -q
```

Never amend, squash, or rewrite sealed research history. Do not update Hermes, CCXT, DuckDB, pandas, NumPy, or PyArrow immediately before a scheduled window without a test pass and rollback point.

## Incident response

1. Do not delete lock owner evidence while a process is active.
2. Run `--status`, `--reconcile`, and `--kill-switch-status`.
3. Preserve `logs/`, the database, reports, and runtime incident metadata outside Git.
4. Create and verify a backup.
5. If reconciliation fails, keep the kill switch active; do not reset automatically.
6. Repair only after independent human review, then use `--reset-kill-switch` explicitly.
7. Never replace production with a backup as part of a verification drill.
