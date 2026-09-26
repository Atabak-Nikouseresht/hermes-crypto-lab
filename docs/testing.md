# Testing and verification

Where later examples use `.venv/Scripts/python.exe`, Linux/macOS users should substitute `.venv/bin/python`.

## Standard suite

Windows:

```bash
.venv/Scripts/python.exe -m compileall -q src tests scripts run_backtest.py run_data_pipeline.py run_experiments.py run_monthly_report.py run_paper.py
.venv/Scripts/python.exe scripts/verify_safety.py
.venv/Scripts/python.exe scripts/verify_scheduler_manifest.py
.venv/Scripts/python.exe scripts/check_markdown_links.py
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest tests -q --cov=src --cov=run_paper --cov-branch --cov-report=term --cov-fail-under=75
```

Linux/macOS:

```bash
.venv/bin/python -m compileall -q src tests scripts run_backtest.py run_data_pipeline.py run_experiments.py run_monthly_report.py run_paper.py
.venv/bin/python scripts/verify_safety.py
.venv/bin/python scripts/verify_scheduler_manifest.py
.venv/bin/python scripts/check_markdown_links.py
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest tests -q --cov=src --cov=run_paper --cov-branch --cov-report=term --cov-fail-under=75
```

The frozen functional baseline at historical pre-rewrite commit `ebeac389b1c309f1ef8f5a9056e96c3b28e08e01` (rewritten public equivalent `1ae75af22c1cf09cf3179823647f7f5a40f845c7`) passed 70 tests. The current count should always be obtained by running the suite. See [Public history rewrite](public-history-rewrite.md) for the verified mapping and the treatment of old SHAs embedded in sealed evidence.

## Coverage

```bash
.venv/Scripts/python.exe -m pytest tests -q \
  --cov=src --cov=run_paper \
  --cov-branch \
  --cov-report=term-missing \
  --cov-fail-under=75
```

Coverage output is generated locally and ignored. It is a testing diagnostic, not permanent governance evidence.

CI also enforces branch-aware critical-path floors after the full suite: 85%
for `src/paper_broker.py`, `src/paper_store.py`, `src/forward_operations.py`,
and `src/research_data.py`; 82% for `src/paper_notifications.py`; 78% for
`src/backup_restore.py`; 70% for `src/scheduler_contract.py`; and 80% for
`run_paper.py`, `src/paper_forward.py`, `src/statistical_diagnostics.py`,
`src/download_data.py`, `src/experiment_runner.py`,
`scripts/paper_forward_monthly.py`, `src/forward_governance.py`,
`src/paper_market.py`, and `src/forward_counterfactual.py`. These gates
emphasize real failure, recovery, idempotency, reconciliation, data-boundary,
and retry behavior. CI also retains a 75% overall branch-coverage floor.

Ruff enforces syntax/indentation (`E4`, `E7`, `E9`), Pyflakes (`F`), and
correctness-oriented Bugbear (`B`) rules. CI also runs Ruff's selected security
rules over production code while retaining the domain-specific public-only
scanner. MyPy is a deliberately bounded critical-module gate, not repo-wide
strict typing: `pyproject.toml` explicitly lists 14 targets across validation
and configuration, storage/data integrity, hardening, metrics/costs, scheduling,
release provenance, counterfactual analysis, and paper notifications.
`check_untyped_defs` and `no_implicit_optional` remain enabled, while
`follow_imports = "silent"` prevents the gate from expanding through imported
modules; errors in listed targets remain visible.

## Integrity checks

```bash
.venv/Scripts/python.exe scripts/generate_hardening_manifest.py
.venv/Scripts/python.exe -c "from pathlib import Path; from run_paper import load_paper_configuration; from src.forward_governance import economic_spec_hash_v2, locked_strategy_hash, verify_trust_anchors; root=Path('.').resolve(); config,_=load_paper_configuration(root); print(locked_strategy_hash(config)); print(economic_spec_hash_v2(config)); print(verify_trust_anchors(root,config))"
.venv/Scripts/python.exe -c "from pathlib import Path; from src.hardening_manifest import verify_hardening_manifest; print(verify_hardening_manifest(Path('.'), Path('forward_experiment/hardening_manifest.json')))"
.venv/Scripts/python.exe scripts/sealed_v1_manifest.py --expected-sha256 5f5ad2ba69c9b0283bb925399c8a2815f73502a94b671a5c3c43ed26869079d8
```

Regenerate the active manifest only after reviewing that changes are non-strategy maintenance. The generator refuses to proceed if the locked strategy hash or governance trust anchors differ. The frozen baseline manifest remains archived separately.

The active hardening manifest is regeneratable and verifies transitive local dependencies of governed entry points. The sealed V1 run-artifact manifest is different: CI verifies its exact file set and byte hashes against the independently anchored digest above and never regenerates it. `--generate` is reserved for an explicitly approved initial freeze or formally versioned historical-preservation process.

## Dependency checks

```bash
uv pip check --python .venv/Scripts/python.exe
.venv/Scripts/python.exe -m pip_audit -r requirements.lock
```

The SBOM is reproducibly generated when needed rather than committed:

```bash
.venv/Scripts/python.exe -c "from pathlib import Path; Path('security').mkdir(exist_ok=True)"
uvx --from cyclonedx-bom cyclonedx-py requirements requirements.lock \
  --output-reproducible --of JSON -o security/sbom.json
```

`security/*.json` is ignored because SBOM and vulnerability-database output are generated, large or time-sensitive artifacts.

## Test layers

The suite includes:

- deterministic strategy, portfolio, benchmark and metric unit tests;
- look-ahead and future-data invariance checks;
- downloader retry and market-data validation tests;
- raw/Parquet/DuckDB persistence integration tests;
- experiment-grid, holdout-gate and hash-ledger tests;
- restart-safe experiment-ledger seals and frozen V1 artifact integrity tests;
- paper quote, post-scaling quantity-rule, non-negative-cash, state, reconciliation and notification tests;
- property-based portfolio invariants;
- real subprocess lock contention/recovery tests;
- backup/temporary-restore and corruption tests;
- scheduler and repeated missed-window tests;
- legacy and comprehensive economic governance, protocol, manifest and prohibited-private-path tests.

## Mutation testing

The `Scheduled Security Assurance` workflow runs the hash-locked `mutmut` on
Ubuntu and enforces an 80% mutation-score floor. It copies only the selected
targets into a temporary `mutation_targets` package because `mutmut` rejects
modules imported under the project's `src.*` namespace. The scheduled run is
main-only; `workflow_dispatch` can also be used on a feature branch to verify
the bounded mutation suite before merge. Native Windows execution is
unsupported, so claim a mutation score only from a successful assurance run.

## CI

`.github/workflows/ci.yml` runs on pushes and pull requests with read-only repository permissions. It installs the hash-pinned lock, compiles source/tests, runs the low-noise Ruff correctness gate, verifies the public-only boundary, audits dependencies, checks trust anchors, the active hardening manifest and its current dependency closure, verifies the frozen V1 manifest without regenerating it, and runs the complete suite with branch coverage.