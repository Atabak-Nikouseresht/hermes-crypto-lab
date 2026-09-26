from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REQUIRED_STAT_FIELDS = (
    "killed",
    "survived",
    "total",
    "no_tests",
    "skipped",
    "suspicious",
    "timeout",
    "check_was_interrupted_by_user",
    "segfault",
)
DEFAULT_STATS_PATH = Path("mutants/mutmut-cicd-stats.json")


def summarize_mutation_score(
    stats: dict[str, Any], minimum_score: int = 80
) -> tuple[int, int, float]:
    """Return successful mutants, tested mutants and percentage; reject bad runs."""
    if type(minimum_score) is not int or not 1 <= minimum_score <= 100:
        raise ValueError("minimum_score must be an exact integer in [1, 100]")
    if type(stats) is not dict:
        raise ValueError("mutmut statistics must be a JSON object")

    counts: dict[str, int] = {}
    for field in REQUIRED_STAT_FIELDS:
        value = stats.get(field)
        if type(value) is not int or value < 0:
            raise ValueError(f"mutmut statistic {field!r} must be a nonnegative integer")
        counts[field] = value

    if counts["check_was_interrupted_by_user"]:
        raise ValueError("mutmut run was interrupted")
    if counts["segfault"]:
        raise ValueError("mutmut reported a segmentation fault")

    total = counts["total"]
    skipped = counts["skipped"]
    tested = total - skipped
    if total <= 0 or tested <= 0:
        raise ValueError("mutmut produced no tested mutants")
    successes = counts["killed"] + counts["timeout"]
    if successes > tested:
        raise ValueError("mutmut success count exceeds tested mutants")

    score = successes * 100.0 / tested
    if successes * 100 < minimum_score * tested:
        raise ValueError(
            f"mutation score {score:.2f}% is below the required {minimum_score}%"
        )
    return successes, tested, score


def verify_mutation_score(stats_path: Path, minimum_score: int = 80) -> str:
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read mutmut statistics at {stats_path}") from exc

    successes, tested, score = summarize_mutation_score(stats, minimum_score)
    return (
        f"Mutation score {score:.2f}% ({successes}/{tested}); "
        f"required {minimum_score}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum-score", type=int, default=80)
    args = parser.parse_args()

    exported = subprocess.run(
        [sys.executable, "-m", "mutmut", "export-cicd-stats"],
        check=False,
        capture_output=True,
        text=True,
    )
    if exported.returncode != 0:
        print("FAIL: mutmut could not export CI statistics", file=sys.stderr)
        return 1
    if not DEFAULT_STATS_PATH.is_file():
        print("FAIL: mutmut did not produce its CI statistics file", file=sys.stderr)
        return 1

    try:
        message = verify_mutation_score(DEFAULT_STATS_PATH, args.minimum_score)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"PASS: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
