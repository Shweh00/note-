from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import base64
import json
import mimetypes
import os
import subprocess
import time
import urllib.error
import urllib.request

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


class OpenAiOcrEngine:
    name = "openai"

    def __init__(self, api_key_env: str, model: str | None, timeout_seconds: int, retry_count: int) -> None:
        self.api_key_env = api_key_env
        self.model = model or "gpt-4.1-mini"
        self.timeout_seconds = timeout_seconds
        self.retry_count = retry_count

    def recognize(self, image_path: Path, *, language: str) -> OcrResult:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set; use ocr.mode: mock for no-credential runs")

        prompt = (
            "Transcribe the handwritten text in this image for Obsidian note-taking. "
            f"Prefer language(s): {language}. Preserve line breaks when useful. "
            "Return only the transcription text. If a token is uncertain, mark it with [?]."
        )
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": _image_data_url(image_path), "detail": "high"},
                    ],
                }
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response_text = self._send(request)
        text = _extract_response_text(json.loads(response_text)).strip()
        if not text:
            raise RuntimeError("OpenAI OCR returned an empty transcription")
        return OcrResult(
            text=text,
            raw_text=text,
            uncertain_items=_uncertain_items(text),
            provider=self.name,
            model=self.model,
            language=language,
        )

    def _send(self, request: urllib.request.Request) -> str:
        attempts = max(self.retry_count, 0) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                raw_body = exc.read() if exc.fp else exc.reason
                body = raw_body.decode("utf-8", errors="replace") if isinstance(raw_body, bytes) else str(raw_body)
                last_error = RuntimeError(f"OpenAI OCR failed with HTTP {exc.code}: {body}")
                if exc.code not in {408, 409, 429, 500, 502, 503, 504} or attempt == attempts - 1:
                    raise last_error
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt == attempts - 1:
                    raise RuntimeError(f"OpenAI OCR request failed: {exc}") from exc
            time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"OpenAI OCR request failed: {last_error}")  # pragma: no cover


def create_ocr_engine(config: OcrConfig) -> OcrEngine:
    if config.mode == "mock" or config.provider == "mock":
        return MockOcrEngine(config.fallback_text, config.model or "mock")
    if config.provider == "tesseract":
        return TesseractOcrEngine(config.command, config.model)
    if config.provider == "openai":
        return OpenAiOcrEngine(config.api_key_env, config.model, config.timeout_seconds, config.retry_count)
    if config.provider == "paddle":
        raise RuntimeError("Paddle OCR is not installed in this MVP. Use provider: mock or tesseract.")
    raise ValueError(f"Unsupported OCR provider: {config.provider}")


def _image_data_url(image_path: Path) -> str:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_response_text(payload: object) -> str:
    if isinstance(payload, dict):
        output_text = payload.get("output_text")
        if isinstance(output_text, str):
            return output_text
        parts: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        return "\n".join(parts)
    return ""


def _uncertain_items(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split() if "[?]" in part)
