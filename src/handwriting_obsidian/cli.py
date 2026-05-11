from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import os
import shutil
import sqlite3
import sys

from .config import AppConfig, load_config
from .ocr import create_ocr_engine
from .processor import process_batch, retry_failed, watch
from .state import ProcessingState


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="handwriting-ocr")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create vault folders and a starter YAML config.")
    init_parser.add_argument("--vault", default=".", help="Obsidian vault path, or current directory by default.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config file.")

    for name, help_text in (
        ("batch", "Process all new images once."),
        ("watch", "Continuously watch for new images."),
        ("status", "Print processing totals and recent failures."),
        ("retry-failed", "Retry records currently marked failed."),
        ("doctor", "Check local configuration and runtime prerequisites."),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--config", default="config.yaml", help="Path to YAML/TOML config.")

    init_config_parser = subparsers.add_parser("init-config", help="Compatibility alias for creating config.yaml.")
    init_config_parser.add_argument("--output", default="config.yaml")
    init_config_parser.add_argument("--force", action="store_true")
    return parser


def run_batch(config_path: str) -> int:
    config = load_config(Path(config_path))
    results = process_batch(config, create_ocr_engine(config.ocr))
    for result in results:
        print(f"{result.status}: {result.image_path} {result.reason}")
    failed = [result for result in results if result.status == "failed"]
    print(_summary(results))
    return 2 if failed else 0


def run_retry_failed(config_path: str) -> int:
    config = load_config(Path(config_path))
    results = retry_failed(config, create_ocr_engine(config.ocr))
    for result in results:
        print(f"{result.status}: {result.image_path} {result.reason}")
    print(_summary(results))
    return 2 if any(result.status == "failed" for result in results) else 0


def run_watch(config_path: str) -> int:
    config = load_config(Path(config_path))
    watch(config, create_ocr_engine(config.ocr))
    return 0


def run_status(config_path: str) -> int:
    config = load_config(Path(config_path))
    with ProcessingState.open(config.state_path) as state:
        counts = state.counts()
        print(f"total: {counts['total']}")
        for status in ("success", "failed", "duplicate", "pending", "processing"):
            print(f"{status}: {counts[status]}")
        failures = state.recent_failures()
        if failures:
            print("recent failures:")
            for failure in failures:
                print(f"- {failure.source_path}: {failure.error_message}")
    return 0


def run_doctor(config_path: str) -> int:
    try:
        config = load_config(Path(config_path))
    except Exception as exc:
        print(f"config: FAIL ({exc})")
        return 1
    checks = [
        ("input_dir", _check_dir(config.input_dir, writable=False)),
        ("output_dir", _check_dir(config.output_dir, writable=True)),
        ("processed_dir", _check_dir(config.processed_dir, writable=True)),
        ("error_dir", _check_dir(config.error_dir, writable=True)),
        ("sqlite", _check_sqlite(config.state_path)),
        ("ocr", _check_ocr(config)),
    ]
    failed = False
    for name, result in checks:
        label = "OK" if result is None else f"FAIL ({result})"
        print(f"{name}: {label}")
        failed = failed or result is not None
    return 1 if failed else 0


def init_vault(vault: str, force: bool) -> int:
    root = Path(vault).expanduser().resolve()
    config_dir = root / ".handwriting-ocr"
    input_dir = root / "Inbox" / "HandwritingImages"
    output_dir = root / "Inbox" / "HandwritingNotes"
    for directory in (input_dir, input_dir / "processed", input_dir / "error", output_dir, config_dir):
        directory.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    if config_path.exists() and not force:
        print(f"{config_path} already exists; use --force to overwrite", file=sys.stderr)
        return 2
    config_path.write_text(_config_text(root), encoding="utf-8")
    print(f"wrote {config_path}")
    return 0


def init_config(output: str, force: bool) -> int:
    target = Path(output)
    if target.exists() and not force:
        print(f"{target} already exists; use --force to overwrite", file=sys.stderr)
        return 2
    target.write_text(_config_text(Path(".").resolve()), encoding="utf-8")
    print(f"wrote {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "batch":
            return run_batch(args.config)
        if args.command == "watch":
            return run_watch(args.config)
        if args.command == "status":
            return run_status(args.config)
        if args.command == "retry-failed":
            return run_retry_failed(args.config)
        if args.command == "doctor":
            return run_doctor(args.config)
        if args.command == "init":
            return init_vault(args.vault, args.force)
        if args.command == "init-config":
            return init_config(args.output, args.force)
    except (KeyError, ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command {args.command}")


def _summary(results: list[object]) -> str:
    success = sum(1 for result in results if getattr(result, "status") == "success")
    duplicate = sum(1 for result in results if getattr(result, "status") == "duplicate")
    failed = sum(1 for result in results if getattr(result, "status") == "failed")
    return f"done: {success} processed, {duplicate} duplicate, {failed} failed"


def _check_dir(path: Path, *, writable: bool) -> str | None:
    if not path.exists():
        if writable:
            path.mkdir(parents=True, exist_ok=True)
        else:
            return "missing"
    if writable and not os.access(path, os.W_OK):
        return "not writable"
    return None


def _check_sqlite(path: Path) -> str | None:
    try:
        with ProcessingState.open(path):
            return None
    except sqlite3.Error as exc:
        return str(exc)


def _check_ocr(config: AppConfig) -> str | None:
    if config.ocr.mode == "mock" or config.ocr.provider == "mock":
        return None
    if config.ocr.provider == "openai" and not os.environ.get(config.ocr.api_key_env):
        return f"{config.ocr.api_key_env} is not set"
    if config.ocr.provider == "tesseract" and not shutil.which(config.ocr.command):
        return f"{config.ocr.command} not found"
    return None


def _config_text(root: Path) -> str:
    return f"""watch:
  input_dir: "{root / 'Inbox' / 'HandwritingImages'}"
  output_dir: "{root / 'Inbox' / 'HandwritingNotes'}"
  processed_dir: "{root / 'Inbox' / 'HandwritingImages' / 'processed'}"
  error_dir: "{root / 'Inbox' / 'HandwritingImages' / 'error'}"
  settle_seconds: 1
  recursive: false
  polling: true

ocr:
  mode: "mock"
  provider: "mock"
  model: "mock"
  language: "zh-cn,en"
  retry_count: 3
  timeout_seconds: 120
  api_key_env: "OPENAI_API_KEY"
  fallback_text: "TODO: replace with OCR text"

markdown:
  filename_template: "{{{{date}}}}-{{{{source_basename}}}}.md"
  include_frontmatter: true
  include_source_image: true
  include_raw_ocr: true
  default_tags: ["handwriting", "ocr", "to-review"]

state:
  sqlite_path: "{root / '.handwriting-ocr' / 'state.sqlite'}"
  log_path: "{root / '.handwriting-ocr' / 'handwriting-ocr.log'}"

dedupe:
  strategy: "content_hash"
  on_duplicate: "skip"

archive:
  after_success: "move"
  after_error: "move"
"""
