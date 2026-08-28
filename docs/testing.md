# Testing and verification

Where later examples use `.venv/Scripts/python.exe`, Linux/macOS users should substitute `.venv/bin/python`.

## Standard suite

Windows:

```bash
.venv/Scripts/python.exe -m compileall -q src tests scripts run_backtest.py run_data_pipeline.py run_experiments.py run_monthly_report.py run_paper.py
.venv/Scripts/python.exe scripts/verify_safety.py
.venv/Scripts/python.exe scripts/verify_scheduler_manifest.py
.venv/Scripts/python.exe scripts/check_markdown_links.py
.venv/Scripts/python.exe -m pytest tests -q
```

Linux/macOS:

```bash
.venv/bin/python -m compileall -q src tests scripts run_backtest.py run_data_pipeline.py run_experiments.py run_monthly_report.py run_paper.py
.venv/bin/python scripts/verify_safety.py
.venv/bin/python scripts/verify_scheduler_manifest.py
.venv/bin/python scripts/check_markdown_links.py
.venv/bin/python -m pytest tests -q
```

The frozen functional baseline at commit `ebeac389b1c309f1ef8f5a9056e96c3b28e08e01` passed 70 tests. The current count should always be obtained by running the suite.

## Coverage

```bash
.venv/Scripts/python.exe -m pytest tests -q \
  --cov=src \
  --cov-branch \
  --cov-report=term-missing \
  --cov-report=html
```

Coverage output is generated locally and ignored. It is a testing diagnostic, not permanent governance evidence.

## Integrity checks

```bash
.venv/Scripts/python.exe scripts/generate_hardening_manifest.py
.venv/Scripts/python.exe -c "from pathlib import Path; from run_paper import load_paper_configuration; from src.forward_governance import economic_spec_hash_v2, locked_strategy_hash, verify_trust_anchors; root=Path('.').resolve(); config,_=load_paper_configuration(root); print(locked_strategy_hash(config)); print(economic_spec_hash_v2(config)); print(verify_trust_anchors(root,config))"
.venv/Scripts/python.exe -c "from pathlib import Path; from src.hardening_manifest import verify_hardening_manifest; print(verify_hardening_manifest(Path('.'), Path('forward_experiment/hardening_manifest.json')))"
```

Regenerate the active manifest only after reviewing that changes are non-strategy maintenance. The generator refuses to proceed if the locked strategy hash or governance trust anchors differ. The frozen baseline manifest remains archived separately.

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
- paper quote, post-scaling quantity-rule, non-negative-cash, state, reconciliation and notification tests;
- property-based portfolio invariants;
- real subprocess lock contention/recovery tests;
- backup/temporary-restore and corruption tests;
- scheduler and repeated missed-window tests;
- legacy and comprehensive economic governance, protocol, manifest and prohibited-private-path tests.

## Mutation testing limitation

Mutation testing was attempted but not completed on the validated Windows/Python 3.11 environment:

- `mutmut` reported that native Windows is unsupported;
- `mutatest` installed only with an explicit `setuptools` compatibility dependency;
- the bounded run failed on Python 3.11 AST handling before producing a mutation score.

No mutation score is claimed. This limitation should not be relabeled as a pass.

## CI

`.github/workflows/ci.yml` runs on pushes and pull requests with read-only repository permissions. It installs the hash-pinned lock, compiles source/tests, verifies the public-only boundary, audits dependencies, checks trust anchors and the active hardening manifest, and runs the complete suite.