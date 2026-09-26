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
    ".github/workflows/scheduled-assurance.yml",
    ".gitignore",
    ".gitleaks.toml",
    ".python-version",
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "audits/README.md",
    "data/README.md",
    "docs/architecture.md",
    "docs/binance-execution-rules-audit.md",
    "docs/execution-model.md",
    "docs/forward-validation.md",
    "docs/governance.md",
    "docs/methodology.md",
    "docs/operations.md",
    "docs/public-history-rewrite.md",
    "docs/repository-policy.md",
    "docs/testing.md",
    "config/assets.yaml",
    "config/canonical_research.yaml",
    "config/strategy.yaml",
    "forward_experiment/checkpoint_manifest.json",
    "forward_experiment/execution_protocol_v2.json",
    "forward_experiment/execution_protocol_v3.json",
    "forward_experiment/governance.json",
    "forward_experiment/governance_amendment_v2.json",
    "forward_experiment/governance_amendment_v3_economic_spec.json",
    "forward_experiment/governance_amendment_v4_quote_coherence.json",
    "forward_experiment/governance_amendment_v5_transient_failure_semantics.json",
    "forward_experiment/governance_amendment_v6_reference_price_evidence.json",
    "forward_experiment/governance_amendment_v7_execution_rules_price_range.json",
    "forward_experiment/execution_rules_price_range_contract_v1.json",
    "forward_experiment/paper_schema.sql",
    "forward_experiment/quote_coherence_contract_v1.json",
    "forward_experiment/reference_price_evidence_contract_v1.json",
    "forward_experiment/market_rule_evidence_contract_v2.json",
    "forward_experiment/research_lifecycle.json",
    "forward_experiment/scheduler_manifest.json",
    "requirements.lock",
    "requirements-quality.in",
    "requirements-quality.lock",
    "requirements.txt",
    "pyproject.toml",
    "run_backtest.py",
    "run_data_pipeline.py",
    "run_experiments.py",
    "run_monthly_report.py",
    "run_paper.py",
    "scripts/backup_forward.py",
    "scripts/check_markdown_links.py",
    "scripts/data_cross_check.py",
    "scripts/generate_hardening_manifest.py",
    "scripts/hermes_gateway_watchdog.ps1",
    "scripts/read_windows_task_scheduler.ps1",
    "scripts/paper_forward_audit.py",
    "scripts/paper_forward_monthly.py",
    "scripts/interpreter.py",
    "scripts/paper_forward_weekly.py",
    "scripts/sealed_v1_manifest.py",
    "scripts/prepare_mutation_assurance.py",
    "scripts/verify_hardening_manifest.py",
    "scripts/verify_mutation_score.py",
    "scripts/verify_safety.py",
    "scripts/verify_scheduler_deployment.py",
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
    "src/forward_monthly.py",
    "src/statistical_diagnostics.py",
    "src/strategy.py",
    "src/scheduler_contract.py",
    "src/scheduler_deployment.py",
    "tests/test_config_validation.py",
    "tests/test_costs.py",
    "tests/test_ci_quality.py",
    "tests/test_config.py",
    "tests/test_coverage_gate.py",
    "tests/test_forward_operations.py",
    "tests/test_forward_safety.py",
    "tests/test_paper_config.py",
    "tests/test_paper_market.py",
    "tests/test_paper_notifications.py",
    "tests/test_paper_report.py",
    "tests/test_paper_trading.py",
    "tests/test_metrics.py",
    "tests/test_mutation_score.py",
    "tests/test_mutation_assurance.py",
    "tests/test_schedule.py",
    "tests/test_scheduler_deployment.py",
    "tests/test_secret_scanner.py",
    "tests/test_statistical_diagnostics.py",
    "tests/test_validate_data.py",
    "tests/test_runner_safety.py",
    "tests/test_scheduler_contract.py",
)

ECONOMIC_ENTRYPOINTS = (
    "run_data_pipeline.py",
    "run_backtest.py",
    "run_experiments.py",
    "run_paper.py",
    "run_monthly_report.py",
    "scripts/backup_forward.py",
    "scripts/paper_forward_audit.py",
    "scripts/paper_forward_monthly.py",
    "scripts/paper_forward_weekly.py",
)


def _module_file(project_root: Path, module: str) -> str | None:
    if not module:
        return None
    relative = module.replace(".", "/")
    module_file = f"{relative}.py"
    if (project_root / module_file).is_file():
        return module_file
    package_file = f"{relative}/__init__.py"
    if (project_root / package_file).is_file():
        return package_file
    return None


def _local_imports(project_root: Path, relative: str) -> set[str]:
    tree = ast.parse((project_root / relative).read_text(encoding="utf-8"), filename=relative)
    imports: set[str] = set()
    source = (project_root / relative).resolve()
    try:
        package = source.relative_to(project_root.resolve()).parent.parts
    except ValueError:
        package = ()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                path = _module_file(project_root, alias.name)
                if path:
                    imports.add(path)
        elif isinstance(node, ast.ImportFrom):
            base_parts = list(package) if node.level else []
            if node.level:
                base_parts = base_parts[: max(0, len(base_parts) - node.level + 1)]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
            path = _module_file(project_root, base)
            if path:
                imports.add(path)
            for alias in node.names:
                if alias.name == "*":
                    continue
                path = _module_file(
                    project_root, ".".join(filter(None, (base, alias.name)))
                )
                if path:
                    imports.add(path)
    return imports


def discover_critical_source_files(project_root: Path) -> tuple[str, ...]:
    """Return the transitive local dependency closure of governed entry points."""
    roots = set(ECONOMIC_ENTRYPOINTS)
    roots.update(path.name for path in project_root.glob("run_*.py"))
    pending = sorted((root for root in roots if (project_root / root).is_file()), reverse=True)
    # Governed entry points are themselves trust-surface files, including newly
    # added run_*.py files even when they currently have no local imports.
    discovered: set[str] = set(pending)
    while pending:
        relative = pending.pop()
        for dependency in _local_imports(project_root, relative):
            if dependency not in discovered:
                discovered.add(dependency)
                pending.append(dependency)
    return tuple(sorted(discovered))


def verify_critical_source_coverage(
    project_root: Path,
    critical_files: tuple[str, ...] | set[str] | None = None,
) -> None:
    """Fail when a reachable local source dependency is absent from coverage."""
    if critical_files is None:
        critical_files = CRITICAL_FILES
    omitted = sorted(set(discover_critical_source_files(project_root)) - set(critical_files))
    if omitted:
        raise ValueError(f"critical manifest omits reachable local dependencies: {omitted}")


def verify_manifest_critical_source_coverage(
    project_root: Path, manifest_path: Path
) -> None:
    """Require the checked-in active manifest to list every reachable local dependency."""
    try:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("active hardening manifest is malformed") from error
    files = payload.get("files") if type(payload) is dict else None
    if type(files) is not dict:
        raise ValueError("active hardening manifest file inventory is malformed")
    verify_critical_source_coverage(project_root, set(files))


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
    verify_critical_source_coverage(project_root, CRITICAL_FILES)

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
