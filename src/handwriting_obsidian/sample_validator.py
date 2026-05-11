from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
from typing import Any


IMAGE_FILE_RE = re.compile(
    r"^(?P<sample_id>\d{4}-\d{2}-\d{2}_(?P<number>[0-9]{3})_(?P<lang>zh|en|mixed|num)_"
    r"(?P<scenario>[a-z0-9-]+)_(?P<quality>[a-z0-9-]+))\.(?P<ext>jpg|jpeg|png)$",
    re.IGNORECASE,
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_FRONTMATTER_RE = re.compile(r"^---\n(?P<frontmatter>.*?)\n---\n?(?P<body>.*)$", re.DOTALL)
SECTION_RE = re.compile(r"^## (?P<name>[A-Za-z0-9_-]+)\s*$", re.MULTILINE)
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
PLACEHOLDER_PHRASES = (
    "在这里填写人工转写文本",
    "TODO",
    "replace with",
    "人工转写文本",
)
REQUIRED_SECTIONS = (
    "expected_text",
    "must_include",
    "acceptable_variants",
    "ignore_regions",
    "notes_for_reviewer",
)
REQUIRED_COVERAGE_SUFFIXES = {
    "001_zh_meeting_clear",
    "002_zh_todo_faint",
    "003_zh_diary_tilted",
    "004_zh_vertical_layout",
    "005_en_notes_clear",
    "006_mixed_bilingual_clear",
    "007_num_math_grid",
    "008_zh_schedule_table",
    "009_num_receipt_amounts",
    "010_mixed_contact_redacted",
    "011_zh_mindmap_arrows",
    "012_mixed_recipe_shadow",
    "013_zh_sticky_small",
    "014_zh_notes_crowded",
    "015_zh_revision_crossed",
    "016_zh_contrast_faint",
    "017_zh_photo_tilted",
    "018_mixed_pages_marker",
}


@dataclass(frozen=True)
class SampleIssue:
    level: str
    code: str
    message: str
    sample_id: str | None = None


@dataclass(frozen=True)
class SampleValidationResult:
    manifest_path: Path
    image_dir: Path
    expected_dir: Path
    issues: list[SampleIssue] = field(default_factory=list)
    min_count: int = 18
    no_hash: bool = False

    @property
    def failures(self) -> list[SampleIssue]:
        return [issue for issue in self.issues if issue.level == "fail"]

    @property
    def warnings(self) -> list[SampleIssue]:
        return [issue for issue in self.issues if issue.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def valid_samples(self) -> int:
        failed_sample_ids = {issue.sample_id for issue in self.failures if issue.sample_id}
        sample_ids = {
            issue.sample_id
            for issue in self.issues
            if issue.sample_id and issue.code not in {"COVERAGE_REQUIRED_SAMPLE_MISSING"}
        }
        return max(0, len(sample_ids - failed_sample_ids))


def validate_sample_vault(
    vault: Path,
    *,
    manifest_path: Path | None = None,
    image_dir: Path | None = None,
    expected_dir: Path | None = None,
    min_count: int | None = None,
    no_hash: bool = False,
) -> SampleValidationResult:
    vault_root = vault.expanduser().resolve()
    manifest = (manifest_path.expanduser().resolve() if manifest_path else vault_root / ".handwriting-ocr" / "real-samples" / "sample-manifest.yaml")
    final_image_dir = image_dir.expanduser().resolve() if image_dir else vault_root / "Inbox" / "HandwritingImages" / "real-samples"
    final_expected_dir = expected_dir.expanduser().resolve() if expected_dir else vault_root / ".handwriting-ocr" / "real-samples" / "expected"
    issues: list[SampleIssue] = []
    required_count = min_count or 18

    if not vault_root.exists():
        _fail(issues, "SAMPLE_MANIFEST_MISSING", f"vault not found: {vault_root}")
    if not manifest.is_file():
        _fail(issues, "SAMPLE_MANIFEST_MISSING", f"manifest not found: {manifest}")
    if not final_image_dir.is_dir():
        _fail(issues, "SAMPLE_IMAGE_DIR_MISSING", f"image directory not found: {final_image_dir}")
    if not final_expected_dir.is_dir():
        _fail(issues, "SAMPLE_EXPECTED_DIR_MISSING", f"expected directory not found: {final_expected_dir}")

    data: dict[str, Any] = {}
    if manifest.is_file():
        try:
            loaded = _load_yaml(manifest)
            if isinstance(loaded, dict):
                data = loaded
            else:
                _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", "manifest root must be a mapping")
        except Exception as exc:
            _fail(issues, "SAMPLE_MANIFEST_INVALID_YAML", f"manifest YAML could not be parsed: {exc}")

    dataset = _require_mapping(data.get("dataset"), "dataset", issues)
    validation = _require_mapping(data.get("validation"), "validation", issues)
    samples_raw = data.get("samples")
    samples = samples_raw if isinstance(samples_raw, list) else []
    if not isinstance(samples_raw, list):
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", "samples must be a non-empty list")
    if not samples:
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", "samples must be a non-empty list")

    if min_count is None and isinstance(validation.get("minimum_sample_count"), int):
        required_count = int(validation["minimum_sample_count"])

    _validate_dataset(dataset, issues)
    configured_image_dir = _resolve_vault_path(vault_root, dataset.get("image_dir", "")) if dataset.get("image_dir") else None
    configured_expected_dir = _resolve_vault_path(vault_root, dataset.get("expected_dir", "")) if dataset.get("expected_dir") else None
    if configured_image_dir and configured_image_dir != final_image_dir:
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"dataset.image_dir={configured_image_dir} expected={final_image_dir}")
    if configured_expected_dir and configured_expected_dir != final_expected_dir:
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"dataset.expected_dir={configured_expected_dir} expected={final_expected_dir}")

    _validate_config(vault_root, dataset, final_image_dir, issues)
    image_files, unsupported = _collect_images(final_image_dir)
    expected_files = sorted(final_expected_dir.glob("*.expected.md")) if final_expected_dir.is_dir() else []
    if unsupported:
        _fail(issues, "SAMPLE_UNSUPPORTED_EXTENSION", f"unsupported image extensions; convert first: {', '.join(path.name for path in unsupported)}")
    if len(image_files) < required_count:
        _fail(issues, "SAMPLE_COUNT_TOO_LOW", f"image count={len(image_files)} required>={required_count} dir={final_image_dir}")
    if len(samples) < required_count:
        _fail(issues, "SAMPLE_COUNT_TOO_LOW", f"manifest samples={len(samples)} required>={required_count}")
    if len(expected_files) < required_count:
        _fail(issues, "EXPECTED_COUNT_TOO_LOW", f"expected files={len(expected_files)} required>={required_count} dir={final_expected_dir}")
    if no_hash:
        _warn(issues, "NO_HASH_MODE_USED", "--no-hash skips sha256 recomputation and is not final admission")

    seen_ids: set[str] = set()
    seen_numbers: set[str] = set()
    manifest_image_names: set[str] = set()
    manifest_expected_paths: set[Path] = set()
    coverage: set[str] = set()
    for index, sample_raw in enumerate(samples, start=1):
        if not isinstance(sample_raw, dict):
            _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"samples[{index}] must be a mapping")
            continue
        _validate_sample(
            sample_raw,
            index,
            final_image_dir,
            final_expected_dir,
            manifest.parent,
            seen_ids,
            seen_numbers,
            manifest_image_names,
            manifest_expected_paths,
            coverage,
            issues,
            no_hash=no_hash,
        )

    for suffix in sorted(REQUIRED_COVERAGE_SUFFIXES - coverage):
        _fail(issues, "COVERAGE_REQUIRED_SAMPLE_MISSING", f"required coverage missing: {suffix}")

    extra_images = sorted(path.name for path in image_files if path.name not in manifest_image_names)
    for image_name in extra_images:
        _warn(issues, "EXTRA_IMAGE_NOT_IN_MANIFEST", f"image={image_name} is not listed in manifest")

    return SampleValidationResult(manifest, final_image_dir, final_expected_dir, issues, required_count, no_hash)


