"""Application settings. Values come from environment variables / `.env`.

No secrets are hard-coded here (DevelopmentRule_04). GENSPARK_MODE picks the presentation
provider (STAGE 7); GENSPARK_API_KEY / GENSPARK_API_URL / GENSPARK_CLI_PATH /
GENSPARK_TIMEOUT_SECONDS configure the real Genspark CLI provider (STAGE 8).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:  # pragma: no cover
    pass

logger = logging.getLogger("ailecturegen")

BACKEND_DIR = Path(__file__).resolve().parents[1]

ANALYZER_MODES = ("heuristic", "hybrid", "llm")
# Default: heuristic. It needs no key, costs nothing, is deterministic and never
# sends the lecture material to an external service. LLM analysis is opt-in.
DEFAULT_ANALYZER_MODE = "heuristic"

ENRICHMENT_PROVIDERS = ("none", "openai", "mock")
DEFAULT_ENRICHMENT_PROVIDER = "none"

# STAGE 7/8: which presentation provider runs. "mock" works offline; "genspark" drives the Genspark CLI
# and reports `not_configured` when the CLI or the key is missing.
GENSPARK_MODES = ("mock", "genspark")
DEFAULT_GENSPARK_MODE = "mock"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_upload_mb: int
    cors_origins: tuple[str, ...]
    # ---- STAGE 2.5 (all optional; defaults keep the heuristic-only behaviour)
    analyzer_mode: str = DEFAULT_ANALYZER_MODE
    llm_api_key: str | None = field(default=None, repr=False)  # never printed
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_timeout_seconds: float = 60.0
    # ---- STAGE 6A: slide content enrichment. "none" (default) = no LLM is called; every
    # slide keeps its rule-based content and says so. "openai" uses LLM_* above.
    # "mock" is a deterministic offline writer for demos/tests (never for real lectures).
    enrichment_provider: str = DEFAULT_ENRICHMENT_PROVIDER
    enrichment_batch_size: int = 5
    enrichment_workers: int = 1  # ENRICHMENT_MAX_CONCURRENCY (ENRICHMENT_WORKERS is the older name)
    enrichment_model: str | None = None  # falls back to LLM_MODEL
    # Low on purpose (reproducible, less invention). Sent only if the model accepts it: a
    # provider that rejects `temperature` is asked again without it, never guessed at.
    enrichment_temperature: float | None = 0.1
    enrichment_timeout_seconds: float = 90.0
    enrichment_max_retries: int = 2  # transient errors only (timeout / rate limit / connection / 5xx)
    enrichment_cache_enabled: bool = True
    # ---- STAGE 7: presentation provider (GENSPARK_MODE)
    genspark_mode: str = DEFAULT_GENSPARK_MODE
    # ---- STAGE 8: the Genspark CLI (`gsk`). The key is read from the environment / backend/.env
    # only, handed to the CLI through its environment (never on a command line) and never printed.
    genspark_api_key: str | None = field(default=None, repr=False)
    genspark_api_url: str | None = None  # passed as the CLI's documented --base-url
    genspark_cli_path: str | None = None  # default: `gsk` found on PATH
    genspark_timeout_seconds: int = 2100  # a slide task "usually takes minutes"; the CLI's own limit is 30 min
    genspark_poll_seconds: float = 20.0  # between two `gsk task status` calls

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def parse_analyzer_mode(value: str | None) -> str:
    mode = (value or DEFAULT_ANALYZER_MODE).strip().lower()
    if mode not in ANALYZER_MODES:
        logger.warning(
            "Unknown ANALYZER_MODE %r; using %r (allowed: %s)",
            value, DEFAULT_ANALYZER_MODE, ", ".join(ANALYZER_MODES),
        )
        return DEFAULT_ANALYZER_MODE
    return mode


def parse_enrichment_provider(value: str | None) -> str:
    provider = (value or DEFAULT_ENRICHMENT_PROVIDER).strip().lower()
    if provider not in ENRICHMENT_PROVIDERS:
        logger.warning(
            "Unknown ENRICHMENT_PROVIDER %r; using %r (allowed: %s)",
            value, DEFAULT_ENRICHMENT_PROVIDER, ", ".join(ENRICHMENT_PROVIDERS),
        )
        return DEFAULT_ENRICHMENT_PROVIDER
    return provider


def parse_genspark_mode(value: str | None) -> str:
    mode = (value or DEFAULT_GENSPARK_MODE).strip().lower()
    if mode not in GENSPARK_MODES:
        logger.warning(
            "Unknown GENSPARK_MODE %r; using %r (allowed: %s)",
            value, DEFAULT_GENSPARK_MODE, ", ".join(GENSPARK_MODES),
        )
        return DEFAULT_GENSPARK_MODE
    return mode


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("%s is not an integer; using %d", name, default)
        return default
    return min(max(value, 1), maximum)


def _env_float(name: str, default: float | None, low: float, high: float) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s is not a number; using %s", name, default)
        return default
    return min(max(value, low), high)


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    logger.warning("%s is not a boolean; using %s", name, default)
    return default


def _concurrency() -> int:
    name = "ENRICHMENT_MAX_CONCURRENCY" if os.getenv("ENRICHMENT_MAX_CONCURRENCY") else "ENRICHMENT_WORKERS"
    return _positive_int(name, 1, 8)


def load_settings() -> Settings:
    data_dir = Path(os.getenv("DATA_DIR", str(BACKEND_DIR / "data")))
    if not data_dir.is_absolute():
        data_dir = BACKEND_DIR / data_dir
    origins = os.getenv(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    )
    return Settings(
        data_dir=data_dir,
        max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "50")),
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        analyzer_mode=parse_analyzer_mode(os.getenv("ANALYZER_MODE")),
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL") or None,
        llm_base_url=os.getenv("LLM_BASE_URL") or None,
        llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
        enrichment_provider=parse_enrichment_provider(os.getenv("ENRICHMENT_PROVIDER")),
        enrichment_batch_size=_positive_int("ENRICHMENT_BATCH_SIZE", 5, 20),
        enrichment_workers=_concurrency(),
        enrichment_model=os.getenv("ENRICHMENT_MODEL") or None,
        enrichment_temperature=_env_float("ENRICHMENT_TEMPERATURE", 0.1, 0.0, 2.0),
        enrichment_timeout_seconds=_env_float("ENRICHMENT_TIMEOUT_SECONDS", 90.0, 5.0, 600.0),
        enrichment_max_retries=int(_env_float("ENRICHMENT_MAX_RETRIES", 2, 0, 5)),
        enrichment_cache_enabled=_env_bool("ENRICHMENT_CACHE_ENABLED", True),
        genspark_mode=parse_genspark_mode(os.getenv("GENSPARK_MODE")),
        genspark_api_key=os.getenv("GENSPARK_API_KEY") or None,
        genspark_api_url=os.getenv("GENSPARK_API_URL") or None,
        genspark_cli_path=os.getenv("GENSPARK_CLI_PATH") or None,
        genspark_timeout_seconds=int(_env_float("GENSPARK_TIMEOUT_SECONDS", 2100, 60, 7200)),
        genspark_poll_seconds=float(_env_float("GENSPARK_POLL_SECONDS", 20, 1, 120)),
    )
