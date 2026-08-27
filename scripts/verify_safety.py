"""Fail-closed source scan for prohibited private exchange and credential paths."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

PROHIBITED = {
    "real order method": re.compile(r"\.(?:create_order|createOrder)\s*\("),
    "private balance method": re.compile(r"\.(?:fetch_balance|fetchBalance)\s*\("),
    "withdrawal method": re.compile(r"\.(?:withdraw|withdrawal)\s*\("),
    "leverage method": re.compile(r"\.(?:set_leverage|setLeverage)\s*\("),
    "private credential assignment": re.compile(
        r"\b(?:apiKey|api_key|apiSecret|api_secret|BINANCE_API_KEY|BINANCE_SECRET)\s*="
    ),
}


def scan(root: Path) -> list[tuple[str, str, int]]:
    findings: list[tuple[str, str, int]] = []
    candidates = [root / "src"] + [
        root / name
        for name in ("run_paper.py", "run_monthly_report.py", "run_weekly.py")
    ]
    files = []
    for candidate in candidates:
        if candidate.is_dir():
            files.extend(candidate.rglob("*.py"))
        elif candidate.is_file():
            files.append(candidate)
    for path in sorted(files):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for category, pattern in PROHIBITED.items():
                if pattern.search(line):
                    findings.append(
                        (category, str(path.relative_to(root)).replace("\\", "/"), line_number)
                    )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    findings = scan(args.root.resolve())
    if findings:
        for category, path, line in findings:
            print(f"PROHIBITED category={category} path={path} line={line}")
        return 1
    print("PASS: no prohibited private exchange or credential path found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
