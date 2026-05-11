from __future__ import annotations

from pathlib import Path

import pytest

from handwriting_obsidian.cli import main
from handwriting_obsidian.config import load_config
from handwriting_obsidian.markdown import slugify
from handwriting_obsidian.ocr import MockOcrEngine, OcrResult, create_ocr_engine
from handwriting_obsidian.processor import process_batch, retry_failed
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


def test_duplicate_content_in_new_file_is_recorded_without_new_note(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    first = tmp_path / "incoming" / "first.jpg"
    first.write_bytes(b"same bytes")
    process_batch(config, create_ocr_engine(config.ocr))

    duplicate = tmp_path / "incoming" / "duplicate.jpg"
    duplicate.write_bytes(b"same bytes")
    results = process_batch(config, create_ocr_engine(config.ocr))

    assert [result.status for result in results] == ["duplicate"]
    assert len(list(config.output_dir.glob("*.md"))) == 1
    with ProcessingState.open(config.state_path) as state:
        counts = state.counts()
    assert counts["success"] == 1
    assert counts["duplicate"] == 1


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
    assert slugify('bad/name:*? "x"') == "bad-name----x"

    bad_config = write_config(tmp_path / "bad")
    text = bad_config.read_text(encoding="utf-8").replace('provider: "mock"', 'provider: "bad"')
    bad_config.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(bad_config)
