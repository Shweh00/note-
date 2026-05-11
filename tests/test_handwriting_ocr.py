from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import os
import shlex
import subprocess
import sys
import types
import urllib.error
import xml.etree.ElementTree as ET

import pytest

from handwriting_obsidian.cli import main
from handwriting_obsidian.config import OcrConfig, PaddleConfig, TesseractConfig, _load_simple_yaml, _parse_scalar, load_config
from handwriting_obsidian.index import update_index
from handwriting_obsidian.markdown import build_note_path, render_date_folder, resolve_note_date, resolve_note_output_dir, slugify
from handwriting_obsidian.ocr import MockOcrEngine, OcrResult, create_ocr_engine, _extract_paddle_texts, _extract_response_text
from handwriting_obsidian.processor import (
    archive_image,
    discover_images,
    process_batch,
    process_image,
    retry_failed,
    wait_until_stable,
)
from handwriting_obsidian.sample_validator import validate_sample_vault
from handwriting_obsidian.state import ProcessingState


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FailingOcr:
    name = "failing"

    def recognize(self, image_path: Path, *, language: str) -> OcrResult:
        raise RuntimeError("boom")


def write_config(tmp_path: Path, *, provider: str = "mock", mode: str = "mock") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
watch:
  input_dir: "{tmp_path / 'incoming'}"
  output_dir: "{tmp_path / 'vault' / 'Inbox' / 'HandwritingNotes'}"
  processed_dir: "{tmp_path / 'incoming' / 'processed'}"
  error_dir: "{tmp_path / 'incoming' / 'error'}"
  settle_seconds: 1
  recursive: false
  polling: true

ocr:
  mode: "{mode}"
  provider: "{provider}"
  model: "mock"
  language: "zh-cn,en"
  retry_count: 1
  timeout_seconds: 5
  api_key_env: "MISSING_OPENAI_KEY"
  fallback_text: "fallback"

markdown:
  filename_template: "{{{{date}}}}-{{{{source_basename}}}}.md"
  include_frontmatter: true
  include_source_image: true
  include_raw_ocr: true
  default_tags: ["handwriting", "ocr", "to-review"]

state:
  sqlite_path: "{tmp_path / '.handwriting-ocr' / 'state.sqlite'}"
  log_path: "{tmp_path / '.handwriting-ocr' / 'handwriting-ocr.log'}"

dedupe:
  strategy: "content_hash"
  on_duplicate: "skip"

archive:
  after_success: "move"
  after_error: "move"
""",
        encoding="utf-8",
    )
    (tmp_path / "incoming").mkdir()
    return config


def paddle_model_dirs(root: Path) -> dict[str, Path]:
    det = root / "det"
    rec = root / "rec"
    det.mkdir(parents=True)
    rec.mkdir(parents=True)
    return {"text_detection_model_dir": det, "text_recognition_model_dir": rec}


def paddle_yaml_fields(dirs: dict[str, Path]) -> str:
    return (
        f'    text_detection_model_dir: "{dirs["text_detection_model_dir"]}"\n'
        f'    text_recognition_model_dir: "{dirs["text_recognition_model_dir"]}"'
    )


def test_batch_mock_ocr_generates_obsidian_markdown_and_sqlite_dedupe(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    image = tmp_path / "incoming" / "page one.png"
    image.write_bytes(b"fake image bytes")
    image.with_suffix(".txt").write_text("第一行手写内容\n第二行整理结果", encoding="utf-8")

    config = load_config(config_path)
    first = process_batch(config, MockOcrEngine("fallback"))
    second = process_batch(config, MockOcrEngine("fallback"))

    assert [result.status for result in first] == ["success"]
    assert second == []
    notes = list(config.output_dir.glob("*.md"))
    assert len(notes) == 1
    markdown = notes[0].read_text(encoding="utf-8")
    assert "title: \"手写识别 - page one\"" in markdown
    assert "source_hash: \"sha256:" in markdown
    assert "ocr_provider: \"mock\"" in markdown
    assert "  - to-review" in markdown
    assert "![[../../../incoming/processed/page one.png]]" in markdown
    assert "第一行手写内容" in markdown
    assert "## 原始 OCR" in markdown
    assert (config.processed_dir / "page one.png").exists()

    with ProcessingState.open(config.state_path) as state:
        counts = state.counts()
    assert counts["success"] == 1
    assert counts["duplicate"] == 0
    assert not (config.output_dir / "Index.md").exists()
    assert config.markdown.date_folder.enabled is False


def test_date_folder_outputs_final_path_and_relative_image(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  date_folder:\n"
            "    enabled: true\n"
            '    pattern: "YYYY/MM/DD"\n'
            '    date_source: "source_name"',
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    image = tmp_path / "incoming" / "2026-05-11 meeting.png"
    image.write_bytes(b"dated")
    image.with_suffix(".txt").write_text("日期目录内容", encoding="utf-8")

    results = process_batch(config, MockOcrEngine("fallback"))

    assert [result.status for result in results] == ["success"]
    note = results[0].note_path
    assert note is not None
    assert note.parent == config.output_dir / "2026" / "05" / "11"
    assert note.name == "2026-05-11-2026-05-11-meeting.md"
    assert "日期目录内容" in note.read_text(encoding="utf-8")
    assert "![[../../../../../../incoming/processed/2026-05-11 meeting.png]]" in note.read_text(encoding="utf-8")
    with ProcessingState.open(config.state_path) as state:
        records = state.successful_records()
    assert records[0].output_path == str(note)
    assert records[0].note_date == "2026-05-11"


def test_note_date_drives_filename_template_custom_template_and_index_group(tmp_path: Path) -> None:
    template = tmp_path / "note-template.md"
    template.write_text("created={{created_at}}\ndate={{date}}\n{{recognized_markdown}}\n", encoding="utf-8")
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  date_folder:\n"
            "    enabled: true\n"
            '    pattern: "YYYY/MM/DD"\n'
            '    date_source: "source_name"\n'
            "  template:\n"
            '    mode: "file"\n'
            f'    file_path: "{template}"\n'
            '    missing_behavior: "fallback"',
        )
        + """
index:
  enabled: true
  path: "Index.md"
  title: "手写识别索引"
  grouping: "date"
  sort: "desc"
  include_status: true
  include_source_link: false
  update_mode: "managed_block"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    image = tmp_path / "incoming" / "2024_12_31 year-end.png"
    image.write_bytes(b"semantic date")
    image.with_suffix(".txt").write_text("语义日期正文", encoding="utf-8")

    result = process_batch(config, MockOcrEngine("fallback"))[0]

    assert result.status == "success"
    assert result.note_path == config.output_dir / "2024" / "12" / "31" / "2024-12-31-2024_12_31-year-end.md"
    note_text = result.note_path.read_text(encoding="utf-8")
    assert "date=2024-12-31" in note_text
    assert "created=2024-12-31" not in note_text
    index_text = (config.output_dir / "Index.md").read_text(encoding="utf-8")
    assert "## 2024-12-31" in index_text
    assert "## 2026-" not in index_text


