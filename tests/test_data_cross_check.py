import hashlib
import json

import pandas as pd
import pytest

from scripts import data_cross_check
from src.research_data import resolve_canonical_dataset


@pytest.fixture
def governed_publication(tmp_path, monkeypatch):
    """Real schema-v2 pointer, immutable manifest, raw JSON and Parquet."""
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "assets.yaml").write_text(
        "assets: [BTC/USDT, ETH/USDT]\n", encoding="utf-8"
    )
    processed = project / "data" / "processed"
    datasets = {}
    for asset, close in (("BTC/USDT", 101.0), ("ETH/USDT", 202.0)):
        path = _write_canonical_dataset(processed, "active", asset, close)
        raw = project / "data" / "raw" / "active" / (asset.replace("/", "_") + ".json")
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps([[1704412800000, close, close + 1, close - 1, close, 100.0]]))
        datasets[asset] = {
            "path": path.relative_to(processed).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "raw_path": raw.relative_to(project / "data").as_posix(),
            "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            "rows": 1, "raw_rows": 1,
            "start_utc": "2024-01-05T00:00:00Z", "end_utc": "2024-01-05T00:00:00Z",
        }
    manifest = {
        "manifest_schema_version": 2, "run_id": "active", "timeframe": "1d",
        "source": {"exchange_id": "binance", "ccxt_version": "fixture", "since": "2024-01-05T00:00:00Z"},
        "ingestion_git_commit": "a" * 40, "git_dirty": False,
        "version_manifest_path": "active/dataset_manifest.json", "datasets": datasets,
    }

    def publish():
        for path in (processed / "dataset_manifest.json", processed / "active" / "dataset_manifest.json"):
            path.write_text(json.dumps(manifest), encoding="utf-8")

    publish()
    monkeypatch.setattr(data_cross_check, "SAMPLES", ("2024-01-05",))
    monkeypatch.setattr(data_cross_check, "_vision_close", lambda symbol, day: {"BTCUSDT": 101.0, "ETHUSDT": 202.0}[symbol])

    def public_http(url):
        if "coingecko" in url:
            return json.dumps({"prices": [[1704412800000, 101.0], [1704499200000, 101.0]]}).encode()
        return json.dumps([[1704412800000, "0", "0", "0", "101", "0"]]).encode()

    monkeypatch.setattr(data_cross_check, "_get", public_http)
    return project, manifest, publish


def test_ASSET3_governed_universe_order_is_immaterial(governed_publication):
    project, manifest, publish = governed_publication
    manifest["datasets"] = dict(reversed(list(manifest["datasets"].items())))
    publish()
    result = data_cross_check.run(project, project / "audits" / "cross.json")
    assert result["status"] == "PASS"
    assert result["governed_assets"] == ["BTC/USDT", "ETH/USDT"]


