# Security policy

## Supported scope

This is research and paper-trading software. The repository supports public market-data retrieval and local simulated execution only. It does not support private exchange APIs, real orders, balances, withdrawals, leverage, margin, or derivatives.

## Reporting a vulnerability

Do not disclose a suspected vulnerability together with credentials, tokens, private URLs, account identifiers, or database contents in a public issue. Contact the repository owner privately through the GitHub profile associated with the project and provide a minimal reproduction with secrets redacted.

## Secrets

- Never commit `.env`.
- Never add Binance credentials; they are not required by the supported path.
- Configure the optional Hermes notification destination through `HCL_TELEGRAM_TARGET` locally.
- Treat databases, reports, logs, backups, runtime metadata, and current paper-state exports as local operational material.
- If a credential is committed, revoking/rotating it and reviewing Git history are required; deleting the current file alone is insufficient.

## Publication checks

Commands below use Windows virtual-environment paths. On Linux/macOS, replace `.venv/Scripts/python.exe` with `.venv/bin/python`.

Before public release:

```bash
.venv/Scripts/python.exe scripts/verify_safety.py
.venv/Scripts/python.exe -m pip_audit -r requirements.lock
.venv/Scripts/python.exe -m pytest tests -q
```

Automated scans are supporting evidence, not a guarantee that no vulnerability exists.