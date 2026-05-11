from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import ast
import tomllib


SUPPORTED_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic")


@dataclass(frozen=True)
class WatchConfig:
    input_dir: Path
    output_dir: Path
    processed_dir: Path
    error_dir: Path
    settle_seconds: float = 5.0
    recursive: bool = False
    polling: bool = True


@dataclass(frozen=True)
class OcrConfig:
    mode: str = "mock"
    provider: str = "mock"
    model: str | None = None
    language: str = "zh-cn,en"
    retry_count: int = 3
    timeout_seconds: int = 120
    api_key_env: str = "OPENAI_API_KEY"
    fallback_text: str = "TODO: replace with OCR text"
    command: str = "tesseract"


@dataclass(frozen=True)
class MarkdownConfig:
    filename_template: str = "{{date}}-{{source_basename}}.md"
    include_frontmatter: bool = True
    include_source_image: bool = True
    include_raw_ocr: bool = True
    default_tags: tuple[str, ...] = ("handwriting", "ocr", "to-review")


@dataclass(frozen=True)
class StateConfig:
    sqlite_path: Path
    log_path: Path | None = None


@dataclass(frozen=True)
class DedupeConfig:
    strategy: str = "content_hash"
    on_duplicate: str = "skip"


@dataclass(frozen=True)
class ArchiveConfig:
    after_success: str = "move"
    after_error: str = "move"


