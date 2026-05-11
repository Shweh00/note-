from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from dataclasses import dataclass

from .config import AppConfig
from .ocr import OcrResult


INVALID_FILENAME_CHARS = r'[\/\\:*?"<>|]'
TEMPLATE_VARIABLE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")
SOURCE_NAME_DATE_PATTERNS = (
    re.compile(r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"),
    re.compile(r"(?P<year>\d{4})_(?P<month>\d{2})_(?P<day>\d{2})"),
    re.compile(r"(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"),
)


@dataclass(frozen=True)
class NoteDateResolution:
    value: datetime
    warnings: tuple[str, ...] = ()


def slugify(value: str) -> str:
    slug = re.sub(INVALID_FILENAME_CHARS, "-", value.strip())
    slug = re.sub(r"\s+", "-", slug).strip("-._")
    return slug or "handwriting-note"


def markdown_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_note_path(
    output_dir: Path,
    source_stem: str,
    created_at: datetime,
    source_hash: str,
    filename_template: str = "{{datetime}}-{{source_basename}}.md",
    note_date: datetime | None = None,
) -> Path:
    semantic_date = note_date or created_at
    iso_date = semantic_date.strftime("%Y-%m-%d")
    values = {
        "date": iso_date,
        "datetime": created_at.strftime("%Y-%m-%d-%H%M"),
        "source_basename": slugify(source_stem),
        "source_hash": source_hash,
    }
    name = filename_template
    for key, value in values.items():
        name = re.sub(r"{{\s*" + re.escape(key) + r"\s*}}", value, name)
    if "{{" in name or "}}" in name:
        name = f"{created_at.strftime('%Y-%m-%d-%H%M')}-{slugify(source_stem)}.md"
    name = slugify(Path(name).stem) + ".md"
    candidate = output_dir / name
    if candidate.exists():
        candidate = output_dir / f"{candidate.stem}-{source_hash[:8]}.md"
    return candidate


def render_date_folder(pattern: str, dt: datetime) -> Path:
    rendered = pattern.replace("YYYY", dt.strftime("%Y")).replace("MM", dt.strftime("%m")).replace("DD", dt.strftime("%d"))
    return Path(rendered)


def resolve_note_date(config: AppConfig, image_path: Path, processed_at: datetime) -> NoteDateResolution:
    source = config.markdown.date_folder.date_source
    if source == "source_mtime":
        try:
            return NoteDateResolution(datetime.fromtimestamp(image_path.stat().st_mtime, tz=timezone.utc))
        except OSError as exc:
            return NoteDateResolution(
                processed_at,
                (f"date warning: source_mtime could not be read for {image_path.name}: {exc}; using processed_at",),
            )
    if source == "source_name":
        for pattern in SOURCE_NAME_DATE_PATTERNS:
            match = pattern.search(image_path.name)
            if match:
                parts = {key: int(value) for key, value in match.groupdict().items()}
                try:
                    return NoteDateResolution(datetime(parts["year"], parts["month"], parts["day"], tzinfo=timezone.utc))
                except ValueError:
                    return NoteDateResolution(
                        processed_at,
                        (f"date warning: source_name has invalid date in {image_path.name}; using processed_at",),
                    )
        return NoteDateResolution(
            processed_at,
            (f"date warning: source_name could not find YYYY-MM-DD, YYYY_MM_DD, or YYYYMMDD in {image_path.name}; using processed_at",),
        )
    return NoteDateResolution(processed_at)


def note_date(config: AppConfig, image_path: Path, processed_at: datetime) -> datetime:
    return resolve_note_date(config, image_path, processed_at).value


def resolve_note_output_dir(config: AppConfig, image_path: Path, processed_at: datetime) -> Path:
    if not config.markdown.date_folder.enabled:
        return config.output_dir
    return config.output_dir / render_date_folder(config.markdown.date_folder.pattern, note_date(config, image_path, processed_at))


def load_template(config: AppConfig) -> tuple[str | None, list[str]]:
    if config.markdown.template.mode == "default":
        return None, []
    template_path = config.markdown.template.file_path
    if template_path is None:
        return _missing_template(config, "template file_path is empty")
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _missing_template(config, f"template file cannot be read: {exc}")
    if not template.strip():
        return _missing_template(config, "template file is empty")
    warnings = []
    if "{{recognized_markdown}}" not in template:
        warnings.append("template warning: missing {{recognized_markdown}}")
    return template, warnings


def _missing_template(config: AppConfig, message: str) -> tuple[str | None, list[str]]:
    if config.markdown.template.missing_behavior == "fail":
        raise FileNotFoundError(message)
    return None, [f"template warning: {message}; using default template"]


def render_template(template: str, context: dict[str, str]) -> tuple[str, list[str]]:
    warnings: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            warnings.append(f"template warning: unknown variable {{{{{name}}}}}")
            return match.group(0)
        return context[name]

    return TEMPLATE_VARIABLE.sub(replace, template), warnings


def build_template_context(
    *,
    config: AppConfig,
    title: str,
    created_at: datetime,
    note_date: datetime | None = None,
    source_basename: str,
    source_image_relative: str,
    image_hash: str,
    ocr_result: OcrResult,
) -> dict[str, str]:
    iso_created = created_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    semantic_date = note_date or created_at
    text = ocr_result.text.strip() or "_未识别到文字。_"
    raw_text = ocr_result.raw_text.strip() or "(empty)"
    uncertain = "\n".join(f"- {item}" for item in ocr_result.uncertain_items) or "- 无"
    processing_info = (
        f"- 来源文件：`{source_basename}`\n"
        f"- 处理时间：`{iso_created}`\n"
        "- 处理状态：`待人工校对`"
    )
    return {
        "title": f"手写识别 - {title}",
        "created_at": iso_created,
        "date": semantic_date.strftime("%Y-%m-%d"),
        "source_basename": source_basename,
        "source_image": source_image_relative,
        "source_hash": f"sha256:{image_hash}",
        "ocr_mode": config.ocr.mode,
        "ocr_provider": ocr_result.provider,
        "ocr_model": ocr_result.model or "",
        "language": ocr_result.language,
        "status": "to-review",
        "tags_yaml": "\n".join(f"  - {tag}" for tag in config.markdown.default_tags),
        "recognized_markdown": text,
        "uncertain_items": uncertain,
        "raw_ocr": raw_text,
        "processing_info": processing_info,
    }


def render_note(
    *,
    config: AppConfig,
    title: str,
    created_at: datetime,
    note_date: datetime | None = None,
    source_basename: str,
    source_image_relative: str,
    image_hash: str,
    ocr_result: OcrResult,
) -> str:
    context = build_template_context(
        config=config,
        title=title,
        created_at=created_at,
        note_date=note_date,
        source_basename=source_basename,
        source_image_relative=source_image_relative,
        image_hash=image_hash,
        ocr_result=ocr_result,
    )
    custom_template, warnings = load_template(config)
    if custom_template is not None:
        rendered, render_warnings = render_template(custom_template, context)
        for warning in (*warnings, *render_warnings):
            print(warning, flush=True)
        return rendered

    parts: list[str] = []
    if config.markdown.include_frontmatter:
        parts.extend(
            [
                "---",
                f'title: "{markdown_escape(context["title"])}"',
                f"created: {context['created_at']}",
                f'source_image: "{markdown_escape(source_image_relative)}"',
                f'source_hash: "{context["source_hash"]}"',
                f'ocr_mode: "{config.ocr.mode}"',
                f'ocr_provider: "{ocr_result.provider}"',
                f'ocr_model: "{markdown_escape(context["ocr_model"])}"',
                f'language: "{ocr_result.language}"',
                'status: "to-review"',
                "tags:",
                context["tags_yaml"],
                "---",
                "",
            ]
        )
    parts.extend([f"# {context['title']}", ""])
    if config.markdown.include_source_image:
        parts.extend([f"![[{source_image_relative}]]", ""])
    parts.extend(["## 识别正文", "", context["recognized_markdown"], "", "## 可能不确定的内容", "", context["uncertain_items"], ""])
    if config.markdown.include_raw_ocr:
        parts.extend(["## 原始 OCR", "", "```text", context["raw_ocr"], "```", ""])
    parts.extend(["## 处理信息", "", context["processing_info"], ""])
    for warning in warnings:
        print(warning, flush=True)
    return "\n".join(parts)
