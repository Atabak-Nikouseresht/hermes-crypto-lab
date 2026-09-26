from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANARY = "vT4mL2qR8sN6pA1xC9zB3kD7fG5hJ0wY"


def _gitleaks() -> str:
    binary = shutil.which("gitleaks")
    if binary is None:
        if os.environ.get("HCL_REQUIRE_GITLEAKS") == "1":
            pytest.fail("Gitleaks is required by this CI job but is not on PATH")
        pytest.skip("Gitleaks is installed by CI before these scanner contract tests")
    return binary


def _scan(binary: str, source: Path, report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            binary,
            "dir",
            str(source),
            "--config",
            str(ROOT / ".gitleaks.toml"),
            "--redact",
            "--no-banner",
            "--exit-code=2",
            "--report-format=json",
            f"--report-path={report}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_gitleaks_configuration_uses_default_rules():
    with (ROOT / ".gitleaks.toml").open("rb") as config_file:
        config = tomllib.load(config_file)

    assert config["extend"]["useDefault"] is True
    assert config["allowlists"] == [
        {
            "description": "Allow only generated SHA-256 values in the active hardening manifest",
            "condition": "AND",
            "regexTarget": "line",
            "paths": [r"(^|/)forward_experiment/hardening_manifest\.json$"],
            "regexes": [r'^\s+"[^"]+": "[0-9a-f]{64}",?$'],
        }
    ]


def test_manifest_hash_allowlist_is_limited_to_that_file(tmp_path):
    binary = _gitleaks()
    digest = hashlib.sha256(b"gitleaks manifest hash fixture").hexdigest()
    manifest_root = tmp_path / "manifest-scan"
    manifest_path = manifest_root / "forward_experiment" / "hardening_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps({"files": {"tests/test_secret_scanner.py": digest}}, indent=2), encoding="utf-8"
    )
    manifest_report = tmp_path / "manifest-report.json"

    manifest_result = _scan(binary, manifest_root, manifest_report)

    assert manifest_result.returncode == 0
    assert digest not in manifest_report.read_text(encoding="utf-8")

    outside_root = tmp_path / "outside-scan"
    outside_root.mkdir()
    outside_path = outside_root / "hashes.json"
    outside_path.write_text(
        json.dumps({"files": {"tests/test_secret_scanner.py": digest}}, indent=2), encoding="utf-8"
    )
    outside_report = tmp_path / "outside-report.json"

    outside_result = _scan(binary, outside_root, outside_report)

    assert outside_result.returncode == 2
    assert digest not in outside_report.read_text(encoding="utf-8")


def test_gitleaks_detects_canary_without_emitting_its_value(tmp_path):
    binary = _gitleaks()
    source = tmp_path / "canary.txt"
    source.write_text(f'password = "{CANARY}"\n', encoding="utf-8")
    report = tmp_path / "canary-report.json"

    result = _scan(binary, tmp_path, report)

    assert result.returncode == 2, "scanner did not detect the synthetic canary"
    findings = json.loads(report.read_text(encoding="utf-8"))
    assert findings
    assert CANARY not in report.read_text(encoding="utf-8")


def test_gitleaks_ignores_sha256_and_git_object_hashes(tmp_path):
    binary = _gitleaks()
    source = tmp_path / "hashes.txt"
    source.write_text(
        "sha256=" + "0123456789abcdef" * 4 + "\n"
        "commit=" + "0123456789abcdef" * 2 + "01234567" + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "hash-report.json"

    result = _scan(binary, tmp_path, report)

    assert result.returncode == 0, "scanner flagged a known hash-only fixture"