def test_source_name_date_warning_falls_back_to_processed_at(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  date_folder:\n"
            "    enabled: true\n"
            '    pattern: "YYYY/MM/DD"\n'
            '    date_source: "source_name"',
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    image = tmp_path / "incoming" / "2026-99-99 invalid.png"
    image.write_bytes(b"invalid date")

    result = process_batch(config, MockOcrEngine("fallback"))[0]

    assert result.status == "success"
    assert "date warning: source_name has invalid date" in capsys.readouterr().out
    assert "using processed_at" in result.reason


def test_source_mtime_date_warning_falls_back_to_processed_at(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  date_folder:\n"
            "    enabled: true\n"
            '    date_source: "source_mtime"',
        ),
        encoding="utf-8",
    )
    processed_at = datetime(2026, 5, 11, tzinfo=timezone.utc)
    resolution = resolve_note_date(load_config(config_path), tmp_path / "incoming" / "missing.png", processed_at)

    assert resolution.value == processed_at
    assert resolution.warnings
    assert "source_mtime could not be read" in resolution.warnings[0]


def test_date_folder_creation_failure_marks_failed_and_error_archives(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  date_folder:\n"
            "    enabled: true\n"
            '    pattern: "YYYY/MM/DD"\n',
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    image = tmp_path / "incoming" / "dir-fail.png"
    image.write_bytes(b"cannot create date dir")
    config.output_dir.mkdir(parents=True)
    (config.output_dir / str(datetime.now(timezone.utc).year)).write_text("not a directory", encoding="utf-8")

    results = process_batch(config, MockOcrEngine("text"))

    assert [result.status for result in results] == ["failed"]
    assert not (config.processed_dir / "dir-fail.png").exists()
    assert (config.error_dir / "dir-fail.png").exists()


def test_duplicate_content_in_new_file_is_recorded_without_new_note(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    first = tmp_path / "incoming" / "first.jpg"
    first.write_bytes(b"same bytes")
    process_batch(config, create_ocr_engine(config.ocr))

    duplicate = tmp_path / "incoming" / "duplicate.jpg"
    duplicate.write_bytes(b"same bytes")
    results = process_batch(config, create_ocr_engine(config.ocr))
    repeat = process_batch(config, create_ocr_engine(config.ocr))

    assert [result.status for result in results] == ["duplicate"]
    assert repeat == []
    assert len(list(config.output_dir.glob("*.md"))) == 1
    assert not duplicate.exists()
    assert (config.processed_dir / "duplicate.jpg").exists()
    with ProcessingState.open(config.state_path) as state:
        counts = state.counts()
    assert counts["success"] == 1
    assert counts["duplicate"] == 1


def test_custom_template_fallback_fail_and_unknown_variable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    template = tmp_path / "note-template.md"
    template.write_text(
        "# {{title}}\n{{recognized_markdown}}\nimage={{source_image}}\nraw={{raw_ocr}}\nunknown={{unknown}}\n",
        encoding="utf-8",
    )
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  template:\n"
            '    mode: "file"\n'
            f'    file_path: "{template}"\n'
            '    missing_behavior: "fallback"',
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    image = tmp_path / "incoming" / "templated.png"
    image.write_bytes(b"template")
    image.with_suffix(".txt").write_text("模板正文", encoding="utf-8")

    results = process_batch(config, MockOcrEngine("fallback"))

    assert [result.status for result in results] == ["success"]
    note = results[0].note_path
    assert note is not None
    text = note.read_text(encoding="utf-8")
    assert "# 手写识别 - templated" in text
    assert "模板正文" in text
    assert "image=../../../incoming/processed/templated.png" in text
    assert "raw=模板正文" in text
    assert "unknown={{unknown}}" in text
    assert "unknown variable {{unknown}}" in capsys.readouterr().out

    fallback_config_path = write_config(tmp_path / "fallback")
    fallback_config_path.write_text(
        fallback_config_path.read_text(encoding="utf-8").replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  template:\n"
            '    mode: "file"\n'
            f'    file_path: "{tmp_path / "missing.md"}"\n'
            '    missing_behavior: "fallback"',
        ),
        encoding="utf-8",
    )
    fallback_config = load_config(fallback_config_path)
    fallback_image = tmp_path / "fallback" / "incoming" / "fallback.png"
    fallback_image.write_bytes(b"fallback")
    assert [result.status for result in process_batch(fallback_config, MockOcrEngine("fallback text"))] == ["success"]
    assert "## 识别正文" in next(fallback_config.output_dir.glob("*.md")).read_text(encoding="utf-8")

    fail_config_path = write_config(tmp_path / "fail-template")
    fail_config_path.write_text(
        fail_config_path.read_text(encoding="utf-8").replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  template:\n"
            '    mode: "file"\n'
            f'    file_path: "{tmp_path / "missing-fail.md"}"\n'
            '    missing_behavior: "fail"',
        ),
        encoding="utf-8",
    )
    fail_config = load_config(fail_config_path)
    fail_image = tmp_path / "fail-template" / "incoming" / "fail.png"
    fail_image.write_bytes(b"fail")
    failed = process_batch(fail_config, MockOcrEngine("will fail"))
    assert [result.status for result in failed] == ["failed"]
    assert not list(fail_config.output_dir.glob("*.md"))


def test_keep_duplicate_is_not_recorded_again_on_repeated_batch(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace('on_duplicate: "skip"', 'on_duplicate: "keep"'),
        encoding="utf-8",
    )
    config = load_config(config_path)
    first = tmp_path / "incoming" / "first.jpg"
    first.write_bytes(b"same bytes")
    process_batch(config, create_ocr_engine(config.ocr))

    duplicate = tmp_path / "incoming" / "duplicate.jpg"
    duplicate.write_bytes(b"same bytes")
    first_duplicate = process_batch(config, create_ocr_engine(config.ocr))
    second_duplicate = process_batch(config, create_ocr_engine(config.ocr))

    assert [result.status for result in first_duplicate] == ["duplicate"]
    assert [result.status for result in second_duplicate] == ["duplicate"]
    assert second_duplicate[0].reason == "duplicate already recorded"
    assert duplicate.exists()
    assert not (config.processed_dir / "duplicate.jpg").exists()
    assert len(list(config.output_dir.glob("*.md"))) == 1
    with ProcessingState.open(config.state_path) as state:
        counts = state.counts()
    assert counts["success"] == 1
    assert counts["duplicate"] == 1