def _validate_dataset(dataset: dict[str, Any], issues: list[SampleIssue]) -> None:
    for field_name in ("id", "version", "owner", "created_at", "image_dir", "expected_dir", "config_path"):
        if dataset.get(field_name) in (None, ""):
            _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"dataset.{field_name} is required")
    if dataset.get("privacy_level") != "redacted-local-only":
        _fail(issues, "DATASET_PRIVACY_LEVEL_INVALID", "dataset.privacy_level must be redacted-local-only")


def _validate_config(vault_root: Path, dataset: dict[str, Any], expected_image_dir: Path, issues: list[SampleIssue]) -> None:
    config_path = _resolve_vault_path(vault_root, dataset.get("config_path", ".handwriting-ocr/config.yaml"))
    if not config_path.is_file():
        _fail(issues, "CONFIG_FILE_MISSING", f"config file not found: {config_path}")
        return
    try:
        config = _load_yaml(config_path)
    except Exception as exc:
        _fail(issues, "CONFIG_INVALID_YAML", f"config YAML could not be parsed: {exc}")
        return
    watch = config.get("watch") if isinstance(config, dict) else None
    if not isinstance(watch, dict) or not watch.get("input_dir"):
        _fail(issues, "CONFIG_WATCH_INPUT_DIR_MISSING", f"watch.input_dir missing in {config_path}")
        return
    configured = _resolve_path(config_path.parent, watch["input_dir"])
    if configured != expected_image_dir:
        _fail(issues, "CONFIG_WATCH_INPUT_DIR_INVALID", f"watch.input_dir={configured} expected={expected_image_dir}")
    if not configured.exists():
        _fail(issues, "CONFIG_WATCH_INPUT_DIR_INVALID", f"watch.input_dir does not exist: {configured}")


