from __future__ import annotations

from pathlib import Path

import pytest

from handwriting_obsidian.cli import main
from handwriting_obsidian.config import OcrConfig, _load_simple_yaml, _parse_scalar, load_config
from handwriting_obsidian.markdown import build_note_path, slugify
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
    assert slugify('bad/name:*? "x"') == "bad-name-----x"

    bad_config = write_config(tmp_path / "bad")
    text = bad_config.read_text(encoding="utf-8").replace('provider: "mock"', 'provider: "bad"')
    bad_config.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(bad_config)


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
    online = create_ocr_engine(OcrConfig(mode="online", provider="openai", api_key_env="NO_KEY"))
    with pytest.raises(RuntimeError, match="NO_KEY"):
        online.recognize(image, language="eng")
    monkeypatch.setenv("NO_KEY", "x")
    with pytest.raises(RuntimeError, match="reserved"):
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


def test_cli_error_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--config", str(tmp_path / "missing.yaml")]) == 1
    assert "config: FAIL" in capsys.readouterr().out

    existing = tmp_path / "exists.yaml"
    existing.write_text("x", encoding="utf-8")
    assert main(["init-config", "--output", str(existing)]) == 2

    bad_config = write_config(tmp_path / "bad-cli")
    bad_config.write_text(bad_config.read_text(encoding="utf-8").replace('provider: "mock"', 'provider: "bad"'), encoding="utf-8")
    assert main(["batch", "--config", str(bad_config)]) == 1