def test_index_managed_block_sorting_idempotency_and_duplicates(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        .replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  date_folder:\n"
            "    enabled: true\n"
            '    pattern: "YYYY/MM/DD"',
        )
        + """
index:
  enabled: true
  path: "Index.md"
  title: "手写识别索引"
  grouping: "date"
  sort: "desc"
  include_status: true
  include_source_link: true
  update_mode: "managed_block"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    index_file = config.output_dir / "Index.md"
    index_file.write_text("user intro\n<!-- handwriting-ocr:index:start -->\nold\n<!-- handwriting-ocr:index:end -->\nuser outro\n", encoding="utf-8")
    first = tmp_path / "incoming" / "first.png"
    second = tmp_path / "incoming" / "second.png"
    first.write_bytes(b"same")
    second.write_bytes(b"other")

    first_result = process_batch(config, MockOcrEngine("text"))
    duplicate = tmp_path / "incoming" / "duplicate.png"
    duplicate.write_bytes(b"same")
    duplicate_result = process_batch(config, MockOcrEngine("text"))
    second_result = process_batch(config, MockOcrEngine("text"))

    assert [result.status for result in first_result] == ["success", "success"]
    assert [result.status for result in duplicate_result] == ["duplicate"]
    assert second_result == []
    content = index_file.read_text(encoding="utf-8")
    assert content.startswith("user intro")
    assert content.rstrip().endswith("user outro")
    assert content.count("- [[") == 2
    assert content.count("first") == 3  # note target, alias, and source image
    assert "## " in content
    before = content
    with ProcessingState.open(config.state_path) as state:
        assert update_index(config, state) == []
    assert index_file.read_text(encoding="utf-8") == before


def test_index_flat_absolute_path_and_warning_on_path_conflict(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    absolute_index = tmp_path / "absolute-index.md"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f"""
index:
  enabled: true
  path: "{absolute_index}"
  title: "Flat"
  grouping: "flat"
  sort: "asc"
  include_status: false
  include_source_link: false
  update_mode: "managed_block"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    image = tmp_path / "incoming" / "absolute.png"
    image.write_bytes(b"absolute")
    result = process_batch(config, MockOcrEngine("flat"))[0]
    assert result.status == "success"
    assert absolute_index.exists()
    assert "`success`" not in absolute_index.read_text(encoding="utf-8")

    conflict_config_path = write_config(tmp_path / "index-conflict")
    conflict_config_path.write_text(
        conflict_config_path.read_text(encoding="utf-8")
        + """
index:
  enabled: true
  path: "Index.md"
""",
        encoding="utf-8",
    )
    conflict_config = load_config(conflict_config_path)
    conflict_config.output_dir.mkdir(parents=True)
    (conflict_config.output_dir / "Index.md").mkdir()
    conflict_image = tmp_path / "index-conflict" / "incoming" / "conflict.png"
    conflict_image.write_bytes(b"conflict")
    conflict = process_batch(conflict_config, MockOcrEngine("conflict"))[0]
    assert conflict.status == "success"
    assert "index warning" in conflict.reason


