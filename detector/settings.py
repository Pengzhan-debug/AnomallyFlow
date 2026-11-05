"""Environment configuration for the standalone detector service."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    profiles_path: Path
    registry_path: Path
    device: str = "cpu"
    api_key: str = ""
    stub: bool = False
    max_image_bytes: int = 20 * 1024 * 1024
    cache_ttl_seconds: int = 24 * 60 * 60
    cache_max_entries: int = 2048

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        models_root = Path(env.get("DETECTOR_MODELS_ROOT", "detector/models"))
        return cls(
            profiles_path=Path(env.get("DETECTOR_PROFILES_PATH", "data/detector_profiles.json")),
            registry_path=Path(env.get("DETECTOR_REGISTRY_PATH", models_root / "registry.json")),
            device=env.get("DETECTOR_DEVICE", "cpu"),
            api_key=env.get("DETECTOR_API_KEY", ""),
            stub=env.get("DETECTOR_STUB", "0").lower() in {"1", "true"},
            max_image_bytes=int(env.get("DETECTOR_MAX_IMAGE_BYTES", 20 * 1024 * 1024)),
            cache_ttl_seconds=int(env.get("DETECTOR_CACHE_TTL_SECONDS", 24 * 60 * 60)),
            cache_max_entries=int(env.get("DETECTOR_CACHE_MAX_ENTRIES", 2048)),
        )
