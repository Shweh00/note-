from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import urllib.error

import pytest

from handwriting_obsidian.cli import main
from handwriting_obsidian.config import OcrConfig, _load_simple_yaml, _parse_scalar, load_config
from handwriting_obsidian.index import update_index
from handwriting_obsidian.markdown import build_note_path, render_date_folder, resolve_note_date, resolve_note_output_dir, slugify
from handwriting_obsidian.ocr import MockOcrEngine, OcrResult, create_ocr_engine
from handwriting_obsidian.processor import (
    archive_image,
    discover_images,
    process_batch,
    process_image,
    retry_failed,
    wait_until_stable,
)
from handwriting_obsidian.state import ProcessingState


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
        stdout = "hello"

    def fake_run(*_args: object, **_kwargs: object) -> Completed:
        return Completed()

    monkeypatch.setattr("handwriting_obsidian.ocr.subprocess.run", fake_run)
    tesseract = create_ocr_engine(OcrConfig(mode="offline", provider="tesseract", command="tesseract"))
    assert tesseract.recognize(image, language="eng").text == "hello"
    with pytest.raises(RuntimeError, match="Paddle"):
        create_ocr_engine(OcrConfig(mode="offline", provider="paddle"))
    with pytest.raises(ValueError):
        create_ocr_engine(OcrConfig(mode="offline", provider="other"))


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
