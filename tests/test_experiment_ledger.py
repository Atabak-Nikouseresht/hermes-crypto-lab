import json

import pytest

from src.experiment_ledger import ExperimentLedger


def test_ledger_is_hash_chained_verifiable_and_read_only_after_finalize(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = ExperimentLedger(path)
    ledger.append({"stage": "training", "candidate_id": "a", "score": 1.0})
    ledger.append({"stage": "validation", "candidate_id": "a", "score": 0.8})

    assert ledger.verify()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[1]["previous_hash"] == records[0]["record_hash"]

    ledger.finalize()
    with pytest.raises(PermissionError):
        ledger.append({"stage": "test", "candidate_id": "a"})


def test_finalization_survives_new_instance_and_permission_changes(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = ExperimentLedger(path)
    ledger.append({"stage": "training"})
    ledger.finalize()
    original_bytes = path.read_bytes()

    # Permission bits are advisory and may be ignored or reset on another OS.
    path.chmod(0o644)
    reopened = ExperimentLedger(path)
    with pytest.raises(PermissionError, match="finalized"):
        reopened.append({"stage": "validation"})
    assert path.read_bytes() == original_bytes
    assert reopened.final_hash == ledger.final_hash
    assert reopened.record_count == ledger.record_count


def test_historical_ledger_manifest_is_authoritative_seal(tmp_path):
    path = tmp_path / "experiment_ledger.jsonl"
    ledger = ExperimentLedger(path)
    ledger.append({"stage": "training"})
    path.with_name("ledger_manifest.json").write_text(
        json.dumps({
            "ledger": path.name,
            "record_count": ledger.record_count,
            "final_hash": ledger.final_hash,
            "verified_before_finalize": True,
            "read_only_after_finalize": True,
        }),
        encoding="utf-8",
    )
    original_bytes = path.read_bytes()

    reopened = ExperimentLedger(path)
    with pytest.raises(PermissionError, match="finalized"):
        reopened.append({"stage": "new"})

    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("reserved", ["sequence", "recorded_at_utc", "previous_hash", "record_hash"])
def test_append_rejects_reserved_metadata_before_writing(tmp_path, reserved):
    path = tmp_path / "ledger.jsonl"
    ledger = ExperimentLedger(path)
    ledger.append({"stage": "training"})
    original_bytes = path.read_bytes()
    original_final_hash = ledger.final_hash
    original_record_count = ledger.record_count

    with pytest.raises(ValueError, match="reserved"):
        ledger.append({"stage": "training", reserved: "caller-value"})
    assert path.read_bytes() == original_bytes
    assert ledger.final_hash == original_final_hash
    assert ledger.record_count == original_record_count
