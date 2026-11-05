"""Resolve product profiles and lazily load one runner per checkpoint."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import threading

from pydantic import ValidationError

from .runners import Runner, build_runner
from .schemas import ProfileEntry, RegistryEntry
from .settings import Settings


class ModelUnavailable(RuntimeError):
    """No validated profile/checkpoint is available for the requested product."""


class ModelStore:
    def __init__(self, settings: Settings, runner_builder=build_runner) -> None:
        self.settings = settings
        self._runner_builder = runner_builder
        self._runners: dict[tuple[str, str], Runner] = {}
        self._lock = threading.Lock()

    def profile(self, product_code: str) -> ProfileEntry:
        try:
            raw = json.loads(self.settings.profiles_path.read_text(encoding="utf-8"))
            entry = ProfileEntry.model_validate(raw[product_code])
        except (KeyError, OSError, ValueError, ValidationError) as exc:
            raise ModelUnavailable("该产品没有有效的检测配置") from exc
        if not entry.ordered():
            raise ModelUnavailable("该产品的检测阈值区间无效")
        return entry

    def runner(self, product_code: str, model_version: str) -> Runner:
        key = (product_code, model_version)
        with self._lock:
            runner = self._runners.get(key)
        if runner is not None:
            return runner
        entry = self._registry_entry(product_code)
        checkpoint = self._checkpoint_path(entry)
        with self._lock:
            if key not in self._runners:
                self._runners[key] = self._runner_builder(
                    checkpoint, self.settings.device, product_code, self.settings.stub)
            return self._runners[key]

    def deployed_products(self) -> list[str]:
        try:
            raw = json.loads(self.settings.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        products = raw.get("products", raw)
        return sorted(products) if isinstance(products, dict) else []

    def _registry_entry(self, product_code: str) -> RegistryEntry:
        try:
            raw = json.loads(self.settings.registry_path.read_text(encoding="utf-8"))
            product = raw.get("products", raw)[product_code]
            return RegistryEntry.model_validate(product)
        except (KeyError, OSError, ValueError, TypeError, ValidationError) as exc:
            raise ModelUnavailable("该产品尚未部署检测模型") from exc

    def _checkpoint_path(self, entry: RegistryEntry) -> Path:
        root = self.settings.registry_path.parent.resolve()
        checkpoint = (root / entry.checkpoint).resolve()
        if not checkpoint.is_relative_to(root) or not checkpoint.is_file():
            raise ModelUnavailable("检测模型文件不存在")
        if entry.sha256:
            hasher = sha256()
            with checkpoint.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            if hasher.hexdigest() != entry.sha256.lower():
                raise ModelUnavailable("检测模型文件校验失败")
        return checkpoint
