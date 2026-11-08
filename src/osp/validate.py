"""Profile validation: JSON Schema first, then cross-field and filesystem rules."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from osp.backends import supported
from osp.profile import Profile, ProfileError, load_merged


def _schema(name: str) -> dict:
    return json.loads(resources.files("osp").joinpath("schema", name).read_text(encoding="utf-8"))


def _schema_errors(schema_name: str, data, prefix: str = "") -> list[str]:
    validator = Draft202012Validator(_schema(schema_name))
    out = []
    for e in sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path]):
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        out.append(f"{prefix}{loc}: {e.message}")
    return out


def load_install_config(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def validate(data: dict, repo_root: Path) -> list[str]:
    errors = _schema_errors("profile.schema.json", data)
    if errors:
        return errors
    return _semantic_errors(data, Path(repo_root))


def _semantic_errors(data: dict, repo_root: Path) -> list[str]:
    errors: list[str] = []
    distro = data["distro"]
    flavors = supported().get(distro["family"])
    if flavors is None:
        errors.append(f"distro/family: unknown backend {distro['family']!r}")
    elif distro["release"] not in flavors.get(distro["flavor"], set()):
        options = ", ".join(f"{f}/{r}" for f, rs in sorted(flavors.items()) for r in sorted(rs))
        errors.append(f"distro: unsupported {distro['flavor']}/{distro['release']} (supported: {options})")

    names = [u["name"] for u in data.get("users", [])]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        errors.append(f"users: duplicate user names {dups}")
    if data.get("live", {}).get("autologin") and not names:
        errors.append("live/autologin: requires at least one user")

    installer = data.get("installer", {"mode": "none"})
    mode, config = installer["mode"], installer.get("config")
    if mode == "calamares" and "desktop" not in data:
        errors.append("installer/mode: calamares requires a desktop layer (a profile providing 'desktop')")
    if mode == "unattended" and not config:
        errors.append("installer/config: required when mode is unattended")
    if config:
        path = repo_root / config
        if not path.is_file():
            errors.append(f"installer/config: file not found: {config}")
        else:
            cfg = load_install_config(path)
            cfg_errors = _schema_errors("install.schema.json", cfg, prefix=f"installer/config ({config}) ")
            errors += cfg_errors
            if not cfg_errors and mode == "unattended" and "target_disk" not in cfg:
                errors.append(f"installer/config ({config}): target_disk is required for unattended mode")

    for f in data.get("files", []):
        if not (repo_root / f["src"]).exists():
            errors.append(f"files: source not found: {f['src']}")
    for hook in data.get("hooks", {}).get("customize", []):
        if not (repo_root / hook).is_file():
            errors.append(f"hooks/customize: script not found: {hook}")
    return errors


def load_profile(path, repo_root) -> Profile:
    repo_root = Path(repo_root)
    data = load_merged(Path(path), repo_root / "profiles")
    errors = validate(data, repo_root)
    if errors:
        raise ProfileError(f"invalid profile {path}:\n  " + "\n  ".join(errors))
    return Profile(data=data, source=Path(path))
