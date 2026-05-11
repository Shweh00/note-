from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import os
import shutil
import time

from .config import AppConfig
from .index import update_index
from .markdown import build_note_path, render_note, resolve_note_output_dir, slugify
from .ocr import OcrEngine
from .state import ProcessingState, StateRecord


@dataclass(frozen=True)
class ProcessResult:
    image_path: Path
    note_path: Path | None
    status: str
    reason: str

    @property
    def skipped(self) -> bool:
        return self.status in {"duplicate", "unsupported", "missing"}


def discover_images(input_dir: Path, extensions: tuple[str, ...], *, recursive: bool = False) -> list[Path]:
    if not input_dir.exists():
        return []
    allowed = {ext.lower() for ext in extensions}
    pattern = "**/*" if recursive else "*"
    return sorted(path for path in input_dir.glob(pattern) if path.is_file() and path.suffix.lower() in allowed)


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_until_stable(path: Path, seconds: float, *, timeout_seconds: float | None = None, stable_checks: int = 2) -> None:
    if seconds <= 0:
        return
    interval = min(seconds, 2)
    timeout = timeout_seconds if timeout_seconds is not None else max(seconds * 5, seconds + 5)
    deadline = time.monotonic() + timeout
    previous = path.stat()
    stable_count = 0
    while time.monotonic() < deadline:
        time.sleep(interval)
        current = path.stat()
        if current.st_size == previous.st_size and current.st_mtime_ns == previous.st_mtime_ns:
            stable_count += 1
            if stable_count >= stable_checks:
                return
        else:
            stable_count = 0
            previous = current
    raise TimeoutError(f"{path} did not become stable within {timeout:g} seconds")


def relative_markdown_path(from_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target, start=from_dir)).as_posix()


def archive_image(image_path: Path, config: AppConfig, action: str, target_dir: Path) -> Path:
    target = planned_archive_path(image_path, action, target_dir)
    if action == "copy":
        shutil.copy2(image_path, target)
    elif action == "move":
        shutil.move(str(image_path), str(target))
    return target


def planned_archive_path(image_path: Path, action: str, target_dir: Path) -> Path:
    if action == "keep":
        return image_path
    target_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_path(target_dir / image_path.name)
    if action not in {"copy", "move"}:
        raise ValueError(f"Unsupported archive action: {action}")
    return target


