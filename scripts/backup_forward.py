"""CLI for non-overwriting forward-paper backup and temporary restore verification."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from run_paper import _project_paths, load_paper_configuration  # noqa: E402
from src.backup_restore import (  # noqa: E402
    create_verified_backup,
    verify_backup,
    verify_restore_to_temporary,
)
from src.config import load_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--output-root", default="backups")
    verify = subparsers.add_parser("verify")
    verify.add_argument("backup_dir")
    args = parser.parse_args()

    settings = load_settings()
    config, values = load_paper_configuration(settings.project_root)
    database_path, _reports = _project_paths(settings.project_root, values)
    if args.command == "create":
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=settings.project_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        output_root = Path(args.output_root)
        if not output_root.is_absolute():
            output_root = settings.project_root / output_root
        backup = create_verified_backup(
            project_root=settings.project_root,
            database_path=database_path,
            output_root=output_root,
            lock_path=settings.project_root / "runtime" / "forward_writer.lock",
            timestamp=timestamp,
            commit_hash=commit,
        )
        print(json.dumps({"status": "VERIFIED", "backup": str(backup)}, indent=2))
        return

    backup = Path(args.backup_dir).resolve()
    verification = verify_backup(backup)
    with tempfile.TemporaryDirectory(prefix="hcl_restore_verify_") as parent:
        temporary_restore = Path(parent) / "restore"
        restore = verify_restore_to_temporary(backup, temporary_restore)
    print(
        json.dumps(
            {
                "status": "VERIFIED",
                "backup": str(backup),
                "verification": verification,
                "temporary_restore_valid": restore["valid"],
                "production_database_untouched": restore["production_database_untouched"],
                "locked_candidate": config.locked_candidate_id,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
