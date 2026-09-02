"""Regenerate the active critical-file manifest without touching frozen research records."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from run_paper import load_paper_configuration
from src.forward_governance import (
    ECONOMIC_SPEC_HASH_V2_SHA256,
    economic_spec_hash_v2,
    locked_strategy_hash,
    verify_trust_anchors,
)

FROZEN_BASELINE_COMMIT = "ebeac389b1c309f1ef8f5a9056e96c3b28e08e01"
REWRITTEN_PUBLIC_BASELINE_COMMIT = "1ae75af22c1cf09cf3179823647f7f5a40f845c7"
LOCKED_STRATEGY_SHA256 = "29451632091c5cf6d33cd58a03a2bd5a1bf52297a21375b9ae5e5b6fbbbac2d6"
EXECUTION_PROTOCOL = "paper-exec-v3-ask-bid-minspread-utc0010"

STATIC_CRITICAL_FILES = (
    ".env.example",
    ".gitattributes",
    ".github/workflows/ci.yml",
    ".gitignore",
    ".python-version",
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "audits/README.md",
    "data/README.md",
    "docs/architecture.md",
    "docs/execution-model.md",
    "docs/forward-validation.md",
    "docs/governance.md",
    "docs/methodology.md",
    "docs/operations.md",
    "docs/public-history-rewrite.md",
    "docs/repository-policy.md",
    "docs/testing.md",
    "config/assets.yaml",
    "config/strategy.yaml",
    "forward_experiment/checkpoint_manifest.json",
    "forward_experiment/execution_protocol_v2.json",
    "forward_experiment/execution_protocol_v3.json",
    "forward_experiment/governance.json",
    "forward_experiment/governance_amendment_v2.json",
    "forward_experiment/governance_amendment_v3_economic_spec.json",
    "forward_experiment/governance_amendment_v4_quote_coherence.json",
    "forward_experiment/paper_schema.sql",
    "forward_experiment/quote_coherence_contract_v1.json",
    "forward_experiment/scheduler_manifest.json",
    "requirements.lock",
    "requirements.txt",
    "pyproject.toml",
    "run_backtest.py",
    "run_data_pipeline.py",
    "run_experiments.py",
    "run_monthly_report.py",
    "run_paper.py",
    "scripts/backup_forward.py",
    "scripts/check_markdown_links.py",
    "scripts/generate_hardening_manifest.py",
    "scripts/hermes_gateway_watchdog.ps1",
    "scripts/paper_forward_audit.py",
    "scripts/paper_forward_monthly.py",
    "scripts/interpreter.py",
    "scripts/paper_forward_weekly.py",
    "scripts/verify_safety.py",
    "scripts/verify_scheduler_manifest.py",
    "src/backtest.py",
    "src/backup_restore.py",
    "src/currency_reporting.py",
    "src/config.py",
    "src/data_integrity.py",
    "src/execution_protocol.py",
    "src/forward_governance.py",
    "src/forward_operations.py",
    "src/hardening_manifest.py",
    "src/paper_broker.py",
    "src/paper_forward.py",
    "src/paper_market.py",
    "src/paper_notifications.py",
    "src/paper_report.py",
    "src/paper_store.py",
    "src/statistical_diagnostics.py",
    "src/strategy.py",
    "tests/test_ci_quality.py",
    "tests/test_config.py",
    "tests/test_forward_operations.py",
    "tests/test_forward_safety.py",
    "tests/test_paper_config.py",
    "tests/test_paper_market.py",
    "tests/test_paper_notifications.py",
    "tests/test_paper_report.py",
    "tests/test_paper_trading.py",
    "tests/test_statistical_diagnostics.py",
    "tests/test_runner_safety.py",
    "tests/test_scheduler_contract.py",
)

ECONOMIC_ENTRYPOINTS = (
    "run_data_pipeline.py",
    "run_backtest.py",
    "run_experiments.py",
    "run_paper.py",
)


def _local_imports(project_root: Path, relative: str) -> set[str]:
    tree = ast.parse((project_root / relative).read_text(encoding="utf-8"), filename=relative)
    imports: set[str] = set()
    for node in ast.walk(tree):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
        candidates = ([module] if module else []) + names
        for candidate in candidates:
            if not candidate or not candidate.startswith("src."):
                continue
            path = candidate.replace(".", "/") + ".py"
            if (project_root / path).is_file():
                imports.add(path)
    return imports


def discover_critical_source_files(project_root: Path) -> tuple[str, ...]:
    """Return the transitive local dependencies of economic entry points."""
    pending = list(ECONOMIC_ENTRYPOINTS)
    discovered: set[str] = set()
    while pending:
        relative = pending.pop()
        for dependency in _local_imports(project_root, relative):
            if dependency not in discovered:
                discovered.add(dependency)
                pending.append(dependency)
    return tuple(sorted(discovered))


CRITICAL_FILES = tuple(
    dict.fromkeys((*STATIC_CRITICAL_FILES, *discover_critical_source_files(PROJECT_ROOT)))
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate(project_root: Path, output_path: Path) -> dict:
    project_root = project_root.resolve()
    output_path = output_path.resolve()

    config, _values = load_paper_configuration(project_root)
    if locked_strategy_hash(config) != LOCKED_STRATEGY_SHA256:
        raise ValueError("locked strategy hash changed; refusing to regenerate manifest")
    if economic_spec_hash_v2(config) != ECONOMIC_SPEC_HASH_V2_SHA256:
        raise ValueError("economic specification v2 changed; refusing to regenerate manifest")
    verify_trust_anchors(project_root, config)

    missing = [relative for relative in CRITICAL_FILES if not (project_root / relative).is_file()]
    if missing:
        raise FileNotFoundError(f"critical manifest files are missing: {missing}")

    payload = {
        "purpose": "active publication-and-operations critical-file manifest",
        "frozen_forward_baseline_commit": FROZEN_BASELINE_COMMIT,
        "rewritten_public_baseline_commit": REWRITTEN_PUBLIC_BASELINE_COMMIT,
        "public_history_rewrite_document": "docs/public-history-rewrite.md",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution_protocol": EXECUTION_PROTOCOL,
        "locked_strategy_sha256": LOCKED_STRATEGY_SHA256,
        "economic_spec_v2_sha256": ECONOMIC_SPEC_HASH_V2_SHA256,
        "preserved_protocol_records": ["paper-exec-v2-ask-bid-utc0010"],
        "archived_baseline_manifest": "forward_experiment/baselines/ebeac389/hardening_manifest.json",
        "threat_model": (
            "tamper-evident under Git plus code-anchored hashes; not immutable against "
            "compromise of Git history and verifier together"
        ),
        "files": {relative: _sha256(project_root / relative) for relative in CRITICAL_FILES},
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest_hash = _sha256(output_path)
    sidecar = Path(str(output_path) + ".sha256")
    sidecar.write_text(
        f"{manifest_hash} {output_path.name}\n", encoding="ascii", newline="\n"
    )
    return {
        "manifest": str(output_path.relative_to(project_root)),
        "manifest_sha256": manifest_hash,
        "files": len(CRITICAL_FILES),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regenerate the active critical-file manifest after non-strategy maintenance"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("forward_experiment/hardening_manifest.json"),
    )
    args = parser.parse_args()
    root = PROJECT_ROOT
    print(json.dumps(generate(root, root / args.output), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