@pytest.mark.parametrize("kind", ["reduced", "extra"], ids=["ASSET1-reduced-pointer-and-immutable", "ASSET2-extra-asset"])
def test_governed_set_must_match_exactly(governed_publication, kind):
    project, manifest, publish = governed_publication
    if kind == "reduced":
        del manifest["datasets"]["ETH/USDT"]
    else:
        # Add internally valid extra evidence, not merely an inconsistent alias.
        processed = project / "data" / "processed"
        path = _write_canonical_dataset(processed, "active", "SOL/USDT", 202.0)
        raw = project / "data" / "raw" / "active" / "SOL_USDT.json"
        raw.write_text(json.dumps([[1704412800000, 202.0, 203.0, 201.0, 202.0, 100.0]]))
        manifest["datasets"]["SOL/USDT"] = {
            **manifest["datasets"]["ETH/USDT"],
            "path": path.relative_to(processed).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "raw_path": raw.relative_to(project / "data").as_posix(),
            "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        }
    publish()
    # Both manifests and every referenced artifact are otherwise valid; only
    # independent governance can reject this self-consistent wrong universe.
    resolved, _ = resolve_canonical_dataset(project / "data" / "processed")
    assert set(resolved) == set(manifest["datasets"])
    with pytest.raises(ValueError, match="governed assets"):
        data_cross_check.run(project, project / "audits" / "cross.json")


@pytest.mark.parametrize("caller", ["resolver", "audit"])
def test_governed_universe_rechecked_after_publication_interleaving(
    governed_publication, monkeypatch, caller
):
    from pathlib import Path

    project, manifest_a, _ = governed_publication
    processed = project / "data" / "processed"
    pointer = processed / "dataset_manifest.json"
    payload_a = pointer.read_text(encoding="utf-8")
    manifest_b = json.loads(payload_a)
    manifest_b.update(run_id="next", version_manifest_path="next/dataset_manifest.json")
    manifest_b["datasets"]["SOL/USDT"] = dict(manifest_b["datasets"]["ETH/USDT"])
    for asset, entry in manifest_b["datasets"].items():
        parquet = _write_canonical_dataset(
            processed, "next", asset, 101.0 if asset == "BTC/USDT" else 202.0
        )
        raw = project / "data" / "raw" / "next" / (asset.replace("/", "_") + ".json")
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes((project / "data" / entry["raw_path"]).read_bytes())
        entry.update(
            path=parquet.relative_to(processed).as_posix(),
            sha256=hashlib.sha256(parquet.read_bytes()).hexdigest(),
            raw_path=raw.relative_to(project / "data").as_posix(),
            raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
        )
    payload_b = json.dumps(manifest_b)
    immutable_b = processed / manifest_b["version_manifest_path"]
    immutable_b.write_text(payload_b, encoding="utf-8")
    pointer.write_text(payload_b, encoding="utf-8")
    # B is a fully valid publication, rejected only by independent governance.
    paths, provenance = resolve_canonical_dataset(processed)
    assert set(paths) == {"BTC/USDT", "ETH/USDT", "SOL/USDT"}
    assert provenance["immutable_manifest_path"] == "next/dataset_manifest.json"
    pointer.write_text(payload_a, encoding="utf-8")
    staged_pointer = processed / "dataset_manifest.next.json"
    staged_pointer.write_text(payload_b, encoding="utf-8")
    read_text = Path.read_text
    pointer_reads = 0

    def publish_after_first_read(path, *args, **kwargs):
        nonlocal pointer_reads
        payload = read_text(path, *args, **kwargs)
        if path == pointer:
            pointer_reads += 1
            if pointer_reads == 1:
                # Atomic A -> B publication after capturing A, before rereading.
                staged_pointer.replace(pointer)
        return payload

    monkeypatch.setattr(Path, "read_text", publish_after_first_read)
    output = project / "audits" / "cross.json"
    with pytest.raises(ValueError, match="governed assets"):
        if caller == "resolver":
            resolve_canonical_dataset(processed, expected_assets=list(manifest_a["datasets"]))
        else:
            data_cross_check.run(project, output)
    assert pointer_reads == 2
    assert not output.exists()  # In particular, never persist an audit PASS.
    assert read_text(pointer, encoding="utf-8") == read_text(immutable_b, encoding="utf-8")
    assert read_text(processed / manifest_a["version_manifest_path"], encoding="utf-8") == payload_a


@pytest.mark.parametrize("yaml_text", [
    "", "assets: []", "assets: BTC/USDT", "assets: {BTC/USDT: 1}",
    "assets: [null]", "assets: ['']", "assets: [' BTC/USDT']",
    "assets: [BTCUSDT]", "assets: [BTC/USDT, BTC/USDT]", "[]", "assets: [",
])
def test_ASSET4_malformed_governance_is_rejected(governed_publication, yaml_text):
    project, _, _ = governed_publication
    (project / "config" / "assets.yaml").write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError):
        data_cross_check.run(project, project / "audits" / "cross.json")


@pytest.mark.parametrize("expected", [[], "BTC/USDT", {"BTC/USDT": 1, "ETH/USDT": 1}, [""], [None], ["BTCUSDT"], ["BTC/USDT", "ETH/USDT", "BTC/USDT"]])
def test_ASSET7_resolver_rejects_malformed_expected_universe(governed_publication, expected):
    project, _, _ = governed_publication
    with pytest.raises(ValueError, match="governed assets"):
        resolve_canonical_dataset(project / "data" / "processed", expected_assets=expected)


def test_EMPTY1_no_requested_samples_cannot_pass(governed_publication, monkeypatch):
    project, _, _ = governed_publication
    monkeypatch.setattr(data_cross_check, "SAMPLES", ())
    result = data_cross_check.run(project, project / "audits" / "cross.json")
    assert result["status"] == "FAIL_CLOSED"
    assert result["archive_checks"] == []


def test_EMPTY2_empty_canonical_publication_cannot_pass(governed_publication):
    project, manifest, publish = governed_publication
    manifest["datasets"] = {}
    publish()
    with pytest.raises(ValueError):
        data_cross_check.run(project, project / "audits" / "cross.json")


def test_EMPTY3_unavailable_sample_dates_fail_closed(governed_publication, monkeypatch):
    project, _, _ = governed_publication
    monkeypatch.setattr(data_cross_check, "SAMPLES", ("2019-04-07",))
    result = data_cross_check.run(project, project / "audits" / "cross.json")
    assert result["status"] == "FAIL_CLOSED"
    assert all(row["status"] == "INSUFFICIENT_DATA" for row in result["archive_checks"])
    assert all(row["timestamp_utc"] is None for row in result["archive_checks"])


