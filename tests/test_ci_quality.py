from pathlib import Path
import re
import tomllib

import yaml

from scripts.prepare_mutation_assurance import MUTATION_TARGETS

ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_ruff_and_branch_coverage_gate():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python -m ruff check ." in workflow
    assert "python -m mypy\n" in workflow
    assert "python -m ruff check src run_backtest.py run_data_pipeline.py" in workflow
    assert "--select S --ignore S101,S603,S607,S310" in workflow
    assert "--cov=src" in workflow
    assert "--cov=run_paper" in workflow
    assert "--cov-branch" in workflow
    assert "--cov-fail-under=75" in workflow
    for path, minimum in (
        ("run_paper.py", 80),
        ("src/paper_forward.py", 80),
        ("src/paper_broker.py", 85),
        ("src/paper_store.py", 85),
        ("src/forward_operations.py", 85),
        ("src/statistical_diagnostics.py", 80),
        ("src/download_data.py", 80),
        ("src/experiment_runner.py", 80),
        ("src/research_data.py", 85),
        ("scripts/paper_forward_monthly.py", 80),
        ("src/forward_governance.py", 80),
        ("src/paper_notifications.py", 82),
        ("src/backup_restore.py", 78),
        ("src/paper_market.py", 80),
        ("src/scheduler_contract.py", 70),
        ("src/forward_counterfactual.py", 80),
    ):
        assert (
            f"python -m coverage report --precision=2 --include={path} "
            f"--fail-under={minimum}" in workflow
        )


def test_quality_gate_configuration_is_protected():
    generator = (ROOT / "scripts" / "generate_hardening_manifest.py").read_text(
        encoding="utf-8"
    )

    assert '"pyproject.toml"' in generator


def test_hardening_manifest_text_inputs_have_portable_line_endings():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

    assert ".gitleaks.toml text eol=lf" in attributes
    assert "requirements-quality.in text eol=lf" in attributes
    assert "requirements-quality.lock text eol=lf" in attributes
    assert "tests/*.py text eol=lf" in attributes


def test_mypy_targets_are_centralized_and_cover_critical_modules():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mypy_config = config["tool"]["mypy"]
    targets = mypy_config["files"]

    assert mypy_config.get("ignore_missing_imports", False) is False
    assert {
        "src/config_validation.py",
        "src/config.py",
        "src/costs.py",
        "src/metrics.py",
        "src/schedule.py",
        "src/scheduler_contract.py",
        "src/data_integrity.py",
        "src/release_provenance.py",
        "src/forward_counterfactual.py",
        "src/paper_notifications.py",
        "src/scheduler_deployment.py",
    } <= set(targets)


def test_gitleaks_is_pinned_redacted_and_least_privilege():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e" in workflow
    assert "GITLEAKS_VERSION: 8.30.1" in workflow
    assert "GITLEAKS_CONFIG: .gitleaks.toml" in workflow
    assert 'GITLEAKS_ENABLE_UPLOAD_ARTIFACT: "false"' in workflow
    assert 'GITLEAKS_ENABLE_COMMENTS: "false"' in workflow
    assert "GITHUB_TOKEN: ${{ github.token }}" in workflow
    assert "pull-requests: read" in workflow
    assert "Path('results.sarif').unlink(missing_ok=True)" in workflow
    assert "gitleaks dir . --redact" in workflow
    parsed = yaml.load(workflow, Loader=yaml.BaseLoader)
    steps = parsed["jobs"]["verify"]["steps"]
    action_step = next(
        step for step in steps if step.get("name") == "Scan secrets in pull request or push"
    )
    tree_scan_step = next(
        step for step in steps if step.get("name") == "Scan checked-out repository contents"
    )
    assert action_step["if"] == "runner.os == 'Linux'"
    assert tree_scan_step["if"] == "runner.os == 'Linux'"
    canary_step = next(
        step for step in steps if step.get("name") == "Test Gitleaks canary and hash policy"
    )
    assert canary_step["if"] == "runner.os == 'Linux'"


def test_weekly_assurance_workflow_runs_deep_security_and_mutation_gates():
    workflow_path = ROOT / ".github" / "workflows" / "scheduled-assurance.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "prepare_mutation_assurance.py" in workflow
    assert re.search(r"schedule:\s*\n\s+- cron: ['\"]\d+ \d+ \* \* \d['\"]", workflow)
    assert "fetch-depth: 0" in workflow
    assert "pip check" in workflow
    assert "pip_audit" in workflow
    assert "gitleaks detect" in workflow
    assert "--log-opts=--all" in workflow
    assert "mutmut run" in workflow
    assert "verify_mutation_score.py" in workflow
    assert "verify_safety.py" in workflow
    assert "verify_scheduler_manifest.py" in workflow
    assert "python -m scripts.verify_hardening_manifest" in workflow
    assert re.findall(r"uses:\s+[^\s@]+@([^\s]+)", workflow)
    assert all(
        re.fullmatch(r"[0-9a-f]{40}", revision)
        for revision in re.findall(r"uses:\s+[^\s@]+@([^\s]+)", workflow)
    )
    assert "timeout-minutes:" in workflow
    assert "contents: read" in workflow
    assert "pull-requests: read" in workflow
    assert "pip install --require-hashes -r requirements.lock" in workflow
    assert "pip install --require-hashes -r requirements-quality.lock" in workflow
    assert "timeout-minutes: 60" in workflow
    assert "working-directory: ${{ runner.temp }}/hcl-mutation-assurance" in workflow
    assert "HCL_MUTATION_PACKAGE: mutation_targets" in workflow
    assert "--minimum-score 80" in workflow
    assert MUTATION_TARGETS == (
        "src/config_validation.py",
        "src/schedule.py",
        "src/costs.py",
    )


def test_workflow_yaml_parses_and_allows_manual_assurance_on_feature_branches():
    ci = yaml.load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assurance = yaml.load(
        (ROOT / ".github" / "workflows" / "scheduled-assurance.yml").read_text(
            encoding="utf-8"
        ),
        Loader=yaml.BaseLoader,
    )

    assert set(ci["on"]) == {"pull_request", "push"}
    assert set(assurance["on"]) == {"schedule", "workflow_dispatch"}
    assert assurance["on"]["schedule"][0]["cron"] == "30 6 * * 1"
    assert assurance["jobs"]["assurance"]["if"] == (
        "github.ref == 'refs/heads/main' || github.event_name == 'workflow_dispatch'"
    )
