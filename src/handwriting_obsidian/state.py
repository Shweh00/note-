from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json


@dataclass
class ProcessingState:
    path: Path
    processed_hashes: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> "ProcessingState":
        if not path.exists():
            return cls(path=path, processed_hashes={})
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, processed_hashes=data.get("processed_hashes", {}))

    def has_hash(self, image_hash: str) -> bool:
        return image_hash in self.processed_hashes

    def mark_processed(self, image_hash: str, record: dict[str, Any]) -> None:
        self.processed_hashes[image_hash] = record

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"processed_hashes": self.processed_hashes}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
