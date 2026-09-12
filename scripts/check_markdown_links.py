"""Fail when tracked Markdown contains a broken local path reference."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def tracked_markdown(project_root: Path = PROJECT_ROOT) -> list[Path]:
    if not (project_root / ".git").exists():
        return sorted(project_root.rglob("*.md"))
    output = subprocess.check_output(
        ["git", "ls-files", "*.md"], cwd=project_root, text=True
    )
    return [project_root / relative for relative in output.splitlines()]


def broken_links(project_root: Path = PROJECT_ROOT) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for document in tracked_markdown(project_root):
        text = document.read_text(encoding="utf-8")
        for target in MARKDOWN_LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            candidate = (document.parent / path_part).resolve()
            try:
                candidate.relative_to(project_root.resolve())
            except ValueError:
                failures.append((str(document.relative_to(project_root)), target))
                continue
            if not candidate.exists():
                failures.append((str(document.relative_to(project_root)), target))
    return failures


def main() -> None:
    failures = broken_links()
    if failures:
        for source, target in failures:
            print(f"BROKEN: {source} -> {target}", file=sys.stderr)
        raise SystemExit(1)
    print("PASS: tracked Markdown local links resolve")


if __name__ == "__main__":
    main()
