"""Runtime configuration for the Gemini web service, loaded from environment."""

import os
from pathlib import Path

# Load a local .env for non-Docker runs. Existing env vars win (override=False),
# so Docker Compose's injected values are never clobbered. No-op if unavailable.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


_TRUE = {"1", "true", "yes", "on"}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


class Config:
    """Service configuration sourced from environment variables.

    Gemini accounts are NO LONGER configured here — they are managed at runtime
    through the admin UI and persisted to ``accounts_path``. The env cookies are
    only read once to seed the first account when the store is empty.
    """

    def __init__(self) -> None:
        self.api_key = os.getenv("SERVICE_API_KEY", "")
        data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
        self.accounts_path = Path(
            os.getenv("ACCOUNTS_PATH", str(data_dir / "accounts.json"))
        ).resolve()
        # Runtime-tunable settings (e.g. default model) edited via the admin UI.
        self.settings_path = Path(
            os.getenv("SETTINGS_PATH", str(data_dir / "settings.json"))
        ).resolve()
        self.proxy = os.getenv("GEMINI_PROXY") or None
        self.request_timeout = float(os.getenv("REQUEST_TIMEOUT", "300"))
        # Transient buffer for streamed media (mode="stream").
        self.media_cache_dir = Path(
            os.getenv("MEDIA_CACHE_DIR", str(data_dir / "cache"))
        ).resolve()
        self.media_cache_ttl = float(os.getenv("MEDIA_CACHE_TTL", "900"))
        # Strip Gemini's visible watermark from ALL generated images regardless of
        # the per-request flag. Forces a download (url mode is upgraded to stream),
        # since the bytes must be fetched to be cleaned.
        self.force_remove_watermark = _bool_env("FORCE_REMOVE_WATERMARK", True)
        # Async generation jobs (POST /jobs): worker pool + durable state.
        # State lives in Redis when REDIS_URL is set, else a local JSON file.
        self.redis_url = os.getenv("REDIS_URL") or None
        self.jobs_path = Path(
            os.getenv("JOBS_PATH", str(data_dir / "jobs.json"))
        ).resolve()
        self.job_workers = int(os.getenv("JOB_WORKERS", "3"))
        self.job_ttl = float(os.getenv("JOB_TTL", "86400"))
        # Webhook callback delivery (optional per-job callback_url).
        self.job_callback_timeout = float(os.getenv("JOB_CALLBACK_TIMEOUT", "15"))
        self.job_callback_retries = int(os.getenv("JOB_CALLBACK_RETRIES", "3"))
        # Optional one-time seed for the first account (migration from .env).
        self.seed_1psid = os.getenv("GEMINI_SECURE_1PSID", "")
        self.seed_1psidts = os.getenv("GEMINI_SECURE_1PSIDTS", "")

    def validate(self) -> None:
        """Fail fast on missing mandatory configuration."""

        if not self.api_key:
            raise RuntimeError("Missing required env var: SERVICE_API_KEY")


config = Config()
