from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

from .config import AppConfig, SUPPORTED_EXTENSIONS


SAMPLE_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_[0-9]{3}_(zh|en|mixed|num)_[a-z0-9-]+_[a-z0-9-]+$")
IMAGE_FILE_RE = re.compile(
    r"^(?P<sample_id>\d{4}-\d{2}-\d{2}_[0-9]{3}_(zh|en|mixed|num)_[a-z0-9-]+_[a-z0-9-]+)"
    r"\.(jpg|jpeg|png)$",
    re.IGNORECASE,
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_FRONTMATTER_RE = re.compile(r"^---\n(?P<frontmatter>.*?)\n---", re.DOTALL)


@dataclass(frozen=True)
class SampleValidationResult:
    manifest_path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_sample_vault(config: AppConfig, manifest_path: Path) -> SampleValidationResult:
    manifest = manifest_path.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    if not manifest.is_file():
        return SampleValidationResult(manifest, [f"manifest not found: {manifest}"], warnings)

    try:
        data = _load_yaml(manifest)
    except Exception as exc:
        return SampleValidationResult(manifest, [f"manifest could not be parsed: {exc}"], warnings)

    if not isinstance(data, dict):
        return SampleValidationResult(manifest, ["manifest root must be a mapping"], warnings)

    dataset = _mapping(data.get("dataset"), "dataset", errors)
    validation = _mapping(data.get("validation"), "validation", errors)
    samples_value = data.get("samples")
    if not isinstance(samples_value, list):
        errors.append("samples must be a list")
        samples: list[Any] = []
    else:
        samples = samples_value

    expected_count = int(validation.get("minimum_sample_count", 18)) if isinstance(validation, dict) else 18
    if len(samples) != expected_count:
        errors.append(f"samples must contain exactly {expected_count} entries, got {len(samples)}")

    image_dir = _resolve_manifest_path(manifest.parent, dataset.get("image_dir", "")) if dataset else manifest.parent
    expected_dir = _resolve_manifest_path(manifest.parent, dataset.get("expected_dir", "expected")) if dataset else manifest.parent / "expected"
    if config.input_dir.resolve() != image_dir.resolve():
        errors.append(f"watch.input_dir must match dataset.image_dir: {config.input_dir} != {image_dir}")
    if not image_dir.is_dir():
        errors.append(f"dataset.image_dir does not exist: {image_dir}")
    if not expected_dir.is_dir():
        errors.append(f"dataset.expected_dir does not exist: {expected_dir}")

    allowed_extensions = {
        "." + str(ext).lower().lstrip(".") for ext in validation.get("allowed_extensions", ("jpg", "jpeg", "png"))
    }
    allowed_extensions &= {".jpg", ".jpeg", ".png"}
    if not allowed_extensions:
        allowed_extensions = {".jpg", ".jpeg", ".png"}

    image_files = _list_images(image_dir, allowed_extensions) if image_dir.is_dir() else []
    if len(image_files) != expected_count:
        errors.append(f"image directory must contain exactly {expected_count} images, got {len(image_files)} in {image_dir}")
    expected_files = sorted(expected_dir.glob("*.expected.md")) if expected_dir.is_dir() else []
    if len(expected_files) != expected_count:
        errors.append(
            f"expected directory must contain exactly {expected_count} .expected.md files, got {len(expected_files)} in {expected_dir}"
        )

    seen_sample_ids: set[str] = set()
    manifest_image_names: set[str] = set()
    manifest_expected_paths: set[Path] = set()
    for index, sample_value in enumerate(samples, start=1):
        if not isinstance(sample_value, dict):
            errors.append(f"samples[{index}] must be a mapping")
            continue
        sample = sample_value
        prefix = f"samples[{index}]"
        sample_id = str(sample.get("sample_id", ""))
        image_file = str(sample.get("image_file", ""))
        expected_file = str(sample.get("expected_file", ""))

        if sample_id in seen_sample_ids:
            errors.append(f"{prefix}.sample_id is duplicated: {sample_id}")
        seen_sample_ids.add(sample_id)
        if not SAMPLE_ID_RE.fullmatch(sample_id):
            errors.append(f"{prefix}.sample_id must match YYYY-MM-DD_NNN_<zh|en|mixed|num>_<scenario>_<quality>: {sample_id!r}")
        image_match = IMAGE_FILE_RE.fullmatch(image_file)
        if not image_match:
            errors.append(f"{prefix}.image_file must match YYYY-MM-DD_NNN_<lang>_<scenario>_<quality>.(jpg|jpeg|png): {image_file!r}")
        elif image_match.group("sample_id") != sample_id:
            errors.append(f"{prefix}.image_file basename must equal sample_id: {image_file!r} != {sample_id!r}")
        if expected_file != f"expected/{sample_id}.expected.md":
            errors.append(f"{prefix}.expected_file must be expected/{sample_id}.expected.md, got {expected_file!r}")
        if sample.get("privacy_checked") is not True:
            errors.append(f"{prefix}.privacy_checked must be true after redaction")

        image_path = image_dir / image_file
        expected_path = _resolve_manifest_path(manifest.parent, expected_file) if expected_file else expected_dir / f"{sample_id}.expected.md"
        manifest_image_names.add(image_file)
        manifest_expected_paths.add(expected_path.resolve())
        if not image_path.is_file():
            errors.append(f"{prefix}.image_file is missing: {image_path}")
        else:
            actual_sha = _sha256_file(image_path)
            configured_sha = str(sample.get("image_sha256", "")).lower()
            if not SHA256_RE.fullmatch(configured_sha):
                errors.append(f"{prefix}.image_sha256 must be a 64-character lowercase SHA-256 hex digest")
            elif configured_sha != actual_sha:
                errors.append(f"{prefix}.image_sha256 mismatch for {image_file}: expected {configured_sha}, actual {actual_sha}")
        if not expected_path.is_file():
            errors.append(f"{prefix}.expected_file is missing: {expected_path}")
        else:
            _validate_expected_file(expected_path, sample_id, image_file, errors, warnings, prefix)

    stray_images = sorted(path.name for path in image_files if path.name not in manifest_image_names)
    if stray_images:
        errors.append(f"image directory contains files not listed in manifest: {', '.join(stray_images)}")
    stray_expected = sorted(str(path) for path in expected_files if path.resolve() not in manifest_expected_paths)
    if stray_expected:
        errors.append(f"expected directory contains files not listed in manifest: {', '.join(stray_expected)}")

    return SampleValidationResult(manifest, errors, warnings)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise RuntimeError("PyYAML is required to validate sample manifests") from exc
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded or {}


def _mapping(value: Any, name: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{name} must be a mapping")
        return {}
    return value


def _resolve_manifest_path(base: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    if value and str(value).startswith("Inbox/"):
        return (base.parent.parent / path).resolve()
    if value and str(value).startswith(".handwriting-ocr/"):
        return (base.parent.parent / path).resolve()
    return (base / path).resolve()


def _list_images(image_dir: Path, allowed_extensions: set[str]) -> list[Path]:
    return sorted(
        path
        for path in image_dir.iterdir()
        if path.is_file() and path.suffix.lower() in allowed_extensions and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_expected_file(
    expected_path: Path,
    sample_id: str,
    image_file: str,
    errors: list[str],
    warnings: list[str],
    prefix: str,
) -> None:
    text = expected_path.read_text(encoding="utf-8")
    match = EXPECTED_FRONTMATTER_RE.match(text)
    if not match:
        errors.append(f"{prefix}.expected_file must start with YAML frontmatter: {expected_path}")
        return
    try:
        frontmatter = _load_yaml_text(match.group("frontmatter"))
    except Exception as exc:
        errors.append(f"{prefix}.expected_file frontmatter could not be parsed: {expected_path}: {exc}")
        return
    if frontmatter.get("sample_id") != sample_id:
        errors.append(f"{prefix}.expected_file frontmatter sample_id must be {sample_id!r}")
    if frontmatter.get("image_file") != image_file:
        errors.append(f"{prefix}.expected_file frontmatter image_file must be {image_file!r}")
    if frontmatter.get("privacy_checked") is not True:
        errors.append(f"{prefix}.expected_file frontmatter privacy_checked must be true")
    if "expected_text:" not in text:
        warnings.append(f"{prefix}.expected_file has no expected_text section marker: {expected_path}")


def _load_yaml_text(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
        raise RuntimeError("PyYAML is required to validate expected files") from exc
    loaded = yaml.safe_load(text)
    return loaded or {}
