from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import sqlite3


STATUSES = {"pending", "processing", "success", "failed", "duplicate"}


@dataclass(frozen=True)
class StateRecord:
    id: int
    source_path: str
    source_basename: str
    source_hash: str
    file_size: int
    mtime: float
    status: str
    output_path: str | None
    archived_path: str | None
    error_message: str | None
    ocr_provider: str | None
    ocr_model: str | None
    language: str | None
    created_at: str
    updated_at: str


class ProcessingState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ProcessingState":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @classmethod
    def open(cls, path: Path) -> "ProcessingState":
        return cls(path)

    def _migrate(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_files (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_path TEXT NOT NULL,
              source_basename TEXT NOT NULL,
              source_hash TEXT NOT NULL,
              file_size INTEGER NOT NULL,
              mtime REAL NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('pending','processing','success','failed','duplicate')),
              output_path TEXT,
              archived_path TEXT,
              error_message TEXT,
              ocr_provider TEXT,
              ocr_model TEXT,
              language TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_files_hash_success
            ON processed_files(source_hash)
            WHERE status = 'success'
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_processed_files_status ON processed_files(status)"
        )
        self.connection.commit()

    def successful_by_hash(self, source_hash: str) -> StateRecord | None:
        row = self.connection.execute(
            "SELECT * FROM processed_files WHERE source_hash = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
            (source_hash,),
        ).fetchone()
        return _record(row) if row else None

    def duplicate_by_path_hash(self, *, source_path: Path, source_hash: str) -> StateRecord | None:
        row = self.connection.execute(
            """
            SELECT * FROM processed_files
            WHERE source_path = ? AND source_hash = ? AND status = 'duplicate'
            ORDER BY id DESC LIMIT 1
            """,
            (str(source_path), source_hash),
        ).fetchone()
        return _record(row) if row else None

    def insert_pending(self, *, source_path: Path, source_hash: str, file_size: int, mtime: float) -> int:
        now = _now()
        cursor = self.connection.execute(
            """
            INSERT INTO processed_files (
              source_path, source_basename, source_hash, file_size, mtime, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (str(source_path), source_path.name, source_hash, file_size, mtime, now, now),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def update(self, record_id: int, status: str, **values: Any) -> None:
        if status not in STATUSES:  # pragma: no cover - internal callers use fixed statuses
            raise ValueError(f"invalid status {status!r}")
        values["status"] = status
        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.connection.execute(
            f"UPDATE processed_files SET {assignments} WHERE id = ?",
            (*values.values(), record_id),
        )
        self.connection.commit()

    def duplicate(
        self,
        *,
        source_path: Path,
        source_hash: str,
        file_size: int,
        mtime: float,
        existing: StateRecord,
        archived_path: Path | None = None,
    ) -> int:
        record_id = self.insert_pending(
            source_path=source_path, source_hash=source_hash, file_size=file_size, mtime=mtime
        )
        self.update(
            record_id,
            "duplicate",
            output_path=existing.output_path,
            archived_path=str(archived_path) if archived_path else existing.archived_path,
        )
        return record_id

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM processed_files GROUP BY status"
        ).fetchall()
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        counts["total"] = sum(value for key, value in counts.items() if key != "total")
        return counts

    def recent_failures(self, limit: int = 10) -> list[StateRecord]:
        rows = self.connection.execute(
            "SELECT * FROM processed_files WHERE status = 'failed' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_record(row) for row in rows]

    def failed_records(self) -> list[StateRecord]:
        rows = self.connection.execute(
            "SELECT * FROM processed_files WHERE status = 'failed' ORDER BY updated_at ASC"
        ).fetchall()
        return [_record(row) for row in rows]

    def successful_records(self) -> list[StateRecord]:
        rows = self.connection.execute(
            """
            SELECT * FROM processed_files
            WHERE status = 'success' AND output_path IS NOT NULL
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
        return [_record(row) for row in rows]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _record(row: sqlite3.Row) -> StateRecord:
    return StateRecord(
        id=int(row["id"]),
        source_path=str(row["source_path"]),
        source_basename=str(row["source_basename"]),
        source_hash=str(row["source_hash"]),
        file_size=int(row["file_size"]),
        mtime=float(row["mtime"]),
        status=str(row["status"]),
        output_path=row["output_path"],
        archived_path=row["archived_path"],
        error_message=row["error_message"],
        ocr_provider=row["ocr_provider"],
        ocr_model=row["ocr_model"],
        language=row["language"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
