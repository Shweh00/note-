from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import os

from .config import AppConfig
from .state import ProcessingState, StateRecord


INDEX_START = "<!-- handwriting-ocr:index:start -->"
INDEX_END = "<!-- handwriting-ocr:index:end -->"


@dataclass(frozen=True)
class IndexEntry:
    note_path: Path
    source_path: Path | None
    archived_path: Path | None
    title: str
    status: str
    created_at: str
    note_date: str | None


def index_path(config: AppConfig) -> Path:
    raw = config.index.path or Path("Index.md")
    return raw if raw.is_absolute() else config.output_dir / raw


def update_index(config: AppConfig, state: ProcessingState) -> list[str]:
    warnings: list[str] = []
    if not config.index.enabled:
        return warnings
    try:
        target = index_path(config)
        records = _dedup_records(state.successful_records())
        entries = [_entry(record) for record in records if record.output_path]
        block = _render_block(config, target.parent, entries)
        existing = target.read_text(encoding="utf-8") if target.exists() else f"# {config.index.title}\n\n"
        updated = _replace_managed_block(existing, block)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        tmp_path.write_text(updated, encoding="utf-8")
        tmp_path.replace(target)
    except Exception as exc:  # index warnings must not roll back OCR success
        warnings.append(f"index warning: {exc}")
    return warnings


def _dedup_records(records: list[StateRecord]) -> list[StateRecord]:
    by_output: dict[str, StateRecord] = {}
    for record in records:
        if record.output_path:
            by_output[record.output_path] = record
    return list(by_output.values())


def _entry(record: StateRecord) -> IndexEntry:
    note_path = Path(record.output_path or "")
    return IndexEntry(
        note_path=note_path,
        source_path=Path(record.source_path) if record.source_path else None,
        archived_path=Path(record.archived_path) if record.archived_path else None,
        title=note_path.stem,
        status=record.status,
        created_at=record.created_at,
        note_date=record.note_date,
    )


def _render_block(config: AppConfig, index_dir: Path, entries: list[IndexEntry]) -> str:
    reverse = config.index.sort == "desc"
    sorted_entries = sorted(entries, key=lambda entry: (entry.note_date or entry.created_at[:10], entry.created_at, entry.note_path.as_posix()), reverse=reverse)
    lines = [INDEX_START]
    if config.index.grouping == "date":
        grouped: OrderedDict[str, list[IndexEntry]] = OrderedDict()
        for entry in sorted_entries:
            grouped.setdefault(entry.note_date or entry.created_at[:10], []).append(entry)
        for date, group in grouped.items():
            lines.extend(["", f"## {date}", ""])
            lines.extend(_entry_line(config, index_dir, entry) for entry in group)
    else:
        lines.extend([""])
        lines.extend(_entry_line(config, index_dir, entry) for entry in sorted_entries)
    lines.extend(["", INDEX_END, ""])
    return "\n".join(lines)


def _entry_line(config: AppConfig, index_dir: Path, entry: IndexEntry) -> str:
    note_link = _note_link(config, entry.note_path)
    pieces = [f"- {note_link}"]
    if config.index.include_status:
        pieces.append(f"`{entry.status}`")
    if config.index.include_source_link:
        source = entry.archived_path or entry.source_path
        if source:
            pieces.append(f"- 来源：![[{_relative(index_dir, source)}]]")
    return " ".join(pieces)


def _note_link(config: AppConfig, note_path: Path) -> str:
    try:
        rel = Path(os.path.relpath(note_path.with_suffix(""), start=config.output_dir)).as_posix()
    except ValueError:  # pragma: no cover - Windows drive mismatch guard
        rel = note_path.with_suffix("").as_posix()
    return f"[[{rel}|{note_path.stem}]]"


def _relative(from_dir: Path, target: Path) -> str:
    try:
        return Path(os.path.relpath(target, start=from_dir)).as_posix()
    except ValueError:  # pragma: no cover - Windows drive mismatch guard
        return target.as_posix()


def _replace_managed_block(existing: str, block: str) -> str:
    start = existing.find(INDEX_START)
    end = existing.find(INDEX_END)
    if start != -1 and end != -1 and end > start:
        end += len(INDEX_END)
        return existing[:start] + block.rstrip() + existing[end:]
    suffix = "\n" if existing.endswith("\n") else "\n\n"
    return existing + suffix + block
