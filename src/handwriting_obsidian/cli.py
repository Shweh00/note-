from __future__ import annotations

from argparse import ArgumentParser
from datetime import date
import json
from pathlib import Path
import importlib
import os
import shutil
import sqlite3
import subprocess
import sys

from .config import AppConfig, load_config
from .ocr import configured_paddle_model_dirs, create_ocr_engine, missing_required_paddle_model_dirs
from .processor import process_batch, retry_failed, watch
from .sample_validator import validate_sample_vault
from .sample_vault_init import (
    InitSampleVaultError,
    format_result_text,
    init_sample_vault,
    result_to_json,
)
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

    validate_parser = subparsers.add_parser(
        "validate-samples",
        help="Validate a real handwriting sample vault and sample-manifest.yaml before OCR acceptance.",
    )
    validate_parser.add_argument("--vault", default=".", help="Obsidian vault root. Defaults to the current directory.")
    validate_parser.add_argument("--config", default=None, help="Compatibility shortcut; infers --vault from this config path.")
    validate_parser.add_argument(
        "--manifest",
        default=None,
        help="Path to sample-manifest.yaml. Defaults to <vault>/.handwriting-ocr/real-samples/sample-manifest.yaml.",
    )
    validate_parser.add_argument("--image-dir", default=None, help="Path to the real sample image directory.")
    validate_parser.add_argument("--expected-dir", default=None, help="Path to the expected reference text directory.")
    validate_parser.add_argument("--min-count", type=int, default=None, help="Minimum valid sample count.")
    validate_parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    validate_parser.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    validate_parser.add_argument("--no-hash", action="store_true", help="Skip sha256 recomputation for local preflight only.")

    init_sample_parser = subparsers.add_parser(
        "init-sample-vault",
        help="Create a local real-sample vault scaffold only; does not OCR, upload, or mark privacy as checked.",
    )
    init_sample_parser.add_argument("--vault", required=True, help="Local test vault root to create or complete.")
    init_sample_parser.add_argument("--date", type=_parse_cli_date, default=date.today(), help="Sample date as YYYY-MM-DD.")
    init_sample_parser.add_argument("--owner", default=None, help="Dataset owner label; defaults to current system user.")
    init_sample_parser.add_argument("--force", action="store_true", help="Overwrite scaffold manifest, expected files, and sample config paths.")
    init_sample_parser.add_argument("--dry-run", action="store_true", help="Print the scaffold plan without writing files.")
    init_sample_parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")

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


def run_watch(config_path: str) -> int:  # pragma: no cover - intentionally long-running command
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


def run_validate_samples(args: object) -> int:
    config_path = getattr(args, "config", None)
    if config_path:
        vault = Path(config_path).expanduser().resolve().parent.parent
    else:
        vault = Path(getattr(args, "vault")).expanduser().resolve()
    result = validate_sample_vault(
        vault,
        manifest_path=Path(args.manifest) if args.manifest else None,
        image_dir=Path(args.image_dir) if args.image_dir else None,
        expected_dir=Path(args.expected_dir) if args.expected_dir else None,
        min_count=args.min_count,
        no_hash=args.no_hash,
    )
    failed = bool(result.failures) or (args.strict and bool(result.warnings))
    if args.format == "json":
        print(json.dumps(_sample_validation_json(result, failed), ensure_ascii=False, indent=2))
    else:
        _print_sample_validation_text(result, failed)
    return 1 if failed else 0