def test_retry_failed_processes_error_archived_file(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    image = tmp_path / "incoming" / "bad.webp"
    image.write_bytes(b"retry me")
    image.with_suffix(".txt").write_text("retry text", encoding="utf-8")

    failed = process_batch(config, FailingOcr())
    retried = retry_failed(config, MockOcrEngine("fallback"))

    assert [result.status for result in failed] == ["failed"]
    assert [result.status for result in retried] == ["success"]
    assert "fallback" in next(config.output_dir.glob("*.md")).read_text(encoding="utf-8")
    with ProcessingState.open(config.state_path) as state:
        counts = state.counts()
    assert counts["failed"] == 1
    assert counts["success"] == 1


def test_markdown_write_failure_marks_same_record_failed_before_success_archive(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    config.output_dir.parent.mkdir(parents=True, exist_ok=True)
    config.output_dir.write_text("not a directory", encoding="utf-8")
    image = tmp_path / "incoming" / "write-fails.png"
    image.write_bytes(b"image")

    results = process_batch(config, MockOcrEngine("recognized before write failure"))

    assert [result.status for result in results] == ["failed"]
    assert not (config.processed_dir / "write-fails.png").exists()
    assert (config.error_dir / "write-fails.png").exists()
    with ProcessingState.open(config.state_path) as state:
        counts = state.counts()
        failures = state.recent_failures()
    assert counts["failed"] == 1
    assert counts["processing"] == 0
    assert "File exists" in (failures[0].error_message or "")


def test_cli_status_doctor_retry_and_init(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = write_config(tmp_path)
    image = tmp_path / "incoming" / "cli.png"
    image.write_bytes(b"cli image")

    assert main(["doctor", "--config", str(config_path)]) == 0
    assert main(["batch", "--config", str(config_path)]) == 0
    assert main(["status", "--config", str(config_path)]) == 0
    assert main(["retry-failed", "--config", str(config_path)]) == 0
    output = capsys.readouterr().out
    assert "sqlite: OK" in output
    assert "done: 1 processed" in output
    assert "success: 1" in output

    vault = tmp_path / "new-vault"
    assert main(["init", "--vault", str(vault)]) == 0
    assert (vault / ".handwriting-ocr" / "config.yaml").exists()
    assert main(["init", "--vault", str(vault)]) == 2
    assert main(["init-config", "--output", str(tmp_path / "standalone.yaml")]) == 0


def test_config_validation_and_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config(tmp_path, provider="openai", mode="online")
    assert main(["doctor", "--config", str(config_path)]) == 1
    monkeypatch.setenv("MISSING_OPENAI_KEY", "present")
    assert main(["doctor", "--config", str(config_path)]) == 0
    assert slugify('bad/name:*? "x"') == "bad-name-----x"

    bad_config = write_config(tmp_path / "bad")
    text = bad_config.read_text(encoding="utf-8").replace('provider: "mock"', 'provider: "bad"')
    bad_config.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(bad_config)

    bad_dedupe = write_config(tmp_path / "bad-dedupe")
    bad_dedupe.write_text(
        bad_dedupe.read_text(encoding="utf-8").replace('on_duplicate: "skip"', 'on_duplicate: "archive"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(bad_dedupe)

    bad_date = write_config(tmp_path / "bad-date")
    bad_date.write_text(
        bad_date.read_text(encoding="utf-8").replace(
            '  default_tags: ["handwriting", "ocr", "to-review"]',
            '  default_tags: ["handwriting", "ocr", "to-review"]\n'
            "  date_folder:\n"
            "    enabled: true\n"
            '    pattern: "../YYYY"\n'
            '    date_source: "processed_at"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="safe relative"):
        load_config(bad_date)

    bad_index = write_config(tmp_path / "bad-index")
    bad_index.write_text(bad_index.read_text(encoding="utf-8") + "\nindex:\n  grouping: bad\n", encoding="utf-8")
    with pytest.raises(ValueError, match="index.grouping"):
        load_config(bad_index)

    assert render_date_folder("YYYY_MM_DD", datetime(2026, 5, 11, tzinfo=timezone.utc)) == Path("2026_05_11")
    assert resolve_note_output_dir(load_config(config_path), tmp_path / "x.png", datetime.now(timezone.utc)) == load_config(config_path).output_dir

    offline_openai = write_config(tmp_path / "offline-openai", provider="openai", mode="offline")
    with pytest.raises(ValueError, match="provider=openai"):
        load_config(offline_openai)
    assert main(["doctor", "--config", str(offline_openai)]) == 1

    offline_tesseract = write_config(tmp_path / "offline-tesseract", provider="tesseract", mode="offline")
    offline_loaded = load_config(offline_tesseract)
    assert offline_loaded.ocr.tesseract.lang == "chi_sim+eng"
    assert offline_loaded.ocr.paddle.lang == "ch"


def test_config_toml_fallback_simple_yaml_and_validation(tmp_path: Path) -> None:
    toml = tmp_path / "config.toml"
    toml.write_text(
        """
input_dir = "in"
output_dir = "out"
assets_dir = "out/assets"
state_path = "state.sqlite"
tags = ["handwriting"]
watch_interval_seconds = 1

[ocr]
provider = "mock"
fallback_text = "legacy"
""",
        encoding="utf-8",
    )
    legacy = load_config(toml)
    assert legacy.input_dir == tmp_path / "in"
    assert legacy.markdown.default_tags == ("handwriting",)
    assert legacy.state_path == tmp_path / "state.sqlite"

    simple = tmp_path / "simple.yaml"
    simple.write_text(
        """
top:
  quoted: "value"
  list: ["a", "b"]
  enabled: true
  number: 3
  ratio: 1.5
  empty:
root_value: plain
bad line
""",
        encoding="utf-8",
    )
    parsed = _load_simple_yaml(simple)
    assert parsed["top"]["quoted"] == "value"
    assert parsed["top"]["list"] == ["a", "b"]
    assert parsed["top"]["enabled"] is True
    assert parsed["top"]["number"] == 3
    assert parsed["top"]["ratio"] == 1.5
    assert parsed["top"]["empty"] == ""
    assert parsed["root_value"] == "plain"
    assert _parse_scalar("not-a-number") == "not-a-number"

    invalid = write_config(tmp_path / "invalid")
    invalid.write_text(invalid.read_text(encoding="utf-8").replace("settle_seconds: 1", "settle_seconds: 0"), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(invalid)


def test_ocr_provider_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    online = create_ocr_engine(OcrConfig(mode="online", provider="openai", api_key_env="NO_KEY", retry_count=0))
    with pytest.raises(RuntimeError, match="NO_KEY"):
        online.recognize(image, language="eng")

    monkeypatch.setenv("NO_KEY", "x")

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"output_text": "line one\nuncertain[?]"}).encode("utf-8")

    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return FakeResponse()

    monkeypatch.setattr("handwriting_obsidian.ocr.urllib.request.urlopen", fake_urlopen)
    result = online.recognize(image, language="eng")
    assert result.text == "line one\nuncertain[?]"
    assert result.uncertain_items == ("uncertain[?]",)
    assert result.provider == "openai"
    assert captured["timeout"] == 120
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")

    def fake_http_error(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.HTTPError("url", 400, "bad", {}, None)

    monkeypatch.setattr("handwriting_obsidian.ocr.urllib.request.urlopen", fake_http_error)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        online.recognize(image, language="eng")

    class Completed:
        returncode = 0
        stdout = "hello"
        stderr = ""

    def fake_run(*_args: object, **_kwargs: object) -> Completed:
        return Completed()

    monkeypatch.setattr("handwriting_obsidian.ocr.subprocess.run", fake_run)
    tesseract = create_ocr_engine(
        OcrConfig(
            mode="offline",
            provider="tesseract",
            command="tesseract",
            tesseract=TesseractConfig(lang="eng", psm=7, oem=1),
        )
    )
    assert tesseract.recognize(image, language="eng").text == "hello"
    assert tesseract.recognize(image, language="eng").language == "eng"
    with pytest.raises(RuntimeError, match="text_detection_model_dir"):
        create_ocr_engine(OcrConfig(mode="offline", provider="paddle"))
    with pytest.raises(ValueError):
        create_ocr_engine(OcrConfig(mode="offline", provider="other"))


def test_cli_rejects_offline_openai_before_any_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config(tmp_path, provider="openai", mode="offline")
    image = tmp_path / "incoming" / "private.png"
    image.write_bytes(b"private handwriting")
    monkeypatch.setenv("MISSING_OPENAI_KEY", "present")

    def forbidden_urlopen(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline OpenAI config must not upload images")

    monkeypatch.setattr("handwriting_obsidian.ocr.urllib.request.urlopen", forbidden_urlopen)

    for command in ("batch", "watch", "retry-failed"):
        assert main([command, "--config", str(config_path)]) == 1


def test_tesseract_success_doctor_failure_and_runtime_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "tesseract"
    fake.write_text(
        """#!/usr/bin/env python3
import sys
if "--list-langs" in sys.argv:
    print("List of available languages (2):")
    print("chi_sim")
    print("eng")
    raise SystemExit(0)
assert "-l" in sys.argv and "chi_sim+eng" in sys.argv
assert "--psm" in sys.argv and "--oem" in sys.argv
print("离线识别文本")
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    config_path = write_config(tmp_path / "ok", provider="tesseract", mode="offline")
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace('fallback_text: "fallback"', f'fallback_text: "fallback"\n  command: "{fake}"'),
        encoding="utf-8",
    )
    image = tmp_path / "ok" / "incoming" / "page.png"
    image.write_bytes(b"image")

    assert main(["doctor", "--config", str(config_path)]) == 0
    assert main(["batch", "--config", str(config_path)]) == 0
    note = next(load_config(config_path).output_dir.glob("*.md"))
    text = note.read_text(encoding="utf-8")
    assert "ocr_provider: \"tesseract\"" in text
    assert "离线识别文本" in text

    missing_lang = tmp_path / "missing-lang"
    missing_lang.write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('List of available languages (1):')\nprint('eng')\n",
        encoding="utf-8",
    )
    missing_lang.chmod(0o755)
    bad_config = write_config(tmp_path / "bad-lang", provider="tesseract", mode="offline")
    bad_config.write_text(
        bad_config.read_text(encoding="utf-8").replace('fallback_text: "fallback"', f'fallback_text: "fallback"\n  command: "{missing_lang}"'),
        encoding="utf-8",
    )
    assert main(["doctor", "--config", str(bad_config)]) == 1

    class Failed:
        returncode = 2
        stdout = ""
        stderr = "language data missing"

    monkeypatch.setattr("handwriting_obsidian.ocr.subprocess.run", lambda *_args, **_kwargs: Failed())
    engine = create_ocr_engine(OcrConfig(mode="offline", provider="tesseract", tesseract=TesseractConfig(lang="eng")))
    with pytest.raises(RuntimeError, match="language data missing"):
        engine.recognize(tmp_path / "x.png", language="eng")

    class Empty:
        returncode = 0
        stdout = "  "
        stderr = ""

    monkeypatch.setattr("handwriting_obsidian.ocr.subprocess.run", lambda *_args, **_kwargs: Empty())
    with pytest.raises(RuntimeError, match="empty"):
        engine.recognize(tmp_path / "x.png", language="eng")

    def timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired("tesseract", 1)

    monkeypatch.setattr("handwriting_obsidian.ocr.subprocess.run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        engine.recognize(tmp_path / "x.png", language="eng")


def test_paddle_provider_fake_success_and_doctor_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_dirs = paddle_model_dirs(tmp_path / "models")
    captured: dict[str, object] = {}

    class FakePaddleOCR:
        def __init__(
            self,
            *,
            lang: str,
            device: str,
            use_doc_orientation_classify: bool,
            use_doc_unwarping: bool,
            use_textline_orientation: bool,
            text_detection_model_dir: str,
            text_recognition_model_dir: str,
        ) -> None:
            kwargs = {
                "lang": lang,
                "device": device,
                "use_doc_orientation_classify": use_doc_orientation_classify,
                "use_doc_unwarping": use_doc_unwarping,
                "use_textline_orientation": use_textline_orientation,
                "text_detection_model_dir": text_detection_model_dir,
                "text_recognition_model_dir": text_recognition_model_dir,
            }
            captured.update(kwargs)

        def predict(self, path: str) -> list[dict[str, object]]:
            assert path.endswith("page.png")
            return [{"rec_texts": ["高置信", "低置信"], "rec_scores": [0.95, 0.5]}]

    fake_module = types.SimpleNamespace(PaddleOCR=FakePaddleOCR)
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)
    config_path = write_config(tmp_path / "paddle", provider="paddle", mode="offline")
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'fallback_text: "fallback"',
            f'''fallback_text: "fallback"
  offline_no_network: true
  paddle:
{paddle_yaml_fields(model_dirs)}
    allow_model_download: false''',
        ),
        encoding="utf-8",
    )
    image = tmp_path / "paddle" / "incoming" / "page.png"
    image.write_bytes(b"image")

    assert main(["doctor", "--config", str(config_path)]) == 0
    assert main(["batch", "--config", str(config_path)]) == 0
    assert captured["lang"] == "ch"
    assert "model_dir" not in captured
    assert captured["text_detection_model_dir"] == str(model_dirs["text_detection_model_dir"])
    assert captured["text_recognition_model_dir"] == str(model_dirs["text_recognition_model_dir"])
    note = next(load_config(config_path).output_dir.glob("*.md"))
    text = note.read_text(encoding="utf-8")
    assert "ocr_provider: \"paddle\"" in text
    assert "低置信 [?]" in text

    no_model = write_config(tmp_path / "paddle-no-model", provider="paddle", mode="offline")
    assert main(["doctor", "--config", str(no_model)]) == 1

    monkeypatch.delitem(sys.modules, "paddleocr")
    with pytest.raises(RuntimeError, match="not installed"):
        create_ocr_engine(
            OcrConfig(
                mode="offline",
                provider="paddle",
                offline_no_network=False,
                paddle=PaddleConfig(allow_model_download=True),
            )
        )


def test_paddle_error_paths_and_result_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_dirs = paddle_model_dirs(tmp_path / "models")
    missing_dir = tmp_path / "missing-models"
    with pytest.raises(RuntimeError, match="does not exist"):
        create_ocr_engine(
            OcrConfig(
                mode="offline",
                provider="paddle",
                paddle=PaddleConfig(
                    text_detection_model_dir=missing_dir,
                    text_recognition_model_dir=model_dirs["text_recognition_model_dir"],
                    allow_model_download=False,
                ),
            )
        )

    class InitFails:
        def __init__(self, **_kwargs: object) -> None:
            raise RuntimeError("init failed")

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=InitFails))
    with pytest.raises(RuntimeError, match="initialization failed"):
        create_ocr_engine(
            OcrConfig(
                mode="offline",
                provider="paddle",
                paddle=PaddleConfig(
                    text_detection_model_dir=model_dirs["text_detection_model_dir"],
                    text_recognition_model_dir=model_dirs["text_recognition_model_dir"],
                    allow_model_download=False,
                ),
            )
        )

    class OcrOnly:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def ocr(self, _path: str) -> list[list[list[object]]]:
            return [[[None, ("legacy text", 0.9)]]]

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=OcrOnly))
    engine = create_ocr_engine(
        OcrConfig(
            mode="offline",
            provider="paddle",
            paddle=PaddleConfig(
                text_detection_model_dir=model_dirs["text_detection_model_dir"],
                text_recognition_model_dir=model_dirs["text_recognition_model_dir"],
                allow_model_download=False,
                lang="",
            ),
        )
    )
    assert engine.recognize(tmp_path / "legacy.png", language="en").text == "legacy text"
    assert engine.recognize(tmp_path / "legacy.png", language="en").language == "en"

    class PredictFails:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def predict(self, _path: str) -> object:
            raise RuntimeError("predict failed")

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=PredictFails))
    engine = create_ocr_engine(
        OcrConfig(
            mode="offline",
            provider="paddle",
            paddle=PaddleConfig(
                text_detection_model_dir=model_dirs["text_detection_model_dir"],
                text_recognition_model_dir=model_dirs["text_recognition_model_dir"],
                allow_model_download=False,
            ),
        )
    )
    with pytest.raises(RuntimeError, match="predict failed"):
        engine.recognize(tmp_path / "x.png", language="zh-cn")

    class EmptyPredict:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def predict(self, _path: str) -> list[dict[str, object]]:
            return [{"rec_texts": ["  "], "rec_scores": ["bad"]}]

    monkeypatch.setitem(sys.modules, "paddleocr", types.SimpleNamespace(PaddleOCR=EmptyPredict))
    engine = create_ocr_engine(
        OcrConfig(
            mode="offline",
            provider="paddle",
            paddle=PaddleConfig(
                text_detection_model_dir=model_dirs["text_detection_model_dir"],
                text_recognition_model_dir=model_dirs["text_recognition_model_dir"],
                allow_model_download=False,
            ),
        )
    )
    with pytest.raises(RuntimeError, match="empty"):
        engine.recognize(tmp_path / "x.png", language="zh-cn")

    class JsonResult:
        json = {"rec_texts": ["json text"], "rec_scores": [0.91]}

    class ResResult:
        res = {"rec_texts": ["res text"], "rec_scores": [None]}

    class DictResult:
        def to_dict(self) -> dict[str, object]:
            return {"rec_texts": ["dict text"], "rec_scores": [0.7]}

    assert _extract_paddle_texts((JsonResult(), ResResult(), DictResult(), object()))[0] == [
        "json text",
        "res text",
        "dict text",
    ]
    assert _extract_response_text({"output": [{"content": [{"type": "text", "text": "nested"}]}]}) == "nested"
    assert _extract_response_text([]) == ""


def test_doctor_offline_edge_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_command = write_config(tmp_path / "missing-command", provider="tesseract", mode="offline")
    missing_command.write_text(
        missing_command.read_text(encoding="utf-8").replace('fallback_text: "fallback"', 'fallback_text: "fallback"\n  command: "missing-tesseract"'),
        encoding="utf-8",
    )
    assert main(["doctor", "--config", str(missing_command)]) == 1

    tessdata_config = write_config(tmp_path / "bad-tessdata", provider="tesseract", mode="offline")
    tessdata_config.write_text(
        tessdata_config.read_text(encoding="utf-8").replace(
            'fallback_text: "fallback"',
            f'fallback_text: "fallback"\n  tesseract:\n    tessdata_dir: "{tmp_path / "missing-tessdata"}"',
        ),
        encoding="utf-8",
    )
    assert main(["doctor", "--config", str(tessdata_config)]) == 1

    list_fails = tmp_path / "list-fails"
    list_fails.write_text("#!/usr/bin/env python3\nimport sys\nprint('bad', file=sys.stderr)\nraise SystemExit(2)\n", encoding="utf-8")
    list_fails.chmod(0o755)
    bad_list_config = write_config(tmp_path / "bad-list", provider="tesseract", mode="offline")
    bad_list_config.write_text(
        bad_list_config.read_text(encoding="utf-8").replace('fallback_text: "fallback"', f'fallback_text: "fallback"\n  command: "{list_fails}"'),
        encoding="utf-8",
    )
    assert main(["doctor", "--config", str(bad_list_config)]) == 1

    model_dirs = paddle_model_dirs(tmp_path / "models")
    gpu_config = write_config(tmp_path / "gpu", provider="paddle", mode="offline")
    gpu_config.write_text(
        gpu_config.read_text(encoding="utf-8").replace(
            'fallback_text: "fallback"',
            f'''fallback_text: "fallback"
  paddle:
    device: "gpu:0"
{paddle_yaml_fields(model_dirs)}''',
        ),
        encoding="utf-8",
    )
    assert main(["doctor", "--config", str(gpu_config)]) == 1

    no_import = write_config(tmp_path / "paddle-import", provider="paddle", mode="offline")
    no_import.write_text(
        no_import.read_text(encoding="utf-8").replace(
            'fallback_text: "fallback"',
            f'fallback_text: "fallback"\n  paddle:\n{paddle_yaml_fields(model_dirs)}',
        ),
        encoding="utf-8",
    )
    monkeypatch.delitem(sys.modules, "paddleocr", raising=False)
    assert main(["doctor", "--config", str(no_import)]) == 1


def test_processor_edges_and_archive_modes(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    assert discover_images(tmp_path / "missing", config.extensions) == []
    wait_until_stable(tmp_path / "missing", 0)

    txt = tmp_path / "incoming" / "note.txt"
    txt.write_text("not image", encoding="utf-8")
    with ProcessingState.open(config.state_path) as state:
        unsupported = process_image(txt, config=config, state=state, ocr_engine=MockOcrEngine("x"))
    assert unsupported.skipped

    keep_image = tmp_path / "incoming" / "keep.png"
    keep_image.write_bytes(b"keep")
    assert archive_image(keep_image, config, "keep", config.processed_dir) == keep_image
    copied = archive_image(keep_image, config, "copy", config.processed_dir)
    assert copied.exists()
    with pytest.raises(ValueError):
        archive_image(keep_image, config, "bad", config.processed_dir)

    existing_note = config.output_dir / "2026-01-01-1200-same.md"
    config.output_dir.mkdir(parents=True, exist_ok=True)
    existing_note.write_text("exists", encoding="utf-8")
    note = build_note_path(config.output_dir, "same", __import__("datetime").datetime(2026, 1, 1, 12, 0), "abcdef123")
    assert note.name.endswith("-abcdef12.md")


def test_wait_until_stable_requires_consecutive_stable_observations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = tmp_path / "incoming" / "slow.png"
    image.parent.mkdir()
    image.write_bytes(b"a")
    sleeps = {"count": 0}

    def fake_sleep(_seconds: float) -> None:
        sleeps["count"] += 1
        if sleeps["count"] == 1:
            image.write_bytes(b"ab")

    monkeypatch.setattr("handwriting_obsidian.processor.time.sleep", fake_sleep)
    wait_until_stable(image, 0.01, timeout_seconds=1, stable_checks=2)
    assert sleeps["count"] >= 3


def test_wait_until_stable_times_out_when_file_keeps_changing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = tmp_path / "incoming" / "never-stable.png"
    image.parent.mkdir()
    image.write_bytes(b"a")
    ticks = {"now": 0.0}

    def fake_monotonic() -> float:
        ticks["now"] += 0.02
        return ticks["now"]

    def fake_sleep(_seconds: float) -> None:
        image.write_bytes(image.read_bytes() + b"x")

    monkeypatch.setattr("handwriting_obsidian.processor.time.monotonic", fake_monotonic)
    monkeypatch.setattr("handwriting_obsidian.processor.time.sleep", fake_sleep)
    with pytest.raises(TimeoutError):
        wait_until_stable(image, 0.01, timeout_seconds=0.05, stable_checks=2)


def test_cli_error_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--config", str(tmp_path / "missing.yaml")]) == 1
    assert "config: FAIL" in capsys.readouterr().out

    existing = tmp_path / "exists.yaml"
    existing.write_text("x", encoding="utf-8")
    assert main(["init-config", "--output", str(existing)]) == 2

    bad_config = write_config(tmp_path / "bad-cli")
    bad_config.write_text(bad_config.read_text(encoding="utf-8").replace('provider: "mock"', 'provider: "bad"'), encoding="utf-8")
    assert main(["batch", "--config", str(bad_config)]) == 1


def test_daemon_runbook_and_templates_cover_supported_platforms() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    runbook_path = PROJECT_ROOT / "docs" / "daemon-runbook.md"
    runbook = runbook_path.read_text(encoding="utf-8")

    assert "docs/daemon-runbook.md" in readme
    for required in (
        "systemd --user",
        "launchd",
        "Task Scheduler",
        "handwriting-ocr watch",
        "handwriting-ocr doctor",
        "journalctl --user",
        "retry-failed",
        "state.log_path",
    ):
        assert required in runbook

    templates = {
        "scripts/templates/systemd/handwriting-ocr.service": ("ExecStart=", '"__CONFIG_PATH__"', "Restart=on-failure"),
        "scripts/templates/launchd/com.example.handwriting-ocr.plist": ("ProgramArguments", "__CONFIG_PATH__", "KeepAlive"),
        "scripts/templates/windows/handwriting-ocr-watch.ps1": ("handwriting_ocr watch", "__CONFIG_PATH__", "watch.err.log"),
        "scripts/templates/windows/handwriting-ocr-watch-task.xml": ("LogonTrigger", "__SCRIPT_PATH__", "powershell.exe"),
    }
    for relative_path, expected_parts in templates.items():
        content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        for expected in expected_parts:
            assert expected in content


def test_daemon_templates_and_docs_preserve_paths_with_spaces() -> None:
    project_dir = "/home/me/Obsidian Tools/handwriting ocr"
    config_path = "/home/me/Obsidian Vault/.handwriting-ocr/config.yaml"
    service = (PROJECT_ROOT / "scripts/templates/systemd/handwriting-ocr.service").read_text(encoding="utf-8")
    rendered_service = service.replace("__PROJECT_DIR__", project_dir).replace("__CONFIG_PATH__", config_path)

    assert "network-online.target" not in service
    assert f'WorkingDirectory="{project_dir}"' in rendered_service
    exec_line = next(line for line in rendered_service.splitlines() if line.startswith("ExecStart="))
    assert shlex.split(exec_line.removeprefix("ExecStart=")) == [
        f"{project_dir}/.venv/bin/python",
        "-m",
        "handwriting_ocr",
        "watch",
        "--config",
        config_path,
    ]

    task_xml = (PROJECT_ROOT / "scripts/templates/windows/handwriting-ocr-watch-task.xml").read_text(encoding="utf-8")
    script_path = r"C:\Users\me\Obsidian Tools\handwriting-ocr-watch.ps1"
    rendered_xml = task_xml.replace("__SCRIPT_PATH__", script_path).replace("__AUTHOR__", "me")
    root = ET.fromstring(rendered_xml)
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    arguments = root.findtext(".//task:Arguments", namespaces=namespace)
    assert arguments == f'-NoProfile -ExecutionPolicy Bypass -File "{script_path}"'

    runbook = (PROJECT_ROOT / "docs" / "daemon-runbook.md").read_text(encoding="utf-8")
    assert '-File `"$ScriptPath`"' in runbook
    assert "保留模板里的引号" in runbook


def write_sample_vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "sample-vault"
    image_dir = vault / "Inbox" / "HandwritingImages" / "real-samples"
    expected_dir = vault / ".handwriting-ocr" / "real-samples" / "expected"
    image_dir.mkdir(parents=True)
    expected_dir.mkdir(parents=True)
    config_path = vault / ".handwriting-ocr" / "config.yaml"
    config_path.write_text(
        f"""
watch:
  input_dir: "{image_dir}"
  output_dir: "{vault / 'Inbox' / 'HandwritingNotes'}"
  processed_dir: "{image_dir / '_processed'}"
  error_dir: "{image_dir / '_errors'}"
  settle_seconds: 1
  recursive: false
  polling: true

ocr:
  mode: "mock"
  provider: "mock"

state:
  sqlite_path: "{vault / '.handwriting-ocr' / 'state.sqlite'}"
""",
        encoding="utf-8",
    )
    samples = []
    scenarios = [
        ("zh", "meeting", "clear", "jpg"),
        ("zh", "todo", "faint", "jpg"),
        ("zh", "diary", "tilted", "jpg"),
        ("zh", "vertical", "layout", "jpg"),
        ("en", "notes", "clear", "jpg"),
        ("mixed", "bilingual", "clear", "jpg"),
        ("num", "math", "grid", "jpg"),
        ("zh", "schedule", "table", "jpg"),
        ("num", "receipt", "amounts", "jpg"),
        ("mixed", "contact", "redacted", "jpg"),
        ("zh", "mindmap", "arrows", "jpg"),
        ("mixed", "recipe", "shadow", "png"),
        ("zh", "sticky", "small", "jpg"),
        ("zh", "notes", "crowded", "jpg"),
        ("zh", "revision", "crossed", "jpg"),
        ("zh", "contrast", "faint", "jpg"),
        ("zh", "photo", "tilted", "jpg"),
        ("mixed", "pages", "marker", "jpg"),
    ]
    for number, (lang, scenario, quality, ext) in enumerate(scenarios, start=1):
        sample_id = f"2026-05-11_{number:03d}_{lang}_{scenario}_{quality}"
        image_file = f"{sample_id}.{ext}"
        image_bytes = f"fake image {number}".encode()
        (image_dir / image_file).write_bytes(image_bytes)
        digest = sha256(image_bytes).hexdigest()
        expected_file = f"expected/{sample_id}.expected.md"
        manifest_language = {
            "zh": "zh-cn",
            "en": "en",
            "mixed": "zh-cn,en",
            "num": "numbers",
        }[lang]
        (expected_dir / f"{sample_id}.expected.md").write_text(
            f"""---
sample_id: "{sample_id}"
image_file: "{image_file}"
language: "{manifest_language}"
scenario: "{scenario}"
quality_tags: ["{quality}"]
privacy_checked: true
---

## expected_text

sample {number} redacted text

## must_include

- sample

## acceptable_variants

- none

## ignore_regions

- none

## notes_for_reviewer

- test fixture
""",
            encoding="utf-8",
        )
        samples.append(
            f"""  - sample_id: "{sample_id}"
    image_file: "{image_file}"
    image_sha256: "{digest}"
    expected_file: "{expected_file}"
    language: "{manifest_language}"
    scenario: "{scenario}"
    quality_tags: ["{quality}"]
    expected_character_count: 20
    must_include: ["sample"]
    review_priority: "p1"
    privacy_checked: true
"""
        )
    manifest_path = vault / ".handwriting-ocr" / "real-samples" / "sample-manifest.yaml"
    manifest_path.write_text(
        """dataset:
  id: "real-handwriting-min18-20260511"
  version: 1
  owner: "dev"
  privacy_level: "redacted-local-only"
  created_at: "2026-05-11"
  image_dir: "Inbox/HandwritingImages/real-samples"
  expected_dir: ".handwriting-ocr/real-samples/expected"
  config_path: ".handwriting-ocr/config.yaml"

validation:
  minimum_sample_count: 18
  allowed_extensions: ["jpg", "jpeg", "png"]
  required_privacy_checked: true

samples:
"""
        + "\n".join(samples),
        encoding="utf-8",
    )
    return config_path, manifest_path


def test_validate_samples_cli_accepts_complete_real_sample_vault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, manifest_path = write_sample_vault(tmp_path)
    vault = tmp_path / "sample-vault"

    assert main(["validate-samples", "--vault", str(vault)]) == 0
    assert main(["validate-samples", "--config", str(config_path)]) == 0
    assert main(["validate-samples", "--config", str(config_path), "--manifest", str(manifest_path)]) == 0
    output = capsys.readouterr().out
    assert "sample validation: PASS" in output
    assert "samples: 18 valid, 0 failed, 0 warnings" in output

    assert main(["validate-samples", "--vault", str(vault), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["summary"]["valid_samples"] == 18


def test_validate_samples_cli_rejects_admission_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path, manifest_path = write_sample_vault(tmp_path)
    vault = tmp_path / "sample-vault"
    bad_image = vault / "Inbox" / "HandwritingImages" / "real-samples" / "2026-05-11_001_zh_meeting_clear.jpg"
    bad_image.write_bytes(b"changed")
    stray = vault / "Inbox" / "HandwritingImages" / "real-samples" / "2026-05-11_019_zh_extra_clear.jpg"
    stray.write_bytes(b"extra")
    expected = (
        vault
        / ".handwriting-ocr"
        / "real-samples"
        / "expected"
        / "2026-05-11_002_zh_todo_faint.expected.md"
    )
    expected.write_text(expected.read_text(encoding="utf-8").replace("privacy_checked: true", "privacy_checked: false"), encoding="utf-8")
    (
        vault
        / ".handwriting-ocr"
        / "real-samples"
        / "expected"
        / "2026-05-11_019_zh_extra_clear.expected.md"
    ).write_text("---\nprivacy_checked: true\n---\n", encoding="utf-8")
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest_text = manifest_text.replace('privacy_checked: true', 'privacy_checked: false', 1)
    manifest_text = manifest_text.replace('image_file: "2026-05-11_003_zh_diary_tilted.jpg"', 'image_file: "bad-name.jpg"', 1)
    manifest_path.write_text(manifest_text, encoding="utf-8")

    assert main(["validate-samples", "--vault", str(vault), "--manifest", str(manifest_path)]) == 1
    output = capsys.readouterr().out
    assert "sample validation: FAIL" in output
    assert "FAIL SAMPLE_PRIVACY_NOT_CHECKED:" in output
    assert "FAIL SAMPLE_HASH_MISMATCH:" in output
    assert "FAIL SAMPLE_FILENAME_INVALID:" in output
    assert "expected frontmatter privacy_checked must be true" in output
    assert "FAIL EXTRA_IMAGE_NOT_IN_MANIFEST:" in output
    assert "FAIL EXTRA_EXPECTED_NOT_IN_MANIFEST:" in output


def test_validate_samples_cli_rejects_invalid_sample_id_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _config_path, manifest_path = write_sample_vault(tmp_path)
    vault = tmp_path / "sample-vault"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            'sample_id: "2026-05-11_001_zh_meeting_clear"',
            'sample_id: "bad-sample-id"',
            1,
        ),
        encoding="utf-8",
    )

    assert main(["validate-samples", "--vault", str(vault), "--manifest", str(manifest_path)]) == 1
    captured = capsys.readouterr()
    stdout = captured.out
    stderr = captured.err

    assert "sample validation: FAIL" in stdout
    assert "FAIL SAMPLE_ID_INVALID:" in stdout
    assert "Traceback" not in stdout
    assert "Traceback" not in stderr


def test_validate_samples_cli_rejects_template_manifest_and_empty_real_samples(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "template-vault"
    image_dir = vault / "Inbox" / "HandwritingImages" / "real-samples"
    expected_dir = vault / ".handwriting-ocr" / "real-samples" / "expected"
    image_dir.mkdir(parents=True)
    expected_dir.mkdir(parents=True)
    config_path = vault / ".handwriting-ocr" / "config.yaml"
    config_path.write_text(
        f"""
watch:
  input_dir: "{vault / 'Inbox' / 'HandwritingImages'}"
  output_dir: "{vault / 'Inbox' / 'HandwritingNotes'}"
""",
        encoding="utf-8",
    )
    first_id = "2026-05-11_001_zh_meeting_clear"
    (expected_dir / f"{first_id}.expected.md").write_text(
        f"""---
sample_id: "{first_id}"
image_file: "{first_id}.jpg"
language: "zh-cn"
scenario: "meeting"
privacy_checked: false
---

## expected_text

在这里填写人工转写文本

## must_include

- meeting

## acceptable_variants

- none

## ignore_regions

- none

## notes_for_reviewer

- template
""",
        encoding="utf-8",
    )
    samples = []
    for number, (lang, scenario, quality, _ext) in enumerate(
        [
            ("zh", "meeting", "clear", "jpg"),
            ("zh", "todo", "faint", "jpg"),
            ("zh", "diary", "tilted", "jpg"),
            ("zh", "vertical", "layout", "jpg"),
            ("en", "notes", "clear", "jpg"),
            ("mixed", "bilingual", "clear", "jpg"),
            ("num", "math", "grid", "jpg"),
            ("zh", "schedule", "table", "jpg"),
            ("num", "receipt", "amounts", "jpg"),
            ("mixed", "contact", "redacted", "jpg"),
            ("zh", "mindmap", "arrows", "jpg"),
            ("mixed", "recipe", "shadow", "png"),
            ("zh", "sticky", "small", "jpg"),
            ("zh", "notes", "crowded", "jpg"),
            ("zh", "revision", "crossed", "jpg"),
            ("zh", "contrast", "faint", "jpg"),
            ("zh", "photo", "tilted", "jpg"),
            ("mixed", "pages", "marker", "jpg"),
        ],
        start=1,
    ):
        sample_id = f"2026-05-11_{number:03d}_{lang}_{scenario}_{quality}"
        language = {"zh": "zh-cn", "en": "en", "mixed": "zh-cn,en", "num": "numbers"}[lang]
        samples.append(
            f"""  - sample_id: "{sample_id}"
    image_file: "{sample_id}.jpg"
    image_sha256: "TODO"
    expected_file: "expected/{sample_id}.expected.md"
    language: "{language}"
    scenario: "{scenario}"
    quality_tags: ["{quality}"]
    expected_character_count: 20
    must_include: ["meeting"]
    review_priority: "p1"
    privacy_checked: false
"""
        )
    (vault / ".handwriting-ocr" / "real-samples" / "sample-manifest.yaml").write_text(
        """dataset:
  id: "real-handwriting-min18-20260511"
  version: 1
  owner: "dev"
  privacy_level: "redacted-local-only"
  created_at: "2026-05-11"
  image_dir: "Inbox/HandwritingImages/real-samples"
  expected_dir: ".handwriting-ocr/real-samples/expected"
  config_path: ".handwriting-ocr/config.yaml"

validation:
  minimum_sample_count: 18

samples:
"""
        + "\n".join(samples),
        encoding="utf-8",
    )

    assert main(["validate-samples", "--vault", str(vault)]) == 1
    output = capsys.readouterr().out

    assert "FAIL SAMPLE_COUNT_TOO_LOW: image count=0 required>=18" in output
    assert "FAIL EXPECTED_COUNT_TOO_LOW: expected files=1 required>=18" in output
    assert "FAIL CONFIG_WATCH_INPUT_DIR_INVALID:" in output
    assert "FAIL SAMPLE_HASH_MISSING:" in output
    assert "FAIL SAMPLE_MANIFEST_TEMPLATE_VALUE:" in output
    assert "FAIL SAMPLE_PRIVACY_NOT_CHECKED:" in output
    assert "FAIL EXPECTED_FILE_MISSING:" in output
    assert "FAIL EXPECTED_TEXT_PLACEHOLDER:" in output


def test_validate_sample_vault_handles_missing_and_malformed_manifest(tmp_path: Path) -> None:
    missing = validate_sample_vault(tmp_path, manifest_path=tmp_path / "missing.yaml")
    assert not missing.ok
    assert missing.failures[0].code == "SAMPLE_MANIFEST_MISSING"

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("- not a mapping\n", encoding="utf-8")
    result = validate_sample_vault(tmp_path, manifest_path=malformed)
    assert not result.ok
    assert any(issue.code == "SAMPLE_MANIFEST_SCHEMA_INVALID" for issue in result.failures)

    invalid_yaml = tmp_path / "invalid.yaml"
    invalid_yaml.write_text("dataset: [\n", encoding="utf-8")
    result = validate_sample_vault(tmp_path, manifest_path=invalid_yaml)
    assert not result.ok
    assert any(issue.code == "SAMPLE_MANIFEST_INVALID_YAML" for issue in result.failures)


def test_validate_sample_vault_reports_manifest_shape_errors(tmp_path: Path) -> None:
    manifest = tmp_path / "shape.yaml"
    manifest.write_text(
        """
dataset: "bad"
validation:
  minimum_sample_count: 18
  allowed_extensions: ["gif"]
samples: "bad"
""",
        encoding="utf-8",
    )

    result = validate_sample_vault(tmp_path, manifest_path=manifest)

    assert not result.ok
    assert any(issue.message == "dataset must be a mapping" for issue in result.failures)
    assert any(issue.message == "samples must be a non-empty list" for issue in result.failures)
    assert any(issue.code == "SAMPLE_COUNT_TOO_LOW" for issue in result.failures)
    assert any(issue.code == "CONFIG_FILE_MISSING" for issue in result.failures)
    assert any(issue.code == "SAMPLE_EXPECTED_DIR_MISSING" for issue in result.failures)


def test_validate_sample_vault_reports_sample_field_errors_and_expected_warnings(tmp_path: Path) -> None:
    image_dir = tmp_path / "images"
    expected_dir = tmp_path / "expected"
    image_dir.mkdir()
    expected_dir.mkdir()
    sample_id = "2026-05-11_001_zh_meeting_clear"
    image_file = f"{sample_id}.jpg"
    (image_dir / image_file).write_bytes(b"image")
    (expected_dir / f"{sample_id}.expected.md").write_text(
        f"""---
sample_id: "wrong"
image_file: "{image_file}"
privacy_checked: true
---
""",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"""
dataset:
  image_dir: "{image_dir}"
  expected_dir: "{expected_dir}"
validation:
  minimum_sample_count: 2
  allowed_extensions: ["jpg"]
samples:
  - "not a mapping"
  - sample_id: "{sample_id}"
    image_file: "2026-05-11_001_zh_other_clear.jpg"
    image_sha256: "bad"
    expected_file: "expected/wrong.expected.md"
    privacy_checked: true
""",
        encoding="utf-8",
    )
    result = validate_sample_vault(tmp_path, manifest_path=manifest, image_dir=image_dir, expected_dir=expected_dir, min_count=2)

    assert not result.ok
    assert any(issue.message == "samples[1] must be a mapping" for issue in result.failures)
    assert any(issue.code == "SAMPLE_ID_MISMATCH" for issue in result.failures)
    assert any(issue.code == "SAMPLE_IMAGE_MISSING" for issue in result.failures)
    assert any(issue.code == "EXPECTED_FILE_MISSING" for issue in result.failures)

    manifest.write_text(
        f"""
dataset:
  image_dir: "{image_dir}"
  expected_dir: "{expected_dir}"
validation:
  minimum_sample_count: 1
  allowed_extensions: ["jpg"]
samples:
  - sample_id: "{sample_id}"
    image_file: "{image_file}"
    image_sha256: "bad"
    expected_file: "{expected_dir / f'{sample_id}.expected.md'}"
    privacy_checked: true
""",
        encoding="utf-8",
    )

    result = validate_sample_vault(tmp_path, manifest_path=manifest, image_dir=image_dir, expected_dir=expected_dir, min_count=1)

    assert not result.ok
    assert any(issue.code == "SAMPLE_HASH_MISSING" for issue in result.failures)
    assert any(issue.code == "EXPECTED_METADATA_MISMATCH" for issue in result.failures)
    assert any(issue.code == "EXPECTED_TEXT_PLACEHOLDER" for issue in result.failures)