@dataclass(frozen=True)
class AppConfig:
    watch: WatchConfig
    ocr: OcrConfig
    markdown: MarkdownConfig
    state: StateConfig
    dedupe: DedupeConfig = field(default_factory=DedupeConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    extensions: tuple[str, ...] = SUPPORTED_EXTENSIONS

    @property
    def input_dir(self) -> Path:
        return self.watch.input_dir

    @property
    def output_dir(self) -> Path:
        return self.watch.output_dir

    @property
    def processed_dir(self) -> Path:
        return self.watch.processed_dir

    @property
    def error_dir(self) -> Path:
        return self.watch.error_dir

    @property
    def state_path(self) -> Path:
        return self.state.sqlite_path


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return ast.literal_eval(value)
    if value.startswith("[") and value.endswith("]"):
        return ast.literal_eval(value)
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    section: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not raw_line.startswith(" ") and line.endswith(":"):
            section = line[:-1].strip()
            data[section] = {}
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        parsed = _parse_scalar(value)
        if raw_line.startswith(" ") and section:
            data[section][key] = parsed
        else:
            data[key] = parsed
    return data


def _load_raw(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".toml":
        with path.open("rb") as handle:
            return tomllib.load(handle)
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - exercised only when PyYAML is absent
        return _load_simple_yaml(path)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded or {}


def _resolve(base: Path, value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (base / candidate).resolve()


def _validate_choice(name: str, value: str, choices: set[str]) -> None:
    if value not in choices:
        raise ValueError(f"{name} must be one of {sorted(choices)}, got {value!r}")


def load_config(path: Path) -> AppConfig:
    config_path = path.expanduser().resolve()
    raw = _load_raw(config_path)
    base = config_path.parent

    if "watch" not in raw:
        raw = _legacy_toml_to_nested(raw)

    watch_raw = raw.get("watch", {})
    ocr_raw = raw.get("ocr", {})
    markdown_raw = raw.get("markdown", {})
    state_raw = raw.get("state", {})
    dedupe_raw = raw.get("dedupe", {})
    archive_raw = raw.get("archive", {})

    input_dir = _resolve(base, watch_raw["input_dir"])
    output_dir = _resolve(base, watch_raw["output_dir"])
    processed_dir = _resolve(base, watch_raw.get("processed_dir", input_dir / "processed"))
    error_dir = _resolve(base, watch_raw.get("error_dir", input_dir / "error"))
    sqlite_path = _resolve(base, state_raw.get("sqlite_path", base / ".handwriting-ocr" / "state.sqlite"))
    log_value = state_raw.get("log_path")

    ocr_mode = str(ocr_raw.get("mode", "mock"))
    ocr_provider = str(ocr_raw.get("provider", "mock"))
    after_success = str(archive_raw.get("after_success", "move"))
    after_error = str(archive_raw.get("after_error", "move"))
    dedupe_strategy = str(dedupe_raw.get("strategy", "content_hash"))
    dedupe_on_duplicate = str(dedupe_raw.get("on_duplicate", "skip"))

    _validate_choice("ocr.mode", ocr_mode, {"online", "offline", "mock"})
    _validate_choice("ocr.provider", ocr_provider, {"openai", "paddle", "mock", "tesseract"})
    _validate_choice("archive.after_success", after_success, {"move", "keep", "copy"})
    _validate_choice("archive.after_error", after_error, {"move", "keep", "copy"})
    _validate_choice("dedupe.strategy", dedupe_strategy, {"content_hash", "path_and_mtime"})
    _validate_choice("dedupe.on_duplicate", dedupe_on_duplicate, {"skip", "keep"})

    settle_seconds = float(watch_raw.get("settle_seconds", 5))
    if not 1 <= settle_seconds <= 300:
        raise ValueError("watch.settle_seconds must be between 1 and 300")

    return AppConfig(
        watch=WatchConfig(
            input_dir=input_dir,
            output_dir=output_dir,
            processed_dir=processed_dir,
            error_dir=error_dir,
            settle_seconds=settle_seconds,
            recursive=bool(watch_raw.get("recursive", False)),
            polling=bool(watch_raw.get("polling", True)),
        ),
        ocr=OcrConfig(
            mode=ocr_mode,
            provider=ocr_provider,
            model=ocr_raw.get("model"),
            language=str(ocr_raw.get("language", ocr_raw.get("languages", "zh-cn,en"))),
            retry_count=int(ocr_raw.get("retry_count", 3)),
            timeout_seconds=int(ocr_raw.get("timeout_seconds", 120)),
            api_key_env=str(ocr_raw.get("api_key_env", "OPENAI_API_KEY")),
            fallback_text=str(ocr_raw.get("fallback_text", "TODO: replace with OCR text")),
            command=str(ocr_raw.get("command", "tesseract")),
        ),
        markdown=MarkdownConfig(
            filename_template=str(markdown_raw.get("filename_template", "{{date}}-{{source_basename}}.md")),
            include_frontmatter=bool(markdown_raw.get("include_frontmatter", True)),
            include_source_image=bool(markdown_raw.get("include_source_image", True)),
            include_raw_ocr=bool(markdown_raw.get("include_raw_ocr", True)),
            default_tags=tuple(markdown_raw.get("default_tags", ("handwriting", "ocr", "to-review"))),
        ),
        state=StateConfig(
            sqlite_path=sqlite_path,
            log_path=_resolve(base, log_value) if log_value else None,
        ),
        dedupe=DedupeConfig(strategy=dedupe_strategy, on_duplicate=dedupe_on_duplicate),
        archive=ArchiveConfig(after_success=after_success, after_error=after_error),
    )


def _legacy_toml_to_nested(raw: dict[str, Any]) -> dict[str, Any]:
    input_dir = raw["input_dir"]
    output_dir = raw["output_dir"]
    return {
        "watch": {
            "input_dir": input_dir,
            "output_dir": output_dir,
            "processed_dir": raw.get("processed_dir", str(Path(input_dir) / "processed")),
            "error_dir": raw.get("error_dir", str(Path(input_dir) / "error")),
            "settle_seconds": raw.get("watch_interval_seconds", 5),
            "polling": True,
        },
        "ocr": raw.get("ocr", {"mode": "mock", "provider": "mock"}),
        "markdown": {"default_tags": raw.get("tags", ("handwriting", "ocr", "to-review"))},
        "state": {"sqlite_path": raw.get("state_path", ".handwriting-ocr-state.sqlite")},
        "archive": {"after_success": "copy", "after_error": "copy"},
        "dedupe": raw.get("dedupe", {"strategy": "content_hash", "on_duplicate": "skip"}),
    }
