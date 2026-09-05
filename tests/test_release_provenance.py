from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.execution_protocol import EXECUTION_PROTOCOL_VERSION
from src.release_provenance import ReleaseProvenanceError, capture_release_provenance


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _clean_git(command, **_kwargs):
    if command[1:] == ["rev-parse", "HEAD"]:
        return SimpleNamespace(stdout="a" * 40 + "\n")
    if command[1:] == ["status", "--porcelain=v1", "--untracked-files=all"]:
        return SimpleNamespace(stdout="")
    raise AssertionError(command)


def test_release_provenance_captures_exact_clean_local_release(monkeypatch):
    import src.release_provenance as provenance

    monkeypatch.setattr(provenance.subprocess, "run", _clean_git)
    monkeypatch.setattr(
        provenance,
        "verify_hardening_manifest",
        lambda *_args: {"manifest_sha256": "b" * 64},
    )

    captured = capture_release_provenance(ROOT, now=datetime(2026, 9, 4, tzinfo=timezone.utc))

    assert captured.git_commit == "a" * 40
    assert captured.git_dirty is False
    assert captured.hardening_manifest_sha256 == "b" * 64
    assert captured.execution_protocol_version == EXECUTION_PROTOCOL_VERSION


@pytest.mark.parametrize(
    "runner",
    [
        lambda command, **_kwargs: SimpleNamespace(stdout="not-a-sha\n"),
        lambda _command, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("git")),
    ],
)
def test_release_provenance_rejects_unavailable_or_malformed_git(monkeypatch, runner):
    import src.release_provenance as provenance

    monkeypatch.setattr(provenance.subprocess, "run", runner)
    monkeypatch.setattr(
        provenance,
        "verify_hardening_manifest",
        lambda *_args: {"manifest_sha256": "b" * 64},
    )

    with pytest.raises(RuntimeError, match="[Rr]elease provenance"):
        capture_release_provenance(ROOT)


def test_release_provenance_rejects_dirty_git_and_manifest_failure(monkeypatch):
    import src.release_provenance as provenance

    def dirty(command, **_kwargs):
        if command[1] == "rev-parse":
            return SimpleNamespace(stdout="a" * 40 + "\n")
        return SimpleNamespace(stdout=" M src/paper_broker.py\n")

    monkeypatch.setattr(provenance.subprocess, "run", dirty)
    monkeypatch.setattr(
        provenance,
        "verify_hardening_manifest",
        lambda *_args: {"manifest_sha256": "b" * 64},
    )
    with pytest.raises(RuntimeError, match="dirty"):
        capture_release_provenance(ROOT)

    monkeypatch.setattr(provenance.subprocess, "run", _clean_git)
    monkeypatch.setattr(
        provenance,
        "verify_hardening_manifest",
        lambda *_args: (_ for _ in ()).throw(ValueError("manifest")),
    )
    with pytest.raises(RuntimeError, match="hardening"):
        capture_release_provenance(ROOT)


@pytest.mark.parametrize("runner,retryable", [
    (lambda _command, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("git")), True),
    (lambda _command, **_kwargs: SimpleNamespace(stdout="bad\n"), False),
])
def test_release_provenance_git_failures_are_typed(monkeypatch, runner, retryable):
    import src.release_provenance as provenance

    monkeypatch.setattr(provenance.subprocess, "run", runner)
    with pytest.raises(ReleaseProvenanceError) as error:
        capture_release_provenance(ROOT)
    assert error.value.retryable is retryable