def run_init_sample_vault(args: object) -> int:
    try:
        result = init_sample_vault(
            Path(args.vault),
            sample_date=args.date,
            owner=args.owner,
            force=args.force,
            dry_run=args.dry_run,
        )
    except InitSampleVaultError as exc:
        if args.format == "json":
            print(json.dumps({"status": "blocked", "code": exc.code, "error": exc.message}, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    if args.format == "json":
        print(result_to_json(result))
    else:
        print(format_result_text(result))
    return 0


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
        if args.command == "validate-samples":
            return run_validate_samples(args)
        if args.command == "init-sample-vault":
            return run_init_sample_vault(args)
        if args.command == "init":
            return init_vault(args.vault, args.force)
        if args.command == "init-config":
            return init_config(args.output, args.force)
    except (KeyError, ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover


def _parse_cli_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        from argparse import ArgumentTypeError

        raise ArgumentTypeError("--date must use YYYY-MM-DD") from exc


def _summary(results: list[object]) -> str:
    success = sum(1 for result in results if getattr(result, "status") == "success")
    duplicate = sum(1 for result in results if getattr(result, "status") == "duplicate")
    failed = sum(1 for result in results if getattr(result, "status") == "failed")
    return f"done: {success} processed, {duplicate} duplicate, {failed} failed"


def _print_sample_validation_text(result: object, failed: bool) -> None:
    print(f"sample validation: {'FAIL' if failed else 'PASS'}")
    print(f"manifest: {result.manifest_path}")
    print(f"image_dir: {result.image_dir}")
    print(f"expected_dir: {result.expected_dir}")
    print(f"samples: {result.valid_samples} valid, {len(result.failures)} failed, {len(result.warnings)} warnings")
    print(f"privacy declarations: {'passed' if not any(issue.code in {'SAMPLE_PRIVACY_NOT_CHECKED', 'DATASET_PRIVACY_LEVEL_INVALID'} for issue in result.failures) else 'failed'}")
    print(f"hashes: {'not recomputed' if result.no_hash else ('verified' if not any(issue.code in {'SAMPLE_HASH_MISSING', 'SAMPLE_HASH_MISMATCH'} for issue in result.failures) else 'failed')}")
    for issue in result.issues:
        print(f"{issue.level.upper()} {issue.code}: {issue.message}")
    if not failed:
        config_path = result.manifest_path.parents[1] / "config.yaml"
        print("next:")
        print(f'  handwriting-ocr doctor --config "{config_path}"')
        print(f'  handwriting-ocr batch --config "{config_path}"')
        print(f'  handwriting-ocr status --config "{config_path}"')


def _sample_validation_json(result: object, failed: bool) -> dict[str, object]:
    return {
        "status": "fail" if failed else "pass",
        "manifest": str(result.manifest_path),
        "image_dir": str(result.image_dir),
        "expected_dir": str(result.expected_dir),
        "summary": {
            "valid_samples": result.valid_samples,
            "failed_samples": len(result.failures),
            "warnings": len(result.warnings),
            "hashes_verified": not result.no_hash and not any(
                issue.code in {"SAMPLE_HASH_MISSING", "SAMPLE_HASH_MISMATCH"} for issue in result.failures
            ),
            "privacy_declarations_passed": not any(
                issue.code in {"SAMPLE_PRIVACY_NOT_CHECKED", "DATASET_PRIVACY_LEVEL_INVALID"} for issue in result.failures
            ),
        },
        "issues": [
            {
                "level": issue.level,
                "code": issue.code,
                **({"sample_id": issue.sample_id} if issue.sample_id else {}),
                "message": issue.message,
            }
            for issue in result.issues
        ],
    }


def _check_dir(path: Path, *, writable: bool) -> str | None:
    if not path.exists():
        if writable:
            path.mkdir(parents=True, exist_ok=True)
        else:  # pragma: no cover - guarded by argparse/config validation
            return "missing"
    if writable and not os.access(path, os.W_OK):  # pragma: no cover - platform permission dependent
        return "not writable"
    return None


def _check_sqlite(path: Path) -> str | None:
    try:
        with ProcessingState.open(path):
            return None
    except sqlite3.Error as exc:  # pragma: no cover - hard to trigger with local sqlite paths
        return str(exc)


def _check_ocr(config: AppConfig) -> str | None:
    if config.ocr.mode == "mock" or config.ocr.provider == "mock":
        return None
    if config.ocr.provider == "openai" and not os.environ.get(config.ocr.api_key_env):
        return f"{config.ocr.api_key_env} is not set"
    if config.ocr.provider == "tesseract":
        return _check_tesseract(config)
    if config.ocr.provider == "paddle":
        return _check_paddle(config)
    return None


def _check_tesseract(config: AppConfig) -> str | None:
    if config.ocr.tesseract.tessdata_dir and not config.ocr.tesseract.tessdata_dir.is_dir():
        return f"tessdata_dir not found: {config.ocr.tesseract.tessdata_dir}"
    if not shutil.which(config.ocr.command):
        return f"{config.ocr.command} not found"
    try:
        result = subprocess.run(
            [config.ocr.command, "--list-langs"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"cannot list Tesseract languages: {exc}"
    if result.returncode != 0:
        return f"cannot list Tesseract languages: {result.stderr.strip() or result.stdout.strip()}"
    available = {line.strip() for line in result.stdout.splitlines() if line.strip() and not line.startswith("List of")}
    required = set(config.ocr.tesseract.lang.split("+"))
    missing = sorted(required - available)
    if missing:
        return f"missing Tesseract language data: {', '.join(missing)}"
    return None


def _check_paddle(config: AppConfig) -> str | None:
    missing = missing_required_paddle_model_dirs(config.ocr)
    if missing:
        return "PaddleOCR local model directories missing and downloads disabled: " + ", ".join(missing)
    for name, directory in configured_paddle_model_dirs(config.ocr).items():
        if not directory.is_dir():
            return f"PaddleOCR {name} not found: {directory}"
    if config.ocr.paddle.device != "cpu":
        return f"device {config.ocr.paddle.device!r} is not supported by doctor in this version; use cpu"
    try:
        importlib.import_module("paddleocr")
    except ImportError:
        return "PaddleOCR is not installed"
    try:
        create_ocr_engine(config.ocr)
    except RuntimeError as exc:
        return str(exc)
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
  command: "tesseract"
  offline_no_network: true
  paddle:
    engine: "paddle"
    device: "cpu"
    lang: "ch"
    text_detection_model_dir: ""
    text_recognition_model_dir: ""
    doc_orientation_classify_model_dir: ""
    doc_unwarping_model_dir: ""
    textline_orientation_model_dir: ""
    allow_model_download: false
    use_doc_orientation_classify: false
    use_doc_unwarping: false
    use_textline_orientation: false
  tesseract:
    lang: "chi_sim+eng"
    psm: 6
    oem: 1
    tessdata_dir: ""

markdown:
  filename_template: "{{{{date}}}}-{{{{source_basename}}}}.md"
  include_frontmatter: true
  include_source_image: true
  include_raw_ocr: true
  default_tags: ["handwriting", "ocr", "to-review"]
  date_folder:
    enabled: false
    pattern: "YYYY/MM/DD"
    date_source: "processed_at"
  template:
    mode: "default"
    file_path: ""
    missing_behavior: "fallback"

state:
  sqlite_path: "{root / '.handwriting-ocr' / 'state.sqlite'}"
  # Reserved for future app-managed file logging. Long-running watch logs are
  # currently stdout/stderr captured by systemd, launchd, or the Windows script.
  log_path: "{root / '.handwriting-ocr' / 'handwriting-ocr.log'}"

dedupe:
  strategy: "content_hash"
  on_duplicate: "skip"

archive:
  after_success: "move"
  after_error: "move"

index:
  enabled: false
  path: "Index.md"
  title: "手写识别索引"
  grouping: "date"
  sort: "desc"
  include_status: true
  include_source_link: true
  update_mode: "managed_block"
"""
