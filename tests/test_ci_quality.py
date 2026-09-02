from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_ruff_and_branch_coverage_gate():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python -m ruff check ." in workflow
    assert "python -m mypy src/validate_data.py src/storage.py src/hardening_manifest.py" in workflow
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
        ("scripts/paper_forward_monthly.py", 80),
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
