from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import os
import subprocess

from .config import OcrConfig


@dataclass(frozen=True)
class OcrResult:
    text: str
    raw_text: str
    uncertain_items: tuple[str, ...]
    provider: str
    model: str | None
    language: str


class OcrEngine(Protocol):
    name: str

    def recognize(self, image_path: Path, *, language: str) -> OcrResult:
        """Return OCR text and metadata for an image."""


class MockOcrEngine:
    name = "mock"

    def __init__(self, fallback_text: str, model: str | None = "mock") -> None:
        self.fallback_text = fallback_text
        self.model = model

    def recognize(self, image_path: Path, *, language: str) -> OcrResult:
        sidecar = image_path.with_suffix(".txt")
        text = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else self.fallback_text
        return OcrResult(
            text=text,
            raw_text=text,
            uncertain_items=(),
            provider=self.name,
            model=self.model,
            language=language,
        )


class TesseractOcrEngine:
    name = "tesseract"

    def __init__(self, command: str, model: str | None = None) -> None:
        self.command = command
        self.model = model

    def recognize(self, image_path: Path, *, language: str) -> OcrResult:
        result = subprocess.run(
            [self.command, str(image_path), "stdout", "-l", language],
            check=True,
            capture_output=True,
            text=True,
        )
        text = result.stdout.strip()
        return OcrResult(
            text=text,
            raw_text=text,
            uncertain_items=(),
            provider=self.name,
            model=self.model,
            language=language,
        )


class MissingOnlineOcrEngine:
    name = "openai"

    def __init__(self, api_key_env: str, model: str | None) -> None:
        self.api_key_env = api_key_env
        self.model = model

    def recognize(self, image_path: Path, *, language: str) -> OcrResult:
        if not os.environ.get(self.api_key_env):
            raise RuntimeError(f"{self.api_key_env} is not set; use ocr.mode: mock for no-credential runs")
        raise RuntimeError("OpenAI OCR provider is reserved for online mode; mock provider is implemented for MVP testing")


def create_ocr_engine(config: OcrConfig) -> OcrEngine:
    if config.mode == "mock" or config.provider == "mock":
        return MockOcrEngine(config.fallback_text, config.model or "mock")
    if config.provider == "tesseract":
        return TesseractOcrEngine(config.command, config.model)
    if config.provider == "openai":
        return MissingOnlineOcrEngine(config.api_key_env, config.model)
    if config.provider == "paddle":
        raise RuntimeError("Paddle OCR is not installed in this MVP. Use provider: mock or tesseract.")
    raise ValueError(f"Unsupported OCR provider: {config.provider}")
