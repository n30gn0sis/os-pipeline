"""Debian-family backend: bootstraps Debian and Ubuntu root filesystems with mmdebstrap."""
from __future__ import annotations

SUPPORTED: dict[str, set[str]] = {"debian": {"bookworm", "trixie"}, "ubuntu": {"noble"}}
