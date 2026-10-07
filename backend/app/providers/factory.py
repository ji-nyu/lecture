"""Choose the PresentationProvider from settings (GENSPARK_MODE).

    mock      (default) MockPresentationProvider - works offline, output is a placeholder deck
    genspark  GensparkProvider - the real Genspark CLI. Without the CLI, and without either
              GENSPARK_API_KEY or a prior `gsk login`, it reports "not configured"
              (it never falls back to the mock behind the user's back)
"""

from __future__ import annotations

from pathlib import Path

from .base import PresentationProvider
from .genspark import GensparkProvider
from .mock import MockPresentationProvider

MOCK_JOBS_DIRNAME = "mock_presentations"  # under DATA_DIR
GENSPARK_JOBS_DIRNAME = "genspark_jobs"  # under DATA_DIR


def create_provider(settings) -> PresentationProvider:
    data_dir = Path(settings.data_dir)
    if getattr(settings, "genspark_mode", "mock") == "genspark":
        return GensparkProvider(
            data_dir / GENSPARK_JOBS_DIRNAME,
            api_key=getattr(settings, "genspark_api_key", None),
            api_url=getattr(settings, "genspark_api_url", None),
            cli_command=getattr(settings, "genspark_cli_path", None),
            timeout_seconds=getattr(settings, "genspark_timeout_seconds", 2100),
            poll_seconds=getattr(settings, "genspark_poll_seconds", 20),
        )
    return MockPresentationProvider(data_dir / MOCK_JOBS_DIRNAME)
