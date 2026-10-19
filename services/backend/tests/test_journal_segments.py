"""E11: authenticated bounded segments must detect loss, not just invalid ciphertext."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.security import Vault
from fastapi import HTTPException


def vault(tmp_path):
    return Vault(tmp_path, "synthetic-journal-key")


def test_complete_record_removal_and_reordering_are_detected(tmp_path):
    v = vault(tmp_path)
    for n in range(3):
        v.append_journal("effects", {"ordinal": n})
    path = tmp_path / "effects-journal.enc"
    lines = path.read_bytes().splitlines(keepends=True)
    for damaged in (lines[:1] + lines[2:], lines[1:] + lines[:1], lines[:-1], []):
        path.write_bytes(b"".join(damaged))
        with pytest.raises(HTTPException, match="Journal"):
            vault(tmp_path).read_journal("effects")
    path.write_bytes(b"".join(lines))
    assert v.read_journal("effects") == [{"ordinal": n} for n in range(3)]


def test_rotation_conserves_chain_and_detects_missing_segment(tmp_path):
    v = vault(tmp_path)
    # Real release boundary, no tiny patched threshold that hides framing overhead.
    records = [{"ordinal": n, "digest": "x" * 240_000} for n in range(29)]
    for record in records:
        v.append_journal("effects", record)
    segments = sorted(tmp_path.glob("effects-journal-*.enc"))
    assert segments, "Journals rotate at 8 MiB"
    assert all(p.stat().st_size <= 8 * 1024 * 1024 for p in segments)
    assert (tmp_path / "effects-journal.enc").stat().st_size <= 8 * 1024 * 1024
    assert v.read_journal("effects") == records
    checkpoint = v.journal_checkpoint("effects")
    assert checkpoint["sequence"] == len(records)
    assert len(checkpoint["hash"]) == 64
    segments[0].unlink()
    with pytest.raises(HTTPException):
        vault(tmp_path).read_journal("effects")


def test_separate_writers_have_one_monotonic_sequence(tmp_path):
    writers = [vault(tmp_path) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: writers[n % 4].append_journal("effects", {"n": n}), range(80)))
    values = vault(tmp_path).read_journal("effects")
    assert sorted(r["n"] for r in values) == list(range(80))
    assert vault(tmp_path).journal_checkpoint("effects")["sequence"] == 80


def test_legacy_denial_remains_readable_then_links_new_records(tmp_path):
    v = vault(tmp_path)
    path = tmp_path / "consent-journal.enc"
    old = {"event_id": "legacy-denial"}
    path.write_bytes(v.cipher.encrypt(json.dumps(old).encode()) + b"\n")
    v.append_journal("consent", {"event_id": "new-denial"})
    assert v.read_journal("consent") == [old, {"event_id": "new-denial"}]
    assert v.journal_checkpoint("consent")["sequence"] == 2


def test_partial_append_is_never_extended(tmp_path, monkeypatch):
    import os

    v = vault(tmp_path)
    v.append_journal("effects", {"n": 1})
    original = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: original(fd, data[: len(data) // 2]))
    with pytest.raises(OSError):
        v.append_journal("effects", {"n": 2})
    monkeypatch.setattr(os, "write", original)
    before = (tmp_path / "effects-journal.enc").read_bytes()
    with pytest.raises(HTTPException):
        v.append_journal("effects", {"n": 3})
    assert (tmp_path / "effects-journal.enc").read_bytes() == before


def test_journal_names_cannot_escape_protected_directory(tmp_path):
    v = vault(tmp_path)
    with pytest.raises(ValueError):
        v.append_journal("../outside", {"n": 1})


def test_append_does_not_redecrypt_prior_history(tmp_path, monkeypatch):
    v = vault(tmp_path)
    for n in range(100):
        v.append_journal("effects", {"n": n})
    original = v.cipher.decrypt
    reads = []

    def counted(value, *args, **kwargs):
        reads.append(len(value))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(v.cipher, "decrypt", counted)
    v.append_journal("effects", {"n": 101})
    assert len(reads) <= 3, "One effect cannot scan all historical journal records"
