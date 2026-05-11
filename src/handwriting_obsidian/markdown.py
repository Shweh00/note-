from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return slug or "handwriting-note"


def markdown_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_note(
    *,
    title: str,
    created_at: datetime,
    source_image: Path,
    image_hash: str,
    tags: tuple[str, ...],
    asset_relative_path: str,
    recognized_text: str,
) -> str:
    tag_lines = "\n".join(f"  - {tag}" for tag in tags)
    processed = created_at.isoformat()
    text = recognized_text.strip() or "(No text recognized.)"
    return (
        "---\n"
        f'title: "{markdown_escape(title)}"\n'
        f'created: "{processed}"\n'
        f'source_image: "{markdown_escape(str(source_image))}"\n'
        f'image_hash: "{image_hash}"\n'
        "tags:\n"
        f"{tag_lines}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"![]({asset_relative_path})\n\n"
        "## 识别文本\n\n"
        f"{text}\n\n"
        "## 元数据\n\n"
        f"- 处理时间: {processed}\n"
        f"- 原始图片: `{source_image}`\n"
        f"- 图片哈希: `{image_hash}`\n"
    )
