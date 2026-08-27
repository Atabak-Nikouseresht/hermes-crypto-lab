# Audit evidence index

This directory contains bounded, point-in-time verification evidence. These files are not live dashboards and must not be interpreted as proof of economic edge.

## Durable evidence

| Artifact | Purpose |
|---|---|
| `backup_restore.json` | Non-overwriting backup and temporary-restore verification |
| `data_cross_check.json` | Deterministic comparison with a second public source |
| `data_integrity.json` | Byte and semantic validation of preserved market data |
| `environment.json` | Frozen hardening-cycle environment evidence |
| `paper_state_before.json` | Read-only paper account snapshot before a no-mutation verification |
| `paper_state_after.json` | Read-only snapshot after that verification; intentionally identical to the before snapshot |
| `paper_state_current.json` | Read-only state captured at the frozen forward baseline |
| `statistical/` | Post-selection diagnostics, explicitly not sealed out-of-sample evidence |

Narrative evidence was moved to `docs/` for discoverability:

- [Execution-model audit](../docs/execution-model.md)
- [Failure drills](../docs/failure-drills.md)
- [Testing and mutation-test limitations](../docs/testing.md)

## Generated outputs not tracked

Coverage JSON, dependency-audit JSON, and SBOM JSON are reproducible build outputs and are ignored. Generate them using the commands in [Testing and verification](../docs/testing.md).

Audit evidence may contain synthetic paper-account identifiers and virtual balances. It contains no exchange credentials, real brokerage account, or live-capital state.