def _validate_sample(
    sample: dict[str, Any],
    index: int,
    image_dir: Path,
    expected_dir: Path,
    manifest_dir: Path,
    seen_ids: set[str],
    seen_numbers: set[str],
    manifest_image_names: set[str],
    manifest_expected_paths: set[Path],
    coverage: set[str],
    issues: list[SampleIssue],
    *,
    no_hash: bool,
) -> None:
    sample_id = str(sample.get("sample_id", ""))
    image_file = str(sample.get("image_file", ""))
    expected_file = str(sample.get("expected_file", ""))
    context = f"samples[{index}]"
    if not sample_id:
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"{context}.sample_id is required")
    elif sample_id in seen_ids:
        _fail(issues, "SAMPLE_DUPLICATE_ID", f"sample_id is duplicated: {sample_id}", sample_id)
    seen_ids.add(sample_id)

    match = IMAGE_FILE_RE.fullmatch(image_file)
    if not match:
        _fail(issues, "SAMPLE_FILENAME_INVALID", f"sample_id={sample_id} image_file={image_file!r} must match YYYY-MM-DD_NNN_<lang>_<scenario>_<quality>.(jpg|jpeg|png)", sample_id)
    else:
        if match.group("sample_id") != sample_id:
            _fail(issues, "SAMPLE_ID_MISMATCH", f"sample_id={sample_id} image_file basename={match.group('sample_id')}", sample_id)
        number = match.group("number")
        if number in seen_numbers:
            _fail(issues, "SAMPLE_DUPLICATE_ID", f"sample number is duplicated: {number}", sample_id)
        seen_numbers.add(number)
        coverage.add(sample_id.split("_", 1)[1])
        _validate_language(sample_id, match.group("lang").lower(), str(sample.get("language", "")), issues)
    manifest_image_names.add(image_file)

    for field_name in ("language", "scenario", "expected_character_count", "review_priority"):
        if sample.get(field_name) in (None, ""):
            _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"sample_id={sample_id} {field_name} is required", sample_id)
    if not isinstance(sample.get("quality_tags"), list) or not sample.get("quality_tags"):
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"sample_id={sample_id} quality_tags must be a non-empty array", sample_id)
    if not isinstance(sample.get("must_include"), list) or not sample.get("must_include"):
        _fail(issues, "EXPECTED_MUST_INCLUDE_MISSING", f"sample_id={sample_id} samples[].must_include must be a non-empty array", sample_id)
    if sample.get("review_priority") not in {"p0", "p1", "p2"}:
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"sample_id={sample_id} review_priority must be p0, p1, or p2", sample_id)
    if not isinstance(sample.get("expected_character_count"), int) or sample.get("expected_character_count", 0) <= 0:
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"sample_id={sample_id} expected_character_count must be a positive integer", sample_id)
    if sample.get("privacy_checked") is not True:
        _fail(issues, "SAMPLE_PRIVACY_NOT_CHECKED", f"sample_id={sample_id} field=samples[].privacy_checked must be true", sample_id)

    image_path = image_dir / image_file
    if not image_path.is_file():
        _fail(issues, "SAMPLE_IMAGE_MISSING", f"sample_id={sample_id} image not found: {image_path}", sample_id)
    configured_sha = str(sample.get("image_sha256", "")).lower()
    if configured_sha in {"", "todo"}:
        _fail(issues, "SAMPLE_HASH_MISSING", f"sample_id={sample_id} image_sha256 must be filled with actual sha256", sample_id)
        _fail(issues, "SAMPLE_MANIFEST_TEMPLATE_VALUE", f"sample_id={sample_id} image_sha256 still has template value", sample_id)
    elif not SHA256_RE.fullmatch(configured_sha):
        _fail(issues, "SAMPLE_HASH_MISSING", f"sample_id={sample_id} image_sha256 must be a 64-character lowercase SHA-256 hex digest", sample_id)
    elif image_path.is_file() and not no_hash:
        actual_sha = _sha256_file(image_path)
        if configured_sha != actual_sha:
            _fail(issues, "SAMPLE_HASH_MISMATCH", f"sample_id={sample_id} expected={configured_sha} actual={actual_sha} image={image_file}", sample_id)

    if not expected_file:
        _fail(issues, "EXPECTED_FILE_MISSING", f"sample_id={sample_id} expected_file is required", sample_id)
        return
    expected_path = _resolve_path(manifest_dir, expected_file)
    manifest_expected_paths.add(expected_path)
    if not expected_path.is_file():
        _fail(issues, "EXPECTED_FILE_MISSING", f"sample_id={sample_id} path={expected_path}", sample_id)
        return
    _validate_expected_file(expected_path, sample, image_file, issues)


