from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import getpass
import json
from pathlib import Path
from typing import Any

IMAGE_DIR_REL = Path("Inbox") / "HandwritingImages" / "real-samples"
PROCESSED_DIR_NAME = "_processed"
ERROR_DIR_NAME = "_errors"
NOTES_DIR_REL = Path("Inbox") / "HandwritingNotes"
CONFIG_REL = Path(".handwriting-ocr") / "config.yaml"
SAMPLE_ROOT_REL = Path(".handwriting-ocr") / "real-samples"
MANIFEST_REL = SAMPLE_ROOT_REL / "sample-manifest.yaml"
EXPECTED_DIR_REL = SAMPLE_ROOT_REL / "expected"
TEMPLATE_DATE = "2026-05-11"
PLACEHOLDER_TEXT = "在这里填写人工转写文本。保留原始换行、列表层级和关键标点。"


@dataclass(frozen=True)
class InitSampleVaultResult:
    status: str
    vault: Path
    image_dir: Path
    manifest: Path
    expected_dir: Path
    created_files: list[str]
    skipped_files: list[str]
    warnings: list[str]
    next_commands: list[str]
    dry_run: bool


class InitSampleVaultError(Exception):
    def __init__(self, code: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def init_sample_vault(
    vault: Path,
    *,
    sample_date: date,
    owner: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> InitSampleVaultResult:
    root = vault.expanduser().resolve()
    owner_name = owner or getpass.getuser() or "dev"
    manifest_template = _load_manifest_template()
    manifest_data = _build_manifest(manifest_template, sample_date, owner_name)
    expected_text = _load_expected_template()

    image_dir = root / IMAGE_DIR_REL
    manifest_path = root / MANIFEST_REL
    expected_dir = root / EXPECTED_DIR_REL
    config_path = root / CONFIG_REL
    directories = [
        image_dir,
        image_dir / PROCESSED_DIR_NAME,
        image_dir / ERROR_DIR_NAME,
        root / NOTES_DIR_REL,
        expected_dir,
    ]
    expected_paths = [expected_dir / Path(sample["expected_file"]).name for sample in manifest_data["samples"]]
    created: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    _check_scaffold_conflicts(manifest_path, expected_paths, config_path, image_dir, force)
    existing_images = sorted(
        path.name for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    ) if image_dir.is_dir() else []
    if existing_images:
        warnings.append(
            "image directory already contains images; init-sample-vault will not move, delete, or rename them. "
            "Run validate-samples to check manifest alignment."
        )

    if dry_run:
        planned = [str(path) for path in directories + [config_path, manifest_path, *expected_paths]]
        return _result("ready", root, image_dir, manifest_path, expected_dir, planned, [], warnings, dry_run=True)

    try:
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        _write_or_skip_config(config_path, root, force, created, skipped)
        _write_yaml_file(manifest_path, manifest_data)
        created.append(str(manifest_path))
        if force:
            _remove_stale_expected_files(expected_dir, set(expected_paths))
        for sample in manifest_data["samples"]:
            path = expected_dir / Path(sample["expected_file"]).name
            path.write_text(_expected_file_text(expected_text, sample), encoding="utf-8")
            created.append(str(path))
    except OSError as exc:
        raise InitSampleVaultError("SAMPLE_VAULT_WRITE_FAILED", str(exc), exit_code=3) from exc

    return _result("ready", root, image_dir, manifest_path, expected_dir, created, skipped, warnings, dry_run=False)


def result_to_json(result: InitSampleVaultResult) -> str:
    return json.dumps(
        {
            "status": result.status,
            "vault": str(result.vault),
            "image_dir": str(result.image_dir),
            "manifest": str(result.manifest),
            "expected_dir": str(result.expected_dir),
            "created_files": result.created_files,
            "skipped_files": result.skipped_files,
            "warnings": result.warnings,
            "next_commands": result.next_commands,
            "dry_run": result.dry_run,
        },
        ensure_ascii=False,
        indent=2,
    )


def format_result_text(result: InitSampleVaultResult) -> str:
    action = "DRY-RUN" if result.dry_run else "READY"
    expected_count = sum(1 for path in result.created_files if path.endswith(".expected.md"))
    lines = [
        f"sample vault scaffold: {action}",
        f"vault: {result.vault}",
        f"image_dir: {result.image_dir}",
        f"manifest: {result.manifest}",
        f"expected_dir: {result.expected_dir}",
        f"expected_files: {expected_count} created" if not result.dry_run else "expected_files: 18 planned",
    ]
    if result.warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in result.warnings)
    lines.extend(
        [
            "",
            "privacy:",
            "  - Add 18 redacted real handwriting images matching manifest image_file names exactly.",
            "  - Do not include real names, phone numbers, emails, addresses, IDs, order numbers, medical IDs, customer names, or private project data.",
            "  - Check that photo backgrounds do not show screens, shipping labels, IDs, family photos, or door numbers.",
            "  - Remove EXIF/GPS before placing images in the sample directory.",
            "  - Keep privacy_checked: false until image content, background, EXIF/GPS, and expected text have all been reviewed.",
            "",
            "next:",
            "  1. Add redacted images matching manifest image_file names.",
            "  2. Fill expected/*.expected.md with human transcriptions.",
            "  3. Replace image_sha256 TODO values:",
            f'     cd "{result.image_dir}" && sha256sum <image-file>',
            "     macOS: shasum -a 256 <image-file>",
            "     Windows PowerShell: Get-FileHash -Algorithm SHA256 <image-file>",
            "     Paste the lowercase 64-character hex hash into sample-manifest.yaml.",
            "  4. Set privacy_checked: true only after redaction, background check, EXIF/GPS removal, and expected text review.",
            f'  5. Run: handwriting-ocr validate-samples --vault "{result.vault}"',
            "  6. After PASS:",
            f'     handwriting-ocr doctor --config "{result.vault / CONFIG_REL}"',
            f'     handwriting-ocr batch --config "{result.vault / CONFIG_REL}"',
            f'     handwriting-ocr status --config "{result.vault / CONFIG_REL}"',
        ]
    )
    return "\n".join(lines)


def _result(
    status: str,
    root: Path,
    image_dir: Path,
    manifest_path: Path,
    expected_dir: Path,
    created: list[str],
    skipped: list[str],
    warnings: list[str],
    *,
    dry_run: bool,
) -> InitSampleVaultResult:
    config_path = root / CONFIG_REL
    return InitSampleVaultResult(
        status=status,
        vault=root,
        image_dir=image_dir,
        manifest=manifest_path,
        expected_dir=expected_dir,
        created_files=created,
        skipped_files=skipped,
        warnings=warnings,
        next_commands=[
            f'handwriting-ocr validate-samples --vault "{root}"',
            f'handwriting-ocr doctor --config "{config_path}"',
            f'handwriting-ocr batch --config "{config_path}"',
            f'handwriting-ocr status --config "{config_path}"',
        ],
        dry_run=dry_run,
    )


def _load_manifest_template() -> dict[str, Any]:
    path = _project_root() / "docs" / "sample-manifest.template.yaml"
    if not path.is_file():
        raise InitSampleVaultError("TEMPLATE_MISSING", f"{path} not found", exit_code=3)
    return _load_yaml_file(path)


def _load_expected_template() -> str:
    path = _project_root() / "docs" / "sample.expected.md.template"
    if not path.is_file():
        raise InitSampleVaultError("TEMPLATE_MISSING", f"{path} not found", exit_code=3)
    return path.read_text(encoding="utf-8")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_yaml_file(path: Path) -> dict[str, Any]:
    import yaml  # type: ignore[import-untyped]

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise InitSampleVaultError("TEMPLATE_INVALID", f"{path} must contain a YAML mapping", exit_code=3)
    return loaded


def _build_manifest(template: dict[str, Any], sample_date: date, owner: str) -> dict[str, Any]:
    created_at = sample_date.isoformat()
    compact_date = created_at.replace("-", "")
    data = {
        "dataset": dict(template.get("dataset", {})),
        "validation": dict(template.get("validation", {})),
        "samples": [dict(sample) for sample in template.get("samples", [])],
    }
    data["dataset"].update(
        {
            "id": f"real-handwriting-min18-{compact_date}",
            "owner": owner,
            "created_at": created_at,
            "privacy_level": "redacted-local-only",
            "image_dir": IMAGE_DIR_REL.as_posix(),
            "expected_dir": EXPECTED_DIR_REL.as_posix(),
            "config_path": CONFIG_REL.as_posix(),
        }
    )
    data["validation"]["minimum_sample_count"] = 18
    data["validation"]["required_commands"] = [
        'handwriting-ocr validate-samples --vault "$HW_SAMPLE_VAULT"',
        'handwriting-ocr doctor --config "$HW_SAMPLE_VAULT/.handwriting-ocr/config.yaml"',
        'handwriting-ocr batch --config "$HW_SAMPLE_VAULT/.handwriting-ocr/config.yaml"',
        'handwriting-ocr status --config "$HW_SAMPLE_VAULT/.handwriting-ocr/config.yaml"',
    ]
    for sample in data["samples"]:
        old_id = str(sample["sample_id"])
        sample_id = old_id.replace(TEMPLATE_DATE, created_at, 1)
        extension = Path(str(sample["image_file"])).suffix or ".jpg"
        sample.update(
            {
                "sample_id": sample_id,
                "image_file": f"{sample_id}{extension}",
                "image_sha256": "TODO",
                "expected_file": f"expected/{sample_id}.expected.md",
                "privacy_checked": False,
            }
        )
    return data


def _check_scaffold_conflicts(
    manifest_path: Path,
    expected_paths: list[Path],
    config_path: Path,
    image_dir: Path,
    force: bool,
) -> None:
    if manifest_path.exists() and not force:
        raise InitSampleVaultError(
            "SAMPLE_VAULT_EXISTS",
            f"manifest already exists: {manifest_path}; use --force to overwrite scaffold files",
        )
    existing_expected = [path for path in expected_paths if path.exists()]
    if existing_expected and not force:
        preview = ", ".join(str(path) for path in existing_expected[:5])
        raise InitSampleVaultError(
            "SAMPLE_VAULT_EXISTS",
            f"expected files already exist: {preview}; use --force to overwrite scaffold files",
        )
    if config_path.exists():
        configured = _read_watch_input_dir(config_path)
        expected = image_dir.resolve()
        if configured and configured != expected and not force:
            raise InitSampleVaultError(
                "CONFIG_WATCH_INPUT_DIR_CONFLICT",
                f"existing watch.input_dir={configured} expected={expected}; use --force or edit config.yaml manually",
            )


def _read_watch_input_dir(config_path: Path) -> Path | None:
    data = _load_yaml_file(config_path)
    watch = data.get("watch")
    if not isinstance(watch, dict) or not watch.get("input_dir"):
        return None
    return _resolve_path(config_path.parent, watch["input_dir"])


def _write_or_skip_config(config_path: Path, root: Path, force: bool, created: list[str], skipped: list[str]) -> None:
    if config_path.exists():
        configured = _read_watch_input_dir(config_path)
        expected = (root / IMAGE_DIR_REL).resolve()
        if not force and configured == expected:
            skipped.append(str(config_path))
            return
        data = _load_yaml_file(config_path)
    else:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(_starter_config_text(root)) or {}
    watch = data.get("watch")
    if not isinstance(watch, dict):
        watch = {}
        data["watch"] = watch
    watch["input_dir"] = str(root / IMAGE_DIR_REL)
    watch["output_dir"] = str(root / NOTES_DIR_REL)
    watch["processed_dir"] = str(root / IMAGE_DIR_REL / PROCESSED_DIR_NAME)
    watch["error_dir"] = str(root / IMAGE_DIR_REL / ERROR_DIR_NAME)
    _write_yaml_file(config_path, data)
    created.append(str(config_path))


def _remove_stale_expected_files(expected_dir: Path, current_expected_paths: set[Path]) -> None:
    if not expected_dir.is_dir():
        return
    for path in expected_dir.glob("*.expected.md"):
        if path not in current_expected_paths:
            path.unlink()


def _write_yaml_file(path: Path, data: dict[str, Any]) -> None:
    import yaml  # type: ignore[import-untyped]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _expected_file_text(template: str, sample: dict[str, Any]) -> str:
    body_match = template.split("---", 2)
    body = body_match[2].lstrip() if len(body_match) == 3 else template
    body = _replace_section(
        body,
        "expected_text",
        PLACEHOLDER_TEXT + "\n\n- TODO: replace with redacted human transcription.",
    )
    body = _replace_section(
        body,
        "must_include",
        "\n".join(f"- {json.dumps(str(term), ensure_ascii=False)}" for term in sample.get("must_include", [])),
    )
    frontmatter = {
        "sample_id": sample["sample_id"],
        "image_file": sample["image_file"],
        "language": sample["language"],
        "scenario": sample["scenario"],
        "quality_tags": sample["quality_tags"],
        "expected_character_count": sample["expected_character_count"],
        "privacy_checked": False,
    }
    import yaml  # type: ignore[import-untyped]

    return "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False) + "---\n\n" + body.rstrip() + "\n"


def _replace_section(body: str, section: str, replacement: str) -> str:
    marker = f"## {section}"
    start = body.find(marker)
    if start == -1:
        return body
    content_start = body.find("\n", start)
    if content_start == -1:
        return body
    next_start = body.find("\n## ", content_start + 1)
    if next_start == -1:
        return body[: content_start + 1] + "\n" + replacement.rstrip() + "\n"
    return body[: content_start + 1] + "\n" + replacement.rstrip() + "\n" + body[next_start:]


def _resolve_path(base: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()


def _starter_config_text(root: Path) -> str:
    return f"""watch:
  input_dir: "{root / IMAGE_DIR_REL}"
  output_dir: "{root / NOTES_DIR_REL}"
  processed_dir: "{root / IMAGE_DIR_REL / PROCESSED_DIR_NAME}"
  error_dir: "{root / IMAGE_DIR_REL / ERROR_DIR_NAME}"
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
