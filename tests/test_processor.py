from __future__ import annotations

from pathlib import Path
import unittest

from handwriting_obsidian.config import AppConfig, OcrConfig
from handwriting_obsidian.ocr import MockOcrEngine
from handwriting_obsidian.processor import process_batch


class ProcessorTests(unittest.TestCase):
    def test_batch_processes_image_with_mock_ocr_and_skips_duplicate(self) -> None:
        with self.subTest("mock sidecar flow"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temp_dir:
                tmp_path = Path(temp_dir)
                incoming = tmp_path / "incoming"
                output = tmp_path / "vault" / "Handwriting Notes"
                assets = output / "assets"
                state = tmp_path / "state.json"
                incoming.mkdir()

                image = incoming / "page one.png"
                image.write_bytes(b"fake image bytes")
                image.with_suffix(".txt").write_text(
                    "第一行手写内容\n第二行整理结果", encoding="utf-8"
                )

                config = AppConfig(
                    input_dir=incoming,
                    output_dir=output,
                    assets_dir=assets,
                    state_path=state,
                    tags=("handwriting", "daily-note"),
                    ocr=OcrConfig(provider="mock"),
                )

                first_results = process_batch(config, MockOcrEngine("fallback"))

                processed = [result for result in first_results if not result.skipped]
                self.assertEqual(len(processed), 1)
                note_path = processed[0].note_path
                self.assertIsNotNone(note_path)
                assert note_path is not None
                markdown = note_path.read_text(encoding="utf-8")
                self.assertIn("第一行手写内容", markdown)
                self.assertIn("第二行整理结果", markdown)
                self.assertIn("source_image:", markdown)
                self.assertIn("image_hash:", markdown)
                self.assertIn("  - handwriting", markdown)
                self.assertIn("  - daily-note", markdown)
                self.assertIn("![](assets/page-one-", markdown)
                self.assertEqual(len(list(assets.glob("page-one-*.png"))), 1)

                second_results = process_batch(config, MockOcrEngine("fallback"))

                self.assertEqual(
                    second_results,
                    [
                        result
                        for result in second_results
                        if result.skipped and result.reason == "duplicate"
                    ],
                )
                self.assertEqual(len(list(output.glob("*.md"))), 1)

    def test_mock_ocr_uses_fallback_without_sidecar(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "no-sidecar.jpg"
            image.write_bytes(b"image")

            self.assertEqual(MockOcrEngine("fallback text").recognize(image), "fallback text")


if __name__ == "__main__":
    unittest.main()
