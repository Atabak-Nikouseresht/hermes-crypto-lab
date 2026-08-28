# Contributing

Hermes Crypto Lab is in frozen forward paper validation. Contributions must preserve the experimental boundary.

## Before changing code

Classify the proposal as either:

- **presentation/maintenance** — documentation, repository metadata, tests, logging, security, crash handling, corrupt-state handling, timestamp correctness, or broken public API interaction that preserves economics; or
- **strategy/protocol behavior** — anything that can change whether a trade occurs, asset selection, size, timing, execution-price assumptions, portfolio risk, or expected return.

Strategy/protocol behavior requires explicit approval and a new version. It must not be smuggled into cleanup or refactoring.

## Required checks

Commands below use Windows virtual-environment paths. On Linux/macOS, replace `.venv/Scripts/python.exe` with `.venv/bin/python`.

```bash
.venv/Scripts/python.exe -m compileall -q src tests scripts run_backtest.py run_data_pipeline.py run_experiments.py run_monthly_report.py run_paper.py
.venv/Scripts/python.exe scripts/verify_safety.py
.venv/Scripts/python.exe scripts/verify_scheduler_manifest.py
.venv/Scripts/python.exe scripts/check_markdown_links.py
.venv/Scripts/python.exe -m pytest tests -q
```

Also verify the legacy locked strategy hash, comprehensive economic-spec v2
hash, governance trust anchors, and active hardening manifest. Never rewrite
historical v2 fills, candidate locks, experiment ledgers, or sealed research
artifacts.

Do not commit `.env`, databases, market data, reports, logs, backups, runtime state, or real notification targets.
