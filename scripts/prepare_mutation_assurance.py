"""Prepare an isolated package workspace for bounded mutation testing."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MUTATION_WORKSPACE_NAME = "hcl-mutation-assurance"
MUTATION_TARGETS = (
    "src/config_validation.py",
    "src/schedule.py",
    "src/costs.py",
)
MUTATION_TEST = "tests/test_mutation_assurance.py"


def mutation_temporary_root() -> Path:
    """Use the runner scratch path or Hermes scratch, never the OS temp dir."""
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp).resolve()

    hermes_home = os.environ.get("HERMES_HOME")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if hermes_home:
        return (Path(hermes_home) / "cache" / "scratch").resolve()
    if local_app_data:
        return (Path(local_app_data) / "hermes" / "cache" / "scratch").resolve()
    raise RuntimeError("set RUNNER_TEMP or Hermes scratch environment before running")


def prepare_mutation_workspace(project_root: Path, workspace: Path) -> None:
    """Copy the bounded targets under a package name supported by mutmut."""
    project_root = Path(project_root).resolve()
    workspace = Path(workspace).resolve()
    if workspace.exists():
        if not workspace.is_dir() or any(workspace.iterdir()):
            raise FileExistsError("mutation workspace must be a new or empty directory")
    else:
        workspace.mkdir(parents=True)

    package_root = workspace / "mutation_targets"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    for relative in MUTATION_TARGETS:
        source = project_root / relative
        if not source.is_file():
            raise FileNotFoundError(f"mutation target is missing: {relative}")
        shutil.copy2(source, package_root / source.name)

    tests_root = workspace / "tests"
    tests_root.mkdir()
    test_source = project_root / MUTATION_TEST
    if not test_source.is_file():
        raise FileNotFoundError(f"mutation test is missing: {MUTATION_TEST}")
    shutil.copy2(test_source, tests_root / Path(MUTATION_TEST).name)

    config = '''[tool.mutmut]
source_paths = ["mutation_targets/"]
only_mutate = [
    "mutation_targets/config_validation.py",
    "mutation_targets/schedule.py",
    "mutation_targets/costs.py",
]
pytest_add_cli_args_test_selection = ["tests/test_mutation_assurance.py"]
'''
    (workspace / "pyproject.toml").write_text(config, encoding="utf-8", newline="\n")


def main() -> int:
    temporary_root = mutation_temporary_root()
    temporary_root.mkdir(parents=True, exist_ok=True)
    workspace = temporary_root / MUTATION_WORKSPACE_NAME
    if workspace.is_symlink() or (workspace.exists() and not workspace.is_dir()):
        raise RuntimeError("mutation workspace path is not a regular directory")
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError("mutation workspace already contains data")
    prepare_mutation_workspace(PROJECT_ROOT, workspace)
    print(f"Prepared isolated mutation workspace: {workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
