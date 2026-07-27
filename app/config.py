"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

DEFAULT_UPSTREAM = "http://192.168.x.x:8000"
DEFAULT_ALIASES = {"gemma4-26b": "Gemma-4-26B-A4B"}


@dataclass
class Settings:
    upstream_base_url: str = DEFAULT_UPSTREAM
    # Used when the caller sends no Authorization header of its own.
    upstream_api_key: str | None = None
    # client-facing name -> upstream name
    model_aliases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ALIASES))
    connect_timeout: float = 10.0
    read_timeout: float = 600.0

    def resolve_model(self, name: str) -> str:
        """Map a client-facing model name to the name the upstream expects."""
        if name in self.model_aliases:
            return self.model_aliases[name]
        lowered = {k.lower(): v for k, v in self.model_aliases.items()}
        return lowered.get(name.lower(), name)


def load_settings() -> Settings:
    aliases = dict(DEFAULT_ALIASES)
    raw = os.getenv("MODEL_ALIASES")
    if raw:
        # JSON object of {"client-facing name": "upstream name"}; merged over the defaults.
        aliases.update(json.loads(raw))

    return Settings(
        upstream_base_url=os.getenv("UPSTREAM_BASE_URL", DEFAULT_UPSTREAM).rstrip("/"),
        upstream_api_key=os.getenv("UPSTREAM_API_KEY") or None,
        model_aliases=aliases,
        connect_timeout=float(os.getenv("CONNECT_TIMEOUT", "10")),
        read_timeout=float(os.getenv("READ_TIMEOUT", "600")),
    )
