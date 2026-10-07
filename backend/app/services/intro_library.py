"""Shared folder of lecture-video intro clips (`<DATA_DIR>/intros`)."""

from __future__ import annotations

import os
import re
from hashlib import sha256
from pathlib import Path

INTRO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def intros_dir(data_dir: Path) -> Path:
    path = Path(data_dir) / "intros"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_intro_name(name: str | None) -> str | None:
    if not name or not str(name).strip():
        return None
    base = Path(str(name).replace("\\", "/")).name.strip()
    if not base or base in {".", ".."}:
        return None
    stem = _UNSAFE.sub("_", Path(base).stem).strip(" .") or "intro"
    suffix = Path(base).suffix.lower()
    if suffix not in INTRO_EXTENSIONS:
        return None
    return f"{stem}{suffix}"


def list_intros(data_dir: Path) -> list[dict]:
    folder = intros_dir(data_dir)
    items: list[dict] = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in INTRO_EXTENSIONS:
            continue
        items.append(
            {
                "filename": path.name,
                "name": path.stem,
                "size_bytes": path.stat().st_size,
            }
        )
    return items


def resolve_intro(data_dir: Path, name: str | None) -> Path | None:
    safe = safe_intro_name(name)
    if not safe:
        return None
    path = intros_dir(data_dir) / safe
    return path if path.is_file() else None


def save_intro(data_dir: Path, filename: str, data: bytes) -> str:
    safe = safe_intro_name(filename)
    if not safe:
        raise ValueError("지원하지 않는 인트로 영상 형식입니다. mp4, mov, webm, mkv 파일을 올려 주세요.")
    folder = intros_dir(data_dir)
    stem, suffix = Path(safe).stem, Path(safe).suffix
    candidate = safe
    n = 2
    while (folder / candidate).exists():
        candidate = f"{stem}-{n}{suffix}"
        n += 1
    tmp = folder / (candidate + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, folder / candidate)
    return candidate


def intro_digest(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return f"{path.name}:{path.stat().st_size if path.exists() else 0}"
