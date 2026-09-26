from __future__ import annotations

import runpy
import subprocess
import sys

from coverage import Coverage


def test_coverage_report_fails_below_configured_threshold(tmp_path):
    source = tmp_path / "critical_branch.py"
    source.write_text(
        "def choose(flag):\n"
        "    if flag:\n"
        "        return 'yes'\n"
        "    return 'no'\n"
        "\n"
        "choose(True)\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"
    coverage = Coverage(data_file=str(data_file), branch=True, include=[str(source)])
    coverage.start()
    runpy.run_path(str(source))
    coverage.stop()
    coverage.save()

    below_floor = subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            f"--data-file={data_file}",
            f"--include={source}",
            "--precision=2",
            "--fail-under=100",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    above_floor = subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            f"--data-file={data_file}",
            f"--include={source}",
            "--precision=2",
            "--fail-under=70",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert below_floor.returncode == 2
    assert above_floor.returncode == 0
