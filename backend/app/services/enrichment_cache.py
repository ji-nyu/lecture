"""Cache of raw LLM answers so the same input never costs a second LLM call.

The cache key (built by the enricher) hashes everything that can change the answer:
source_hash, lecture_profile_hash, slide_spec_hash, the slide's full request, prompt_version,
model provider and name, enricher version. Change an option or the slide specification and
the keys change, so old entries are never used (= invalidation). Only answers that passed
validation are stored, and they are validated again on every use, so a stricter validator
also applies to cached answers.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("ailecturegen")


class EnrichmentCache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, value: dict[str, Any]) -> None: ...

    def retain(self, keys: set[str]) -> None:
        """Drop every entry that is not in `keys` (called after a run)."""


class InMemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            v = self._data.get(key)
            return json.loads(json.dumps(v)) if v is not None else None

    def set(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = json.loads(json.dumps(value))

    def retain(self, keys: set[str]) -> None:
        with self._lock:
            self._data = {k: v for k, v in self._data.items() if k in keys}

    def __len__(self) -> int:
        return len(self._data)


class JsonFileCache:
    """One JSON file per project (`enrichment_cache.json`)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
                self._data = raw if isinstance(raw, dict) else {}
            except (OSError, ValueError):
                logger.warning("Enrichment cache is unreadable; starting empty.")
                self._data = {}
        return self._data

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load().get(key)

    def set(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._load()[key] = value
            self._flush()

    def retain(self, keys: set[str]) -> None:
        with self._lock:
            data = self._load()
            kept = {k: v for k, v in data.items() if k in keys}
            if len(kept) != len(data):
                self._data = kept
                self._flush()
