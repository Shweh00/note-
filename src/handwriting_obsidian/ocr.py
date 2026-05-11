from __future__ import annotations

from pathlib import Path
from typing import Protocol
import subprocess

from .config import OcrConfig


class OcrEngine(Protocol):
    def recognize(self, image_path: Path) -> str:
        """Return recognized text for an image."""


class MockOcrEngine:
    def __init__(self, fallback_text: str) -> None:
        self.fallback_text = fallback_text

    def recognize(self, image_path: Path) -> str:
        sidecar = image_path.with_suffix(".txt")
        if sidecar.exists():
            return sidecar.read_text(encoding="utf-8").strip()
        return self.fallback_text


class TesseractOcrEngine:
    def __init__(self, command: str, languages: str) -> None:
        self.command = command
        self.languages = languages

    def recognize(self, image_path: Path) -> str:
        result = subprocess.run(
            [self.command, str(image_path), "stdout", "-l", self.languages],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()


def create_ocr_engine(config: OcrConfig) -> OcrEngine:
    if config.provider == "mock":
        return MockOcrEngine(config.fallback_text)
    if config.provider == "tesseract":
        return TesseractOcrEngine(config.command, config.languages)
    raise ValueError(f"Unsupported OCR provider: {config.provider}")