def _validate_language(sample_id: str, lang_token: str, language: str, issues: list[SampleIssue]) -> None:
    normalized = {part.strip().lower() for part in language.replace(";", ",").split(",") if part.strip()}
    ok = (
        (lang_token == "zh" and "zh-cn" in normalized)
        or (lang_token == "en" and normalized == {"en"})
        or (lang_token == "mixed" and len(normalized) > 1)
        or (lang_token == "num" and (normalized == {"numbers"} or len(normalized) > 1))
    )
    if language and not ok:
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"sample_id={sample_id} language={language!r} does not match filename lang={lang_token}", sample_id)


def _validate_expected_file(expected_path: Path, sample: dict[str, Any], image_file: str, issues: list[SampleIssue]) -> None:
    sample_id = str(sample.get("sample_id", ""))
    text = expected_path.read_text(encoding="utf-8")
    match = EXPECTED_FRONTMATTER_RE.match(text)
    if not match:
        _fail(issues, "EXPECTED_FRONTMATTER_INVALID", f"sample_id={sample_id} expected frontmatter missing or invalid: {expected_path}", sample_id)
        return
    try:
        frontmatter = _load_yaml_text(match.group("frontmatter"))
    except Exception as exc:
        _fail(issues, "EXPECTED_FRONTMATTER_INVALID", f"sample_id={sample_id} expected frontmatter YAML invalid: {exc}", sample_id)
        return
    for field_name, expected in (
        ("sample_id", sample_id),
        ("image_file", image_file),
        ("language", sample.get("language")),
        ("scenario", sample.get("scenario")),
    ):
        if frontmatter.get(field_name) != expected:
            _fail(issues, "EXPECTED_METADATA_MISMATCH", f"sample_id={sample_id} frontmatter {field_name}={frontmatter.get(field_name)!r} expected={expected!r}", sample_id)
    if frontmatter.get("privacy_checked") is not True:
        _fail(issues, "SAMPLE_PRIVACY_NOT_CHECKED", f"sample_id={sample_id} expected frontmatter privacy_checked must be true", sample_id)

    body = match.group("body")
    sections = {section.group("name") for section in SECTION_RE.finditer(body)}
    for section in REQUIRED_SECTIONS:
        if section not in sections:
            _fail(issues, "EXPECTED_FRONTMATTER_INVALID", f"sample_id={sample_id} expected file missing section ## {section}", sample_id)
    expected_text = _section_body(body, "expected_text")
    if not expected_text.strip() or any(phrase.lower() in expected_text.lower() for phrase in PLACEHOLDER_PHRASES):
        _fail(issues, "EXPECTED_TEXT_PLACEHOLDER", f"sample_id={sample_id} expected_text is empty or still a template placeholder", sample_id)
    must_include_text = _section_body(body, "must_include")
    if not must_include_text.strip():
        _fail(issues, "EXPECTED_MUST_INCLUDE_MISSING", f"sample_id={sample_id} expected ## must_include must contain at least one item", sample_id)
    missing_terms = [str(term) for term in sample.get("must_include", []) if str(term) not in must_include_text]
    if missing_terms:
        _fail(issues, "EXPECTED_MUST_INCLUDE_MISSING", f"sample_id={sample_id} expected must_include missing manifest terms: {', '.join(missing_terms)}", sample_id)


