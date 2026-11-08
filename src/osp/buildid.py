"""Deterministic build hash (cache key + artifact suffix) and artifact naming."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Mapping

from osp.profile import Profile


def referenced_paths(profile: Profile) -> list[str]:
    paths = [f["src"] for f in profile.files] + profile.hooks
    if profile.installer_config:
        paths.append(profile.installer_config)
    return paths


def _files_under(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file())
    return [path] if path.is_file() else []


def _update_with_traversable(h, node, prefix: str) -> None:
    if node.is_dir():
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            _update_with_traversable(h, child, f"{prefix}/{child.name}")
    elif node.is_file():
        h.update(prefix.encode() + b"\0" + node.read_bytes() + b"\0")


def compute_build_hash(profile: Profile, repo_root: Path, secrets: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(profile.data, sort_keys=True).encode())
    for rel in sorted(referenced_paths(profile)):
        for f in _files_under(repo_root / rel):
            h.update(f.relative_to(repo_root).as_posix().encode() + b"\0")
            h.update(f.read_bytes() + b"\0")
    # Secrets enter as an unsalted sha256 digest. The build hash is published
    # (in the artifact name and in build.json), so a digest of a guessable raw
    # secret would be exposed. This is acceptable because today's secret fields
    # are SSH public keys and pre-salted crypt password hashes. Any future field
    # carrying a raw, guessable secret must switch to a keyed hash (HMAC).
    for ref in sorted(secrets):
        h.update(ref.encode() + b"\0" + hashlib.sha256(secrets[ref].encode()).digest())
    payload = resources.files("osp").joinpath("payload")
    if payload.is_dir():
        _update_with_traversable(h, payload, "payload")
    return h.hexdigest()


def artifact_stem(profile: Profile, build_hash: str, day: date) -> str:
    return f"{profile.name}-{profile.release}-{profile.arch}-{day:%Y%m%d}-{build_hash[:6]}"
