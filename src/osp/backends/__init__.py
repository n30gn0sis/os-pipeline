"""Backend registry. A backend module exposes SUPPORTED, bootstrap_key(profile) and
bootstrap(profile, rootfs, runner)."""
from __future__ import annotations

from types import ModuleType

from osp.backends import debian

_BACKENDS: dict[str, ModuleType] = {"debian": debian}


def get_backend(family: str) -> ModuleType:
    try:
        return _BACKENDS[family]
    except KeyError:
        raise KeyError(f"unknown backend {family!r}") from None


def supported() -> dict[str, dict[str, set[str]]]:
    return {family: module.SUPPORTED for family, module in _BACKENDS.items()}
