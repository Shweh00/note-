from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import os
import shutil
import time

from .config import AppConfig
from .markdown import render_note, slugify
from .ocr import OcrEngine
from .state import ProcessingState


@dataclass(frozen=True)
class ProcessResult:
    image_path: Path
    note_path: Path | None
    skipped: bool
    reason: str


def discover_images(input_dir: Path, extensions: tuple[str, ...]) -> list[Path]:
    if not input_dir.exists():
        return []
    allowed = {ext.lower() for ext in extensions}
    return sorted(
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in allowed
    )


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_markdown_path(from_dir: Path, target: Path) -> str:
    return Path(os.path.relpath(target, start=from_dir)).as_posix()


def process_image(
    image_path: Path,
    *,
    config: AppConfig,
    state: ProcessingState,
    ocr_engine: OcrEngine,
) -> ProcessResult:
    image_hash = fingerprint(image_path)
    if state.has_hash(image_hash):
        return ProcessResult(image_path=image_path, note_path=None, skipped=True, reason="duplicate")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.assets_dir.mkdir(parents=True, exist_ok=True)

    created_at = datetime.now(timezone.utc)
    slug = slugify(image_path.stem)
    short_hash = image_hash[:8]
    asset_name = f"{slug}-{short_hash}{image_path.suffix.lower()}"
    note_name = f"{created_at.strftime('%Y%m%d-%H%M%S')}-{slug}-{short_hash}.md"
    asset_path = config.assets_dir / asset_name
    note_path = config.output_dir / note_name

    shutil.copy2(image_path, asset_path)
    recognized_text = ocr_engine.recognize(image_path)
    asset_relative = relative_markdown_path(config.output_dir, asset_path)

    markdown = render_note(
        title=image_path.stem,
        created_at=created_at,
        source_image=image_path.resolve(),
        image_hash=image_hash,
        tags=config.tags,
        asset_relative_path=asset_relative,
        recognized_text=recognized_text,
    )
    note_path.write_text(markdown, encoding="utf-8")

    state.mark_processed(
        image_hash,
        {
            "source_image": str(image_path.resolve()),
            "note_path": str(note_path.resolve()),
            "asset_path": str(asset_path.resolve()),
            "processed_at": created_at.isoformat(),
        },
    )
    state.save()
    return ProcessResult(image_path=image_path, note_path=note_path, skipped=False, reason="processed")


def process_batch(config: AppConfig, ocr_engine: OcrEngine) -> list[ProcessResult]:
    state = ProcessingState.load(config.state_path)
    results = []
    for image_path in discover_images(config.input_dir, config.extensions):
        results.append(process_image(image_path, config=config, state=state, ocr_engine=ocr_engine))
    return results


def watch(config: AppConfig, ocr_engine: OcrEngine) -> None:
    while True:
        results = process_batch(config, ocr_engine)
        processed = [result for result in results if not result.skipped]
        for result in processed:
            print(f"processed {result.image_path} -> {result.note_path}", flush=True)
        time.sleep(config.watch_interval_seconds)
