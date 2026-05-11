from __future__ import annotations

from dataclasses import dataclass
import importlib
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


LOW_CONFIDENCE_THRESHOLD = 0.80


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

    def __init__(
        self,
        command: str,
        lang: str,
        model: str | None = None,
        timeout_seconds: int = 120,
        psm: int = 6,
        oem: int = 1,
        tessdata_dir: Path | None = None,
    ) -> None:
        self.command = command
        self.lang = lang
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.psm = psm
        self.oem = oem
        self.tessdata_dir = tessdata_dir

    def recognize(self, image_path: Path, *, language: str) -> OcrResult:
        args = [
            self.command,
            str(image_path),
            "stdout",
            "-l",
            self.lang,
            "--psm",
            str(self.psm),
            "--oem",
            str(self.oem),
        ]
        if self.tessdata_dir:
            args.extend(["--tessdata-dir", str(self.tessdata_dir)])
        try:
            result = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Tesseract OCR timed out after {self.timeout_seconds}s for {image_path.name}"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(f"Tesseract OCR failed: {_tail(result.stderr)}")
        text = result.stdout.strip()
        if not text:
            raise RuntimeError("Tesseract OCR returned empty text")
        return OcrResult(
            text=text,
            raw_text=text,
            uncertain_items=(),
            provider=self.name,
            model=self.model,
            language=self.lang,
        )


class PaddleOcrEngine:
    name = "paddle"

    def __init__(self, config: OcrConfig) -> None:
        self.config = config
        self.model = config.model or "PP-OCRv5"
        if (config.offline_no_network or not config.paddle.allow_model_download) and not config.paddle.model_dir:
            raise RuntimeError("PaddleOCR model_dir is required when offline_no_network=true or downloads are disabled")
        if config.paddle.model_dir and not config.paddle.model_dir.is_dir():
            raise RuntimeError(f"PaddleOCR model_dir does not exist: {config.paddle.model_dir}")
        try:
            module = importlib.import_module("paddleocr")
        except ImportError as exc:
            raise RuntimeError(
                "PaddleOCR is not installed; install optional offline dependencies or use provider=tesseract/mock"
            ) from exc
        try:
            self._ocr = module.PaddleOCR(**self._kwargs())
        except Exception as exc:
            raise RuntimeError(f"PaddleOCR initialization failed: {_tail(str(exc))}") from exc

    def recognize(self, image_path: Path, *, language: str) -> OcrResult:
        try:
            if hasattr(self._ocr, "predict"):
                raw_result = self._ocr.predict(str(image_path))
            else:
                raw_result = self._ocr.ocr(str(image_path))
        except Exception as exc:
            raise RuntimeError(f"PaddleOCR failed for {image_path.name}: {_tail(str(exc))}") from exc
        texts, scores = _extract_paddle_texts(raw_result)
        if not texts:
            raise RuntimeError("PaddleOCR returned empty text")
        rendered: list[str] = []
        uncertain: list[str] = []
        for index, text in enumerate(texts):
            score = scores[index] if index < len(scores) else None
            item = text.strip()
            if not item:
                continue
            if score is not None and score < LOW_CONFIDENCE_THRESHOLD:
                item = f"{item} [?]"
                uncertain.append(item)
            rendered.append(item)
        output = "\n".join(rendered).strip()
        if not output:
            raise RuntimeError("PaddleOCR returned empty text")
        return OcrResult(
            text=output,
            raw_text=output,
            uncertain_items=tuple(uncertain),
            provider=self.name,
            model=self.model,
            language=self.config.paddle.lang or language,
        )

    def _kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "lang": self.config.paddle.lang,
            "device": self.config.paddle.device,
            "use_doc_orientation_classify": self.config.paddle.use_doc_orientation_classify,
            "use_doc_unwarping": self.config.paddle.use_doc_unwarping,
            "use_textline_orientation": self.config.paddle.use_textline_orientation,
        }
        if self.config.paddle.model_dir:
            kwargs["model_dir"] = str(self.config.paddle.model_dir)
        return kwargs


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
        return TesseractOcrEngine(
            config.command,
            config.tesseract.lang,
            config.model,
            config.timeout_seconds,
            config.tesseract.psm,
            config.tesseract.oem,
            config.tesseract.tessdata_dir,
        )
    if config.provider == "openai":
        return OpenAiOcrEngine(config.api_key_env, config.model, config.timeout_seconds, config.retry_count)
    if config.provider == "paddle":
        return PaddleOcrEngine(config)
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


def _tail(value: str, *, limit: int = 500) -> str:
    text = value.strip()
    return text[-limit:] if text else "no details"


def _extract_paddle_texts(raw_result: object) -> tuple[list[str], list[float]]:
    texts: list[str] = []
    scores: list[float] = []
    for item in _flatten_paddle_items(raw_result):
        data = _paddle_item_to_dict(item)
        if not data:
            continue
        rec_texts = data.get("rec_texts")
        if isinstance(rec_texts, list):
            texts.extend(str(text) for text in rec_texts if str(text).strip())
        rec_scores = data.get("rec_scores")
        if isinstance(rec_scores, list):
            for score in rec_scores:
                try:
                    scores.append(float(score))
                except (TypeError, ValueError):
                    pass
    if not texts and isinstance(raw_result, list):
        for line in raw_result:
            if isinstance(line, list):
                for candidate in line:
                    if isinstance(candidate, list) and len(candidate) >= 2 and isinstance(candidate[1], tuple):
                        texts.append(str(candidate[1][0]))
                        scores.append(float(candidate[1][1]))
    return texts, scores


def _flatten_paddle_items(value: object) -> list[object]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _paddle_item_to_dict(item: object) -> dict[str, object] | None:
    if isinstance(item, dict):
        return item
    for attr in ("json", "res"):
        value = getattr(item, attr, None)
        if isinstance(value, dict):
            return value
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, dict):
            return value
    return None
