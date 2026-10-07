"""File-based project persistence.

Layout:
    <DATA_DIR>/projects/<project_id>/project.json
    <DATA_DIR>/projects/<project_id>/source/source<ext>
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..errors import ProjectNotFound
from ..models.project import Project

_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "projects"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- paths -----------------------------------------------------------
    def project_dir(self, project_id: str) -> Path:
        if not _ID_RE.match(project_id):  # also blocks path traversal
            raise ProjectNotFound()
        return self.root / project_id

    def source_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "source"

    def presentation_dir(self, project_id: str) -> Path:
        """Where the finished presentation file of a project is kept (STAGE 7)."""
        return self.project_dir(project_id) / "presentation"

    def video_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "video"

    def _json_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    # -- CRUD ------------------------------------------------------------
    def create(self, title: str | None = None) -> Project:
        now = utcnow()
        project = Project(
            id=uuid.uuid4().hex,
            title=(title or "").strip() or None,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self.project_dir(project.id).mkdir(parents=True, exist_ok=True)
            self._write(project)
        return project

    def get(self, project_id: str) -> Project:
        path = self._json_path(project_id)
        with self._lock:
            if not path.is_file():
                raise ProjectNotFound()
            return Project.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, project: Project) -> Project:
        project.updated_at = utcnow()
        with self._lock:
            if not self._json_path(project.id).parent.is_dir():
                raise ProjectNotFound()
            self._write(project)
            # A presentation file belongs to a stored result. Once the result is gone (the lecture
            # changed, or a new one is being generated) the old file must not linger.
            if project.presentation_result is None and project.input_mode != "deck":
                stale = self.presentation_dir(project.id)
                if stale.exists():
                    shutil.rmtree(stale, ignore_errors=True)
            if project.video_result is None:
                stale_video = self.video_dir(project.id)
                if stale_video.exists():
                    shutil.rmtree(stale_video, ignore_errors=True)
        return project

    def list_projects(self) -> list[Project]:
        projects: list[Project] = []
        with self._lock:
            for d in self.root.iterdir():
                f = d / "project.json"
                if d.is_dir() and f.is_file():
                    try:
                        projects.append(
                            Project.model_validate_json(f.read_text(encoding="utf-8"))
                        )
                    except ValueError:
                        continue  # skip corrupted entries
        projects.sort(key=lambda p: p.created_at, reverse=True)
        return projects

    def clear_source_dir(self, project_id: str) -> Path:
        d = self.source_dir(project_id)
        with self._lock:
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
        return d

    def write_artifact(self, project_id: str, name: str, model) -> Path:
        """Store a large derived JSON (e.g. the parsed SourceMaterial) next to project.json."""
        path = self.project_dir(project_id) / name
        with self._lock:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(model.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, path)
        return path

    # -- internals -------------------------------------------------------
    def _write(self, project: Project) -> None:
        path = self._json_path(project.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(project.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)
