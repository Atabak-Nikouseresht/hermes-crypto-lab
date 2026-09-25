"""Append-only, hash-chained experiment ledger."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


class ExperimentLedger:
    _RESERVED_KEYS = frozenset({"sequence", "recorded_at_utc", "previous_hash", "record_hash"})

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seal_path = self.path.with_name(self.path.name + ".sealed")
        self._finalized = False
        self._last_hash = "0" * 64
        self._sequence = 0
        if self.path.exists() and self.path.stat().st_size:
            records = self._read_records()
            if not self.verify():
                raise ValueError(f"Ledger hash chain is invalid: {self.path}")
            self._last_hash = records[-1]["record_hash"]
            self._sequence = int(records[-1]["sequence"]) + 1
        if self._seal_path.exists():
            expected = {"final_hash": self._last_hash, "record_count": self._sequence}
            try:
                seal = json.loads(self._seal_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Ledger seal is invalid: {self._seal_path}") from exc
            if seal != expected:
                raise ValueError(f"Ledger seal does not match ledger: {self.path}")
            self._finalized = True
        else:
            self._finalized = self._has_historical_seal()

    def _has_historical_seal(self) -> bool:
        """Recognize the legacy run manifest without rewriting historical ledgers."""
        manifest_path = self.path.with_name("ledger_manifest.json")
        if not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Historical ledger manifest is invalid: {manifest_path}") from exc
        if type(manifest) is not dict or manifest.get("ledger") != self.path.name:
            return False
        expected_keys = {
            "ledger", "record_count", "final_hash", "verified_before_finalize",
            "read_only_after_finalize",
        }
        if (
            set(manifest) != expected_keys
            or type(manifest["record_count"]) is not int
            or type(manifest["verified_before_finalize"]) is not bool
            or type(manifest["read_only_after_finalize"]) is not bool
            or manifest["verified_before_finalize"] is not True
            or manifest["read_only_after_finalize"] is not True
            or manifest["record_count"] != self._sequence
            or manifest["final_hash"] != self._last_hash
        ):
            raise ValueError(f"Historical ledger manifest does not seal ledger: {self.path}")
        return True

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
        collisions = self._RESERVED_KEYS.intersection(payload)
        if collisions:
            names = ", ".join(sorted(collisions))
            raise ValueError(f"Payload contains reserved ledger metadata: {names}")
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
        seal = {"final_hash": self._last_hash, "record_count": self._sequence}
        try:
            with self._seal_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(seal, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            existing = json.loads(self._seal_path.read_text(encoding="utf-8"))
            if existing != seal:
                raise ValueError(f"Ledger seal does not match ledger: {self.path}") from None
        self._finalized = True
        try:
            self.path.chmod(0o444)
        except OSError:
            pass
