"""Prospective evidence governance must retain its independent trust anchors."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from run_paper import load_paper_configuration
import src.forward_governance as governance

ROOT = Path(__file__).resolve().parents[1]


def test_market_evidence_v2_is_a_code_anchored_prospective_contract():
    config, _ = load_paper_configuration(ROOT)
    verified = governance.verify_trust_anchors(ROOT, config)
    assert "market_rule_evidence_v2_contract" in verified


@pytest.mark.parametrize("field,value", [
    ("max_age_seconds", 301),
    ("max_age_seconds", True),
    ("future_timestamp_permitted", True),
    ("missing_timestamp_permitted_for_nonnull_reference_price", True),
])
def test_v6_reference_contract_rejects_policy_changes(field, value):
    config, _ = load_paper_configuration(ROOT)
    payload = json.loads((ROOT / "forward_experiment/reference_price_evidence_contract_v1.json").read_text())
    payload["rule"][field] = value
    with pytest.raises(ValueError):
        governance.verify_reference_price_evidence_runtime_contract(payload, config)


@pytest.mark.parametrize("field,value", [
    ("version", "binance-market-rule-evidence-v1"),
    ("execution_protocol_version", "paper-exec-v2-ask-bid-utc0010"),
    ("prior_reference_price_governance_amendment_sha256", "0" * 64),
    ("effective_for_new_runs_only", False),
    ("historical_evidence_fabricated", True),
])
def test_v2_contract_semantic_mutations_fail(field, value):
    config, _ = load_paper_configuration(ROOT)
    payload = json.loads((ROOT / "forward_experiment/market_rule_evidence_contract_v2.json").read_text())
    changed = deepcopy(payload)
    changed[field] = value
    with pytest.raises(ValueError):
        governance.verify_market_rule_evidence_v2_runtime_contract(changed, config)


@pytest.mark.parametrize("field,value", [
    ("max_age_seconds", 301),
    ("max_age_seconds", 300.0),
    ("future_timestamp_permitted", True),
    ("missing_timestamp_permitted_for_nonnull_reference_price", True),
    ("acquisition_timestamp_required_for_reference_price", False),
    ("admission_freshness_required", False),
    ("non_reference_sources_require_null_reference_timestamps", False),
])
def test_v2_contract_timing_policy_cannot_drift(field, value):
    config, _ = load_paper_configuration(ROOT)
    payload = json.loads((ROOT / "forward_experiment/market_rule_evidence_contract_v2.json").read_text())
    payload["rule"][field] = value
    with pytest.raises(ValueError, match="freshness policy"):
        governance.verify_market_rule_evidence_v2_runtime_contract(payload, config)


def test_v6_reference_contract_rejects_wrong_execution_protocol():
    config, _ = load_paper_configuration(ROOT)
    payload = json.loads((ROOT / "forward_experiment/reference_price_evidence_contract_v1.json").read_text())
    payload["execution_protocol_version"] = "wrong"
    with pytest.raises(ValueError, match="execution protocol"):
        governance.verify_reference_price_evidence_runtime_contract(payload, config)


@pytest.mark.parametrize("filename", [
    "reference_price_evidence_contract_v1.json",
    "governance_amendment_v6_reference_price_evidence.json",
    "market_rule_evidence_contract_v2.json",
])
def test_contract_tampering_with_refreshed_sidecar_still_fails_code_anchor(tmp_path, filename):
    destination = tmp_path / "forward_experiment"
    destination.mkdir()
    for source in (ROOT / "forward_experiment").iterdir():
        if source.is_file() and source.suffix in {".json", ".sha256"}:
            shutil.copyfile(source, destination / source.name)
    target = destination / filename
    target.write_bytes(target.read_bytes() + b"\n")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    Path(str(target) + ".sha256").write_text(f"{digest} {filename}\n", encoding="ascii")
    config, _ = load_paper_configuration(ROOT)
    with pytest.raises(ValueError, match="trust-anchor"):
        governance.verify_trust_anchors(tmp_path, config)


def test_v6_governance_chain_mismatch_is_rejected_semantically(tmp_path, monkeypatch):
    destination = tmp_path / "forward_experiment"
    destination.mkdir()
    for source in (ROOT / "forward_experiment").iterdir():
        if source.is_file() and source.suffix in {".json", ".sha256"}:
            shutil.copyfile(source, destination / source.name)
    target = destination / "governance_amendment_v6_reference_price_evidence.json"
    payload = json.loads(target.read_text())
    payload["prior_transient_failure_governance_amendment_sha256"] = "0" * 64
    target.write_text(json.dumps(payload), encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    Path(str(target) + ".sha256").write_text(f"{digest} {target.name}\n", encoding="ascii")
    monkeypatch.setattr(governance, "REFERENCE_PRICE_EVIDENCE_GOVERNANCE_AMENDMENT_HASH_SHA256", digest)
    # Also repair the dependent v2 chain and its digest to reach v6 semantics.
    v2 = destination / "market_rule_evidence_contract_v2.json"
    payload = json.loads(v2.read_text())
    payload["prior_reference_price_governance_amendment_sha256"] = digest
    v2.write_text(json.dumps(payload), encoding="utf-8")
    v2_digest = hashlib.sha256(v2.read_bytes()).hexdigest()
    Path(str(v2) + ".sha256").write_text(f"{v2_digest} {v2.name}\n", encoding="ascii")
    monkeypatch.setattr(governance, "MARKET_RULE_EVIDENCE_V2_CONTRACT_HASH_SHA256", v2_digest)
    config, _ = load_paper_configuration(ROOT)
    with pytest.raises(ValueError, match="does not anchor transient-failure governance"):
        governance.verify_trust_anchors(tmp_path, config)
