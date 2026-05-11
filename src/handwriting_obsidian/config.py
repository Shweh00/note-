from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


DEFAULT_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp")


@dataclass(frozen=True)
class OcrConfig:
    provider: str = "mock"
    fallback_text: str = "TODO: replace with OCR text"
    command: str = "tesseract"
    languages: str = "eng"


@dataclass(frozen=True)
class AppConfig:
    input_dir: Path
    output_dir: Path
    assets_dir: Path
    state_path: Path
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    tags: tuple[str, ...] = ("handwriting", "ocr")
    watch_interval_seconds: float = 5.0
    ocr: OcrConfig = field(default_factory=OcrConfig)


def load_config(path: Path) -> AppConfig:
    config_path = path.expanduser().resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    base = config_path.parent

    def resolve_path(value: str) -> Path:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            return candidate
        return (base / candidate).resolve()

    ocr_raw = raw.get("ocr", {})
    return AppConfig(
        input_dir=resolve_path(raw["input_dir"]),
        output_dir=resolve_path(raw["output_dir"]),
        assets_dir=resolve_path(raw["assets_dir"]),
        state_path=resolve_path(raw["state_path"]),
        extensions=tuple(ext.lower() for ext in raw.get("extensions", DEFAULT_EXTENSIONS)),
        tags=tuple(raw.get("tags", ("handwriting", "ocr"))),
        watch_interval_seconds=float(raw.get("watch_interval_seconds", 5)),
        ocr=OcrConfig(
            provider=ocr_raw.get("provider", "mock"),
            fallback_text=ocr_raw.get("fallback_text", "TODO: replace with OCR text"),
            command=ocr_raw.get("command", "tesseract"),
            languages=ocr_raw.get("languages", "eng"),
        ),
    )
