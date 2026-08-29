from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ci_runs_ruff_and_branch_coverage_gate():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "python -m ruff check ." in workflow
    assert "--cov=src" in workflow
    assert "--cov=run_paper" in workflow
    assert "--cov-branch" in workflow
    assert "--cov-fail-under=75" in workflow


def test_quality_gate_configuration_is_protected():
    generator = (ROOT / "scripts" / "generate_hardening_manifest.py").read_text(
        encoding="utf-8"
    )

    assert '"pyproject.toml"' in generator
