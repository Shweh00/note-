from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from .config import AppConfig
from .ocr import OcrResult


INVALID_FILENAME_CHARS = r'[\/\\:*?"<>|]'


def slugify(value: str) -> str:
    slug = re.sub(INVALID_FILENAME_CHARS, "-", value.strip())
    slug = re.sub(r"\s+", "-", slug).strip("-._")
    return slug or "handwriting-note"


def markdown_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_note_path(output_dir: Path, source_stem: str, created_at: datetime, source_hash: str) -> Path:
    name = f"{created_at.strftime('%Y-%m-%d-%H%M')}-{slugify(source_stem)}.md"
    candidate = output_dir / name
    if candidate.exists():
        candidate = output_dir / f"{created_at.strftime('%Y-%m-%d-%H%M')}-{slugify(source_stem)}-{source_hash[:8]}.md"
    return candidate


def render_note(
    *,
    config: AppConfig,
    title: str,
    created_at: datetime,
    source_basename: str,
    source_image_relative: str,
    image_hash: str,
    ocr_result: OcrResult,
) -> str:
    iso_created = created_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    tag_lines = "\n".join(f"  - {tag}" for tag in config.markdown.default_tags)
    text = ocr_result.text.strip() or "_未识别到文字。_"
    raw_text = ocr_result.raw_text.strip() or "(empty)"
    uncertain = "\n".join(f"- {item}" for item in ocr_result.uncertain_items) or "- 无"
    model = ocr_result.model or ""
    return (
        "---\n"
        f'title: "手写识别 - {markdown_escape(title)}"\n'
        f"created: {iso_created}\n"
        f'source_image: "{markdown_escape(source_image_relative)}"\n'
        f'source_hash: "sha256:{image_hash}"\n'
        f'ocr_mode: "{config.ocr.mode}"\n'
        f'ocr_provider: "{ocr_result.provider}"\n'
        f'ocr_model: "{markdown_escape(model)}"\n'
        f'language: "{ocr_result.language}"\n'
        'status: "to-review"\n'
        "tags:\n"
        f"{tag_lines}\n"
        "---\n\n"
        f"# 手写识别 - {title}\n\n"
        f"![[{source_image_relative}]]\n\n"
        "## 识别正文\n\n"
        f"{text}\n\n"
        "## 可能不确定的内容\n\n"
        f"{uncertain}\n\n"
        "## 原始 OCR\n\n"
        "```text\n"
        f"{raw_text}\n"
        "```\n\n"
        "## 处理信息\n\n"
        f"- 来源文件：`{source_basename}`\n"
        f"- 处理时间：`{iso_created}`\n"
        "- 处理状态：`待人工校对`\n"
    )
