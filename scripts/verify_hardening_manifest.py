"""Verify critical-source coverage and the active hardening manifest."""

from __future__ import annotations

from pathlib import Path

from scripts.generate_hardening_manifest import verify_manifest_critical_source_coverage
from src.hardening_manifest import verify_hardening_manifest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "forward_experiment" / "hardening_manifest.json"


def main() -> None:
    verify_manifest_critical_source_coverage(ROOT, MANIFEST)
    print(verify_hardening_manifest(ROOT, MANIFEST))


if __name__ == "__main__":
    main()