def test_EMPTY4_each_governed_asset_requires_executable_sample(governed_publication):
    project, manifest, publish = governed_publication
    entry = manifest["datasets"]["ETH/USDT"]
    path = project / "data" / "processed" / entry["path"]
    frame = pd.read_parquet(path)
    frame["timestamp"] = pd.to_datetime(["2024-01-06T00:00:00Z"])
    frame.to_parquet(path, index=False)
    entry.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                 start_utc="2024-01-06T00:00:00Z", end_utc="2024-01-06T00:00:00Z")
    raw = project / "data" / entry["raw_path"]
    rows = json.loads(raw.read_text())
    rows[0][0] = 1704499200000
    raw.write_text(json.dumps(rows))
    entry["raw_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
    publish()
    result = data_cross_check.run(project, project / "audits" / "cross.json")
    assert result["status"] == "FAIL_CLOSED"
    assert {row["asset"] for row in result["archive_checks"] if row["within_tolerance"]} == {"BTC/USDT"}


def test_canonical_provenance_is_complete_relative_and_matches_bytes(governed_publication):
    from pathlib import PureWindowsPath, PurePosixPath

    project, manifest, _ = governed_publication
    output = project / "audits" / "cross.json"
    result = data_cross_check.run(project, output)
    assert json.loads(output.read_text()) == result
    provenance = result["canonical_provenance"]
    assert result["governed_assets"] == sorted(manifest["datasets"])
    assert provenance["run_id"] == manifest["run_id"]
    processed = project / "data" / "processed"
    assert provenance["immutable_manifest_path"] == "active/dataset_manifest.json"
    assert provenance["immutable_manifest_sha256"] == hashlib.sha256(
        (processed / provenance["immutable_manifest_path"]).read_bytes()).hexdigest()
    assert set(provenance["datasets"]) == set(result["governed_assets"])
    for asset, entry in provenance["datasets"].items():
        assert entry["path"] == manifest["datasets"][asset]["path"]
        assert entry["sha256"] == hashlib.sha256((processed / entry["path"]).read_bytes()).hexdigest()
        assert not PureWindowsPath(entry["path"]).is_absolute()
        assert not PurePosixPath(entry["path"]).is_absolute()
    assert str(project) not in output.read_text()
    assert project.as_posix() not in output.read_text()
    assert {row["asset"] for row in result["archive_checks"]} == set(result["governed_assets"])
    for row in result["archive_checks"]:
        path = processed / provenance["datasets"][row["asset"]]["path"]
        assert row["timestamp_utc"] == pd.read_parquet(path)["timestamp"].iloc[0].isoformat()
        assert row["status"] == "PASS"


@pytest.mark.parametrize("observed", [0.0, -101.0, float("nan"), float("inf")])
def test_nonpositive_nonfinite_archive_observations_fail_closed(governed_publication, monkeypatch, observed):
    project, _, _ = governed_publication
    monkeypatch.setattr(data_cross_check, "_vision_close", lambda *_: observed)
    result = data_cross_check.run(project, project / "audits" / "cross.json")
    assert result["status"] == "FAIL_CLOSED"
    # Strict JSON must not contain NaN/Infinity literals.
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("timestamp", [1704499200000, 1704412800000, 1704412800000000])
def test_archive_csv_timestamp_must_match_requested_day(monkeypatch, timestamp):
    import io
    import zipfile

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("BTCUSDT-1d-2024-01-05.csv", f"{timestamp},101,102,100,101,100\n")
    monkeypatch.setattr(data_cross_check, "_get", lambda _: payload.getvalue())
    if timestamp == 1704499200000:
        with pytest.raises(ValueError, match="timestamp"):
            data_cross_check._vision_close("BTCUSDT", "2024-01-05")
    else:
        assert data_cross_check._vision_close("BTCUSDT", "2024-01-05") == 101.0


@pytest.mark.parametrize("kind", ["empty-parquet", "empty-zip", "archive-unavailable"])
def test_EMPTY_no_executable_evidence_cannot_pass(governed_publication, monkeypatch, kind):
    import io
    import zipfile

    project, manifest, publish = governed_publication
    if kind == "empty-parquet":
        entry = manifest["datasets"]["ETH/USDT"]
        path = project / "data" / "processed" / entry["path"]
        pd.read_parquet(path).iloc[:0].to_parquet(path, index=False)
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        publish()
        with pytest.raises(ValueError, match="row-count mismatch"):
            data_cross_check.run(project, project / "audits" / "cross.json")
        return
    if kind == "empty-zip":
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w"):
            pass
        # Exercise the real archive parser, replacing only the HTTP boundary.
        monkeypatch.undo()
        monkeypatch.setattr(data_cross_check, "SAMPLES", ("2024-01-05",))

        def empty_archive_get(url):
            if "data.binance.vision" in url:
                return payload.getvalue()
            if "coingecko" in url:
                return json.dumps({"prices": [[1704412800000, 101.0], [1704499200000, 101.0]]}).encode()
            return json.dumps([[1704412800000, "0", "0", "0", "101", "0"]]).encode()

        monkeypatch.setattr(data_cross_check, "_get", empty_archive_get)
    else:
        def unavailable(*_):
            raise OSError("transport unavailable")
        monkeypatch.setattr(data_cross_check, "_vision_close", unavailable)
    result = data_cross_check.run(project, project / "audits" / "cross.json")
    assert result["status"] == "FAIL_CLOSED"
    assert not any(row["within_tolerance"] for row in result["archive_checks"])


def _write_canonical_dataset(processed, run_id, asset, close):
    path = processed / run_id / f"{asset.replace('/', '_')}_1d.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2024-01-05T00:00:00Z")],
            "open": [close],
            "high": [close + 1],
            "low": [close - 1],
            "close": [close],
            "volume": [100.0],
        }
    ).to_parquet(path, index=False)
    return path


