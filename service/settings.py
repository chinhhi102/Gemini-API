"""Runtime-tunable service settings, persisted to JSON.

Currently holds the default generation model used when a request omits one.
Kept separate from ``Config`` (boot-time env values) because these are edited
at runtime through the admin UI and must survive restarts, like accounts.json.
"""

import json
import os
from pathlib import Path

from gemini_webapi.constants import Model

DEFAULT_MODEL = "gemini-3-flash"


def available_models() -> list[str]:
    """Model names selectable as the default (excludes the UNSPECIFIED sentinel)."""

    return [m.model_name for m in Model if m is not Model.UNSPECIFIED]


class SettingsStore:
    """JSON-persisted service settings (single-process service)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict = {"default_model": DEFAULT_MODEL}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            self._data.update(json.loads(self.path.read_text() or "{}"))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, self.path)

    @property
    def default_model(self) -> str:
        return self._data.get("default_model") or DEFAULT_MODEL

    def set_default_model(self, model: str) -> str:
        if model not in available_models():
            raise ValueError(f"Unknown model: {model}")
        self._data["default_model"] = model
        self._save()
        return model

    def public_view(self) -> dict:
        """Settings payload for the API/UI: current value plus the valid choices."""

        return {
            "default_model": self.default_model,
            "available_models": available_models(),
        }