def _section_body(text: str, section: str) -> str:
    match = re.search(rf"^## {re.escape(section)}\s*$", text, re.MULTILINE)
    if not match:
        return ""
    next_match = re.search(r"^## [A-Za-z0-9_-]+\s*$", text[match.end() :], re.MULTILINE)
    if not next_match:
        return text[match.end() :]
    return text[match.end() : match.end() + next_match.start()]


def _collect_images(image_dir: Path) -> tuple[list[Path], list[Path]]:
    if not image_dir.is_dir():
        return [], []
    files = [path for path in image_dir.iterdir() if path.is_file()]
    images = sorted(path for path in files if path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS)
    unsupported = sorted(path for path in files if path.suffix and path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS)
    return images, unsupported


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise RuntimeError("PyYAML is required to validate sample manifests") from exc
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_yaml_text(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise RuntimeError("PyYAML is required to validate expected files") from exc
    loaded = yaml.safe_load(text)
    return loaded if isinstance(loaded, dict) else {}


def _require_mapping(value: Any, name: str, issues: list[SampleIssue]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(issues, "SAMPLE_MANIFEST_SCHEMA_INVALID", f"{name} must be a mapping")
        return {}
    return value


def _resolve_vault_path(vault_root: Path, value: Any) -> Path:
    return _resolve_path(vault_root, value)


def _resolve_path(base: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(issues: list[SampleIssue], code: str, message: str, sample_id: str | None = None) -> None:
    issues.append(SampleIssue("fail", code, message, sample_id))


def _warn(issues: list[SampleIssue], code: str, message: str, sample_id: str | None = None) -> None:
    issues.append(SampleIssue("warn", code, message, sample_id))