@pytest.mark.parametrize("legacy_close", [999.0, None], ids=["ASSET5-stale-flat-present", "ASSET6-no-flat-files"])
def test_cross_check_resolves_active_versioned_canonical_manifest_not_legacy_file(
    tmp_path, monkeypatch, legacy_close
):
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "assets.yaml").write_text("assets: [BTC/USDT, ETH/USDT]\n", encoding="utf-8")
    processed = project / "data" / "processed"
    btc = _write_canonical_dataset(processed, "active", "BTC/USDT", 101.0)
    eth = _write_canonical_dataset(processed, "active", "ETH/USDT", 202.0)
    manifest = {
        "run_id": "active",
        "timeframe": "1d",
        "version_manifest_path": "active/dataset_manifest.json",
        "datasets": {
            "BTC/USDT": {"path": "active/BTC_USDT_1d.parquet", "sha256": hashlib.sha256(btc.read_bytes()).hexdigest()},
            "ETH/USDT": {"path": "active/ETH_USDT_1d.parquet", "sha256": hashlib.sha256(eth.read_bytes()).hexdigest()},
        },
    }
    (processed / "active" / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # A stale flat file must not be read even if it exists.
    if legacy_close is not None:
        _write_canonical_dataset(processed, ".", "BTC/USDT", legacy_close)
    monkeypatch.setattr(data_cross_check, "SAMPLES", ("2024-01-05",))
    monkeypatch.setattr(data_cross_check, "_vision_close", lambda symbol, _day: {"BTCUSDT": 101.0, "ETHUSDT": 202.0}[symbol])

    def fake_get(url):
        if "coingecko" in url:
            return json.dumps({"prices": [[1704412800000, 101.0], [1704499200000, 101.0]]}).encode()
        return json.dumps([[1704412800000, "0", "0", "0", "101", "0"]]).encode()

    monkeypatch.setattr(data_cross_check, "_get", fake_get)
    result = data_cross_check.run(project, project / "audits" / "cross.json")

    assert result["status"] == "PASS"
    assert result["canonical_provenance"]["run_id"] == "active"
    assert result["canonical_provenance"]["immutable_manifest_path"] == "active/dataset_manifest.json"
    assert {row["processed_close"] for row in result["archive_checks"]} == {101.0, 202.0}


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing", "referenced dataset is missing"),
        ("pointer-mismatch", "pointer and immutable manifest mismatch"),
        ("hash", "hash mismatch"),
    ],
)
def test_cross_check_fails_closed_on_active_canonical_pointer_or_artifact_corruption(
    tmp_path, kind, message
):
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "config" / "assets.yaml").write_text("assets: [BTC/USDT]\n", encoding="utf-8")
    processed = project / "data" / "processed"
    path = _write_canonical_dataset(processed, "active", "BTC/USDT", 101.0)
    manifest = {
        "run_id": "active",
        "timeframe": "1d",
        "version_manifest_path": "active/dataset_manifest.json",
        "datasets": {
            "BTC/USDT": {
                "path": "active/missing.parquet" if kind == "missing" else "active/BTC_USDT_1d.parquet",
                "sha256": "0" * 64 if kind == "hash" else hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        },
    }
    immutable = dict(manifest)
    if kind == "pointer-mismatch":
        immutable["run_id"] = "other"
    (processed / "active" / "dataset_manifest.json").write_text(json.dumps(immutable), encoding="utf-8")
    (processed / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        data_cross_check.run(project, project / "audits" / "cross.json")