def process_image(
    image_path: Path,
    *,
    config: AppConfig,
    state: ProcessingState,
    ocr_engine: OcrEngine,
) -> ProcessResult:
    if image_path.suffix.lower() not in config.extensions:
        return ProcessResult(image_path=image_path, note_path=None, status="unsupported", reason="unsupported extension")

    record_id: int | None = None
    try:
        wait_until_stable(image_path, config.watch.settle_seconds)
        stat = image_path.stat()
        image_hash = fingerprint(image_path)
        existing = state.successful_by_hash(image_hash)
        if existing:
            if (
                config.dedupe.on_duplicate == "keep"
                and state.duplicate_by_path_hash(source_path=image_path, source_hash=image_hash)
            ):
                return ProcessResult(
                    image_path=image_path,
                    note_path=Path(existing.output_path) if existing.output_path else None,
                    status="duplicate",
                    reason="duplicate already recorded",
                )
            archived_path = _archive_duplicate(image_path, config)
            state.duplicate(
                source_path=image_path,
                source_hash=image_hash,
                file_size=stat.st_size,
                mtime=stat.st_mtime,
                existing=existing,
                archived_path=archived_path,
            )
            return ProcessResult(image_path=image_path, note_path=Path(existing.output_path) if existing.output_path else None, status="duplicate", reason="duplicate")

        record_id = state.insert_pending(
            source_path=image_path,
            source_hash=image_hash,
            file_size=stat.st_size,
            mtime=stat.st_mtime,
        )
        state.update(record_id, "processing")

        ocr_result = ocr_engine.recognize(image_path, language=config.ocr.language)
        created_at = datetime.now(timezone.utc)
        note_output_dir = resolve_note_output_dir(config, image_path, created_at)
        note_output_dir.mkdir(parents=True, exist_ok=True)
        note_path = build_note_path(
            note_output_dir,
            image_path.stem,
            created_at,
            image_hash,
            config.markdown.filename_template,
        )

        archived_path = planned_archive_path(image_path, config.archive.after_success, config.processed_dir)
        relative_image = relative_markdown_path(note_output_dir, archived_path)
        markdown = render_note(
            config=config,
            title=image_path.stem,
            created_at=created_at,
            source_basename=image_path.name,
            source_image_relative=relative_image,
            image_hash=image_hash,
            ocr_result=ocr_result,
        )
        tmp_path = note_path.with_suffix(note_path.suffix + ".tmp")
        tmp_path.write_text(markdown, encoding="utf-8")
        tmp_path.replace(note_path)
        archived_path = archive_image(image_path, config, config.archive.after_success, config.processed_dir)

        state.update(
            record_id,
            "success",
            output_path=str(note_path),
            archived_path=str(archived_path),
            ocr_provider=ocr_result.provider,
            ocr_model=ocr_result.model,
            language=ocr_result.language,
        )
        warnings = update_index(config, state)
        for warning in warnings:
            print(warning, flush=True)
        reason = "processed" if not warnings else "processed; " + "; ".join(warnings)
        return ProcessResult(image_path=image_path, note_path=note_path, status="success", reason=reason)
    except Exception as exc:
        message = str(exc)
        try:
            stat = image_path.stat()
            image_hash = fingerprint(image_path)
            if record_id is None:  # pragma: no cover - most failures happen after pending insert
                record_id = state.insert_pending(
                    source_path=image_path,
                    source_hash=image_hash,
                    file_size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            archived_path = archive_image(image_path, config, config.archive.after_error, config.error_dir)
            state.update(record_id, "failed", archived_path=str(archived_path), error_message=message)
        except Exception:  # pragma: no cover - last-resort failure recording guard
            pass
        return ProcessResult(image_path=image_path, note_path=None, status="failed", reason=message)


def process_batch(config: AppConfig, ocr_engine: OcrEngine) -> list[ProcessResult]:
    with ProcessingState.open(config.state_path) as state:
        return [
            process_image(image_path, config=config, state=state, ocr_engine=ocr_engine)
            for image_path in discover_images(config.input_dir, config.extensions, recursive=config.watch.recursive)
        ]


def retry_failed(config: AppConfig, ocr_engine: OcrEngine) -> list[ProcessResult]:
    with ProcessingState.open(config.state_path) as state:
        failed = state.failed_records()
        results: list[ProcessResult] = []
        for record in failed:
            path = _retry_source_path(record)
            if path.exists():
                results.append(process_image(path, config=config, state=state, ocr_engine=ocr_engine))
            else:
                results.append(ProcessResult(image_path=path, note_path=None, status="missing", reason="failed source is missing"))
        return results


def watch(config: AppConfig, ocr_engine: OcrEngine) -> None:  # pragma: no cover - intentionally long-running loop
    while True:
        results = process_batch(config, ocr_engine)
        for result in results:
            if result.status == "success":
                print(f"processed {result.image_path} -> {result.note_path}", flush=True)
        time.sleep(max(config.watch.settle_seconds, 1))


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = slugify(path.stem)
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find unique path for {path}")  # pragma: no cover


def _archive_duplicate(image_path: Path, config: AppConfig) -> Path:
    if config.dedupe.on_duplicate == "keep":
        return image_path
    return archive_image(image_path, config, config.archive.after_success, config.processed_dir)


def _retry_source_path(record: StateRecord) -> Path:
    archived = Path(record.archived_path) if record.archived_path else None
    if archived and archived.exists():
        return archived
    return Path(record.source_path)  # pragma: no cover - archived paths are used for MVP retry flow
