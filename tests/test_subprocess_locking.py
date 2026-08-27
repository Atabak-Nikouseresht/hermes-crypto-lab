import json
from pathlib import Path
import subprocess
import sys
import time

from src.forward_operations import InterProcessLock


def _worker(root: Path, lock: Path, mode: str):
    return subprocess.Popen(
        [sys.executable, str(root / "tests" / "helpers" / "lock_worker.py"), str(lock), mode],
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_real_subprocess_writer_contention_is_fail_closed(tmp_path):
    root = Path(__file__).resolve().parents[1]
    lock = tmp_path / "forward.lock"
    holder = _worker(root, lock, "hold")
    assert holder.stdout.readline().strip() == "LOCKED"

    contender = _worker(root, lock, "short")
    stdout, _stderr = contender.communicate(timeout=5)
    holder.terminate()
    holder.wait(timeout=5)

    assert contender.returncode == 23
    assert "CONTENDED" in stdout
    owner = json.loads(lock.with_suffix(".lock.owner.json").read_text(encoding="utf-8"))
    assert owner["pid"] > 0
    assert owner["process_start_time_utc"]
    assert owner["command"] == "worker-hold"


def test_killed_holder_releases_os_lock_and_stale_owner_is_detected(tmp_path):
    root = Path(__file__).resolve().parents[1]
    lock = tmp_path / "forward.lock"
    holder = _worker(root, lock, "hold")
    assert holder.stdout.readline().strip() == "LOCKED"
    holder.kill()
    holder.wait(timeout=5)

    with InterProcessLock(lock, timeout_seconds=2, command_name="recovery") as acquired:
        assert acquired.stale_owner is not None
        assert acquired.stale_owner["pid"] > 0
        assert acquired.stale_owner["command"] == "worker-hold"
