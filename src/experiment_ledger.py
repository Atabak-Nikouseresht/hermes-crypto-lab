"""Append-only, hash-chained experiment ledger."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


class ExperimentLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._finalized = False
        self._last_hash = "0" * 64
        self._sequence = 0
        if self.path.exists() and self.path.stat().st_size:
            records = self._read_records()
            if not self.verify():
                raise ValueError(f"Ledger hash chain is invalid: {self.path}")
            self._last_hash = records[-1]["record_hash"]
            self._sequence = int(records[-1]["sequence"]) + 1

    @staticmethod
    def _canonical(record: dict[str, Any]) -> bytes:
        return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def append(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._finalized:
            raise PermissionError("Experiment ledger has been finalized")
        record = {
            "sequence": self._sequence,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "previous_hash": self._last_hash,
            **payload,
        }
        record_hash = hashlib.sha256(self._canonical(record)).hexdigest()
        stored = {**record, "record_hash": record_hash}
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(stored, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._last_hash = record_hash
        self._sequence += 1
        return stored

    def verify(self) -> bool:
        previous_hash = "0" * 64
        expected_sequence = 0
        for stored in self._read_records():
            record_hash = stored.get("record_hash")
            unsigned = {key: value for key, value in stored.items() if key != "record_hash"}
            if stored.get("sequence") != expected_sequence:
                return False
            if stored.get("previous_hash") != previous_hash:
                return False
            if hashlib.sha256(self._canonical(unsigned)).hexdigest() != record_hash:
                return False
            previous_hash = record_hash
            expected_sequence += 1
        return True

    @property
    def record_count(self) -> int:
        return self._sequence

    @property
    def final_hash(self) -> str:
        return self._last_hash

    def finalize(self) -> None:
        if not self.verify():
            raise ValueError("Cannot finalize an invalid experiment ledger")
        self._finalized = True
        try:
            self.path.chmod(0o444)
        except OSError:
            pass
