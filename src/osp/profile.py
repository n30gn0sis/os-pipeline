"""Profile files: YAML loading, `extends` inheritance and the Profile model."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class ProfileError(Exception):
    pass


def _dedupe(items: list) -> list:
    out: list = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = merge(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            out[key] = _dedupe(current + value)
        else:
            out[key] = value
    return out


def load_merged(path: Path, profiles_dir: Path) -> dict:
    return _load(Path(path), Path(profiles_dir), ())


def _load(path: Path, profiles_dir: Path, chain: tuple[Path, ...]) -> dict:
    if not path.is_file():
        raise ProfileError(f"profile not found: {path}")
    key = path.resolve()
    if key in chain:
        names = " -> ".join(p.stem for p in (*chain, key))
        raise ProfileError(f"extends cycle: {names}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: top level must be a mapping")
    parents = data.pop("extends", None) or []
    if isinstance(parents, str):
        parents = [parents]
    merged: dict = {}
    for name in parents:
        merged = merge(merged, _load(profiles_dir / f"{name}.yaml", profiles_dir, (*chain, key)))
    return merge(merged, data)


@dataclass(frozen=True)
class Profile:
    data: dict
    source: Path

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def family(self) -> str:
        return self.data["distro"]["family"]

    @property
    def flavor(self) -> str:
        return self.data["distro"]["flavor"]

    @property
    def release(self) -> str:
        return self.data["distro"]["release"]

    @property
    def arch(self) -> str:
        return self.data["distro"]["arch"]

    @property
    def packages(self) -> list[str]:
        return list(self.data.get("packages", []))

    @property
    def files(self) -> list[dict]:
        return list(self.data.get("files", []))

    @property
    def users(self) -> list[dict]:
        return list(self.data.get("users", []))

    @property
    def services_enable(self) -> list[str]:
        return list(self.data.get("services", {}).get("enable", []))

    @property
    def services_disable(self) -> list[str]:
        return list(self.data.get("services", {}).get("disable", []))

    @property
    def hooks(self) -> list[str]:
        return list(self.data.get("hooks", {}).get("customize", []))

    @property
    def persistence(self) -> bool:
        return bool(self.data.get("live", {}).get("persistence", False))

    @property
    def autologin(self) -> bool:
        return bool(self.data.get("live", {}).get("autologin", False))

    @property
    def autologin_user(self) -> str | None:
        if self.autologin and self.users:
            return self.users[0]["name"]
        return None

    @property
    def installer_mode(self) -> str:
        return self.data.get("installer", {}).get("mode", "none")

    @property
    def installer_config(self) -> str | None:
        return self.data.get("installer", {}).get("config")

    @property
    def checks(self) -> list[str]:
        return list(self.data.get("test", {}).get("checks", []))

    @property
    def desktop(self) -> dict | None:
        return self.data.get("desktop")
