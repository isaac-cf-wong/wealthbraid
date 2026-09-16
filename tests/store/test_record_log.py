"""Tests for record envelopes, the append-only log, and the evidence store."""

from __future__ import annotations

import json

import pytest

from wealthbraid.errors import ConflictError, IntegrityError, NotFoundError
from wealthbraid.store.records import Record, RecordKind, canonical_json
from wealthbraid.store.store import RecordStore


def _append(store: RecordStore, *payloads: dict) -> list[Record]:
    with store.lock():
        pending = store.begin()
        for payload in payloads:
            pending.add(RecordKind.NOTE, payload, actor="human:alice", operation="opr_x")
        return pending.commit()


def test_canonical_json_is_order_independent():
    """Equal mappings serialise identically regardless of key order."""
    assert canonical_json({"b": 1, "a": [2, {"d": 3, "c": 4}]}) == canonical_json({"a": [2, {"c": 4, "d": 3}], "b": 1})


def test_id_is_content_derived_and_prefixed():
    """The id carries the kind prefix and changes with any envelope field."""
    kwargs = {"seq": 1, "prev": None, "recorded_at": "2026-01-01T00:00:00Z", "actor": "human:a", "operation": None}
    first = Record.create(kind=RecordKind.ENTRY, data={"x": "1"}, **kwargs)
    assert first.id.startswith("ent_")
    assert first.expected_id() == first.id
    assert Record.create(kind=RecordKind.ENTRY, data={"x": "2"}, **kwargs).id != first.id
    assert Record.create(kind=RecordKind.ENTRY, data={"x": "1"}, **{**kwargs, "actor": "agent:b"}).id != first.id


def test_round_trip_through_json_preserves_id():
    """A record parsed from its JSON line re-derives the same id."""
    record = Record.create(
        seq=1,
        prev=None,
        kind=RecordKind.NOTE,
        recorded_at="2026-01-01T00:00:00Z",
        actor="human:a",
        operation="opr_1",
        data={"subjects": ["x"], "text": "héllo"},
    )
    parsed = Record.from_json(json.loads(record.to_line()))
    assert parsed == record
    assert parsed.expected_id() == record.id


def test_append_chains_records_and_assigns_sequence(tmp_path):
    """Appended records link to their predecessor across separate appends."""
    store = RecordStore(tmp_path)
    first = _append(store, {"n": 1}, {"n": 2})
    second = _append(store, {"n": 3})
    records = store.read_records()
    assert [r.seq for r in records] == [1, 2, 3]
    assert records[0].prev is None
    assert records[1].prev == first[0].id
    assert records[2].prev == first[1].id == records[1].id
    assert records[2] == second[0]
    assert store.head() == second[0]


def test_segment_is_named_by_month_and_never_moves_backwards(tmp_path):
    """Records go to records/YYYY/MM.jsonl, but a backwards clock keeps using the latest segment."""
    store = RecordStore(tmp_path)
    with store.lock():
        pending = store.begin(recorded_at="2026-09-02T00:00:00Z")
        pending.add(RecordKind.NOTE, {}, actor="human:a")
        pending.commit()
    with store.lock():
        pending = store.begin(recorded_at="2026-08-30T00:00:00Z")
        pending.add(RecordKind.NOTE, {}, actor="human:a")
        pending.commit()
    assert [p.relative_to(tmp_path).as_posix() for p in store.segments()] == ["records/2026/09.jsonl"]
    assert len(store.read_records()) == 2


def test_lock_is_exclusive_and_released(tmp_path):
    """A second writer is refused while the lock is held, and allowed afterwards."""
    store = RecordStore(tmp_path)
    with store.lock(), pytest.raises(ConflictError), store.lock():
        pass
    with store.lock():
        pass


def test_truncated_line_is_an_integrity_error(tmp_path):
    """A partially written final record is reported, not silently skipped."""
    store = RecordStore(tmp_path)
    _append(store, {"n": 1})
    segment = store.segments()[0]
    segment.write_text(segment.read_text() + '{"id": "not_', encoding="utf-8")
    with pytest.raises(IntegrityError, match="truncated"):
        store.read_records()


def test_malformed_line_is_an_integrity_error(tmp_path):
    """Garbage in a segment names the file and line."""
    store = RecordStore(tmp_path)
    _append(store, {"n": 1})
    segment = store.segments()[0]
    segment.write_text(segment.read_text() + "not json\n", encoding="utf-8")
    with pytest.raises(IntegrityError, match=r"09\.jsonl:2"):
        store.read_records()


def test_evidence_is_content_addressed_and_idempotent(tmp_path):
    """Identical bytes map to one read-only blob named by digest."""
    store = RecordStore(tmp_path)
    digest = store.put_evidence(b"date,amount\n")
    assert store.put_evidence(b"date,amount\n") == digest
    path = store.evidence_path(digest)
    assert path.parent.name == digest[:2]
    assert store.read_evidence(digest) == b"date,amount\n"
    with pytest.raises(NotFoundError):
        store.read_evidence("0" * 64)


def test_corrupt_evidence_blob_is_detected_on_rewrite(tmp_path):
    """Re-storing over a tampered blob refuses rather than trusting it."""
    store = RecordStore(tmp_path)
    digest = store.put_evidence(b"original")
    path = store.evidence_path(digest)
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        store.put_evidence(b"original")


def test_stale_lock_from_a_dead_process_is_recovered(tmp_path):
    """A lock left by a crashed writer does not block the book forever."""
    import subprocess
    import sys

    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True
    )
    store = RecordStore(tmp_path)
    lock = tmp_path / ".wealthbraid" / "lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(child.stdout.strip())
    with store.lock():
        assert lock.read_text() == str(__import__("os").getpid())
    assert not lock.exists()


def test_lock_held_by_a_live_process_is_respected_and_not_deleted(tmp_path):
    import os

    store = RecordStore(tmp_path)
    lock = tmp_path / ".wealthbraid" / "lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getppid()))
    with pytest.raises(ConflictError, match=str(os.getppid())), store.lock():
        pass
    assert lock.read_text() == str(os.getppid())


def test_release_does_not_delete_another_writers_lock(tmp_path):
    import os

    store = RecordStore(tmp_path)
    lock = tmp_path / ".wealthbraid" / "lock"
    with store.lock():
        lock.write_text(str(os.getppid()))
    assert lock.read_text() == str(os.getppid())
