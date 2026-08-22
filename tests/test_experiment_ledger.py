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
