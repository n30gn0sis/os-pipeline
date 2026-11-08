"""Resolve `env:`/`file:` secret references at build time. Values never go into manifests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

from osp.launcher import FILE_MAP_ENV
from osp.profile import Profile

SECRET_FIELDS = ("ssh_keys_from", "password_hash_from")


class SecretError(Exception):
    pass


def collect_refs(profile: Profile) -> list[str]:
    refs: list[str] = []
    for user in profile.users:
        for field in SECRET_FIELDS:
            ref = user.get(field)
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def env_names(refs: Iterable[str]) -> list[str]:
    return [r[len("env:"):] for r in refs if r.startswith("env:")]


def file_paths(refs: Iterable[str]) -> list[str]:
    return [r[len("file:"):] for r in refs if r.startswith("file:")]


def resolve(refs: Iterable[str], environ: Mapping[str, str],
            file_map: Mapping[str, str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    missing: list[str] = []
    for ref in refs:
        kind, _, value = ref.partition(":")
        if kind == "env":
            if environ.get(value):
                out[ref] = environ[value]
            else:
                missing.append(f"environment variable {value}")
        else:
            path = Path((file_map or {}).get(value, value)).expanduser()
            if not path.is_file():
                missing.append(f"file {value}")
                continue
            try:
                content = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                missing.append(f"file {value} (unreadable)")
                continue
            if content:
                out[ref] = content
            else:
                missing.append(f"file {value} (empty)")
    if missing:
        raise SecretError("missing secrets: " + ", ".join(missing))
    return out


def file_map_from_env(environ: Mapping[str, str]) -> dict[str, str]:
    return json.loads(environ.get(FILE_MAP_ENV, "{}"))


def secret_strings(secrets: Mapping[str, str]) -> list[str]:
    return [line.strip() for value in secrets.values() for line in value.splitlines() if line.strip()]
