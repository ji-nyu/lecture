"""PresentationProvider: the one interface between the Lecture Engine and any PPT-generating system.

The Lecture Engine (planner, slide planner, enrichment, prompt builder, presentation service)
depends only on this module. A provider talks to an external system (or pretends to, like the
mock), and is replaceable without touching the engine.

    create_presentation(request) -> ProviderStatus     start a job
    get_status(job_id)           -> ProviderStatus     poll it
    get_result(job_id)           -> ProviderResult     describe the finished file
    download(job_id)             -> DownloadedFile     the file itself

Rules for implementations:
  * Errors are raised as `PresentationProviderUnavailable` (cannot be used right now) or
    `PresentationGenerationFailed` (the job failed). Messages are user-facing Korean text and
    never contain keys, URLs with credentials, server paths or raw provider responses.
  * A provider renders; it never decides the lecture structure, importance, timing or difficulty.
    The request already contains all of that.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models.presentation import (
    DownloadedFile,
    PresentationRequest,
    ProviderResult,
    ProviderStatus,
)


@dataclass(frozen=True)
class ProviderAvailability:
    configured: bool
    message: str | None = None  # why not, when `configured` is False (user-facing)


class PresentationProvider(ABC):
    name: str  # stored with the project ("mock", "genspark", ...)
    is_mock: bool = False  # True: the output is a placeholder, never a designed presentation

    @abstractmethod
    def availability(self) -> ProviderAvailability:
        """Can this provider be used right now? Must not call the network."""

    @abstractmethod
    def create_presentation(self, request: PresentationRequest) -> ProviderStatus:
        """Start generating. Raises PresentationProviderUnavailable when not usable."""

    @abstractmethod
    def get_status(self, job_id: str) -> ProviderStatus:
        """Progress of a job created by this provider."""

    @abstractmethod
    def get_result(self, job_id: str) -> ProviderResult:
        """Only for a completed job. Raises PresentationGenerationFailed otherwise."""

    @abstractmethod
    def download(self, job_id: str) -> DownloadedFile:
        """The finished file of a completed job."""
