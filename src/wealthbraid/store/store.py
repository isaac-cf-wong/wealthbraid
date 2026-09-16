"""The on-disk, append-only record log and content-addressed evidence store.

Layout of a book directory::

    wealthbraid.toml                 settings (see wealthbraid.book.config)
    records/2026/09.jsonl            append-only log segments, one record per line
    evidence/sha256/ab/abcdef…       immutable evidence blobs named by digest
    .wealthbraid/                    local, regenerable state (lock file); git-ignored

Records are only ever appended. A write takes an exclusive lock, computes the
new records' identifiers against the current head of the chain, and appends all
of them in a single write followed by ``fsync``. Nothing in this module can
modify or delete an existing record or evidence blob.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from wealthbraid.errors import ConflictError, IntegrityError, NotFoundError
from wealthbraid.store.records import Record, RecordKind

RECORDS_DIR = "records"
EVIDENCE_DIR = "evidence"
STATE_DIR = ".wealthbraid"
_LOCK_NAME = "lock"


def utc_now() -> dt.datetime:
    """Return the current UTC time truncated to whole seconds.

    Returns:
        A timezone-aware UTC datetime.

    """
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def format_timestamp(moment: dt.datetime) -> str:
    """Format a datetime as the canonical UTC timestamp string.

    Args:
        moment: A timezone-aware datetime.

    Returns:
        A string such as ``"2026-09-16T12:00:00Z"``.

    """
    return moment.astimezone(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class RecordStore:
    """Append-only access to a book's record log and evidence blobs."""

    def __init__(self, root: Path, *, clock: Callable[[], dt.datetime] = utc_now) -> None:
        """Open the store rooted at a book directory.

        Args:
            root: The book directory.
            clock: Source of write timestamps; injectable for deterministic tests.

        """
        self.root = Path(root)
        self._clock = clock

    # -- paths ------------------------------------------------------------

    @property
    def records_dir(self) -> Path:
        """Return the directory holding log segments.

        Returns:
            The ``records`` directory path.

        """
        return self.root / RECORDS_DIR

    @property
    def evidence_dir(self) -> Path:
        """Return the directory holding evidence blobs.

        Returns:
            The ``evidence`` directory path.

        """
        return self.root / EVIDENCE_DIR

    def segments(self) -> list[Path]:
        """Return the log segment files in chain order.

        Returns:
            Segment paths sorted by ``YYYY/MM`` name.

        """
        if not self.records_dir.is_dir():
            return []
        return sorted(self.records_dir.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9].jsonl"))

    # -- reading ----------------------------------------------------------

    def iter_records(self) -> Iterator[Record]:
        """Yield every record in log order.

        Yields:
            Parsed records, without identifier or chain verification.

        Raises:
            IntegrityError: If a line is not valid JSON or not a valid record.

        """
        for segment in self.segments():
            with segment.open(encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    location = f"{segment.relative_to(self.root)}:{number}"
                    if not line.endswith("\n"):
                        raise IntegrityError(f"{location}: truncated record (no trailing newline)")
                    try:
                        yield Record.from_json(json.loads(line))
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise IntegrityError(f"{location}: {exc}") from exc

    def read_records(self) -> list[Record]:
        """Return every record in log order.

        Returns:
            The full list of records.

        """
        return list(self.iter_records())

    def head(self) -> Record | None:
        """Return the last record in the log, if any.

        Returns:
            The most recent record, or ``None`` for an empty log.

        """
        for segment in reversed(self.segments()):
            lines = [line for line in segment.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                try:
                    return Record.from_json(json.loads(lines[-1]))
                except (json.JSONDecodeError, ValueError) as exc:
                    raise IntegrityError(f"{segment.relative_to(self.root)}: unreadable last record: {exc}") from exc
        return None

    # -- writing ----------------------------------------------------------

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the book's exclusive write lock.

        Yields:
            Nothing; the lock is held for the duration of the block.

        Raises:
            ConflictError: If another process holds the lock.

        """
        state = self.root / STATE_DIR
        state.mkdir(parents=True, exist_ok=True)
        lock_path = state / _LOCK_NAME
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ConflictError(
                f"The book is locked by another writer ({lock_path}); remove the file if no writer is running"
            ) from exc
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            lock_path.unlink(missing_ok=True)

    def begin(self, *, recorded_at: str | None = None) -> PendingAppend:
        """Start collecting records to append against the current head.

        The caller must hold :meth:`lock` until :meth:`PendingAppend.commit`.

        Args:
            recorded_at: The timestamp for the staged records; defaults to now.

        Returns:
            A :class:`PendingAppend` bound to the current head.

        """
        return PendingAppend(self, self.head(), recorded_at or format_timestamp(self._clock()))

    def _segment_for(self, recorded_at: str) -> Path:
        """Choose the segment a new record is appended to.

        The segment follows the write month but never precedes the latest
        existing segment, so file order always equals chain order even if the
        clock moves backwards.

        Args:
            recorded_at: The record's timestamp.

        Returns:
            The segment path.

        """
        candidate = self.records_dir / recorded_at[:4] / f"{recorded_at[5:7]}.jsonl"
        existing = self.segments()
        if existing and existing[-1] > candidate:
            return existing[-1]
        return candidate

    # -- evidence ---------------------------------------------------------

    def evidence_path(self, sha256: str) -> Path:
        """Return where the blob with a given digest is stored.

        Args:
            sha256: The lowercase hex SHA-256 digest.

        Returns:
            The blob path (which may not exist).

        """
        return self.evidence_dir / "sha256" / sha256[:2] / sha256

    def put_evidence(self, content: bytes) -> str:
        """Store an evidence blob by digest; storing the same bytes twice is a no-op.

        Args:
            content: The raw file content.

        Returns:
            The SHA-256 digest of ``content``.

        Raises:
            IntegrityError: If a different blob already occupies the digest's path.

        """
        digest = hashlib.sha256(content).hexdigest()
        path = self.evidence_path(digest)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise IntegrityError(f"Evidence blob {digest} is corrupt on disk")
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o444)
        return digest

    def read_evidence(self, sha256: str) -> bytes:
        """Read an evidence blob.

        Args:
            sha256: The blob digest.

        Returns:
            The blob content.

        Raises:
            NotFoundError: If no blob with that digest is stored.

        """
        path = self.evidence_path(sha256)
        if not path.is_file():
            raise NotFoundError(f"Evidence blob not found: {sha256}")
        return path.read_bytes()


class PendingAppend:
    """Records staged for a single atomic append to the log."""

    def __init__(self, store: RecordStore, head: Record | None, recorded_at: str) -> None:
        """Bind a pending append to the head it extends.

        Args:
            store: The owning store.
            head: The current last record, or ``None``.
            recorded_at: The timestamp shared by every record in this append.

        """
        self._store = store
        self._recorded_at = recorded_at
        self._seq = head.seq if head else 0
        self._prev = head.id if head else None
        self.records: list[Record] = []

    def add(self, kind: RecordKind, data: Mapping[str, Any], *, actor: str, operation: str | None = None) -> Record:
        """Stage a record; its identifier is available immediately.

        Args:
            kind: The record kind.
            data: The kind-specific payload.
            actor: The actor string.
            operation: The originating operation, if any.

        Returns:
            The staged :class:`Record`.

        """
        self._seq += 1
        record = Record.create(
            seq=self._seq,
            prev=self._prev,
            kind=kind,
            recorded_at=self._recorded_at,
            actor=actor,
            operation=operation,
            data=data,
        )
        self._prev = record.id
        self.records.append(record)
        return record

    def commit(self) -> list[Record]:
        """Append every staged record in one write and ``fsync``.

        Returns:
            The records written.

        """
        if not self.records:
            return []
        segment = self._store._segment_for(self._recorded_at)
        segment.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(record.to_line() + "\n" for record in self.records)
        with segment.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return list(self.records)
