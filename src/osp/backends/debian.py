"""Debian-family backend: bootstraps Debian and Ubuntu root filesystems with mmdebstrap."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from osp.profile import Profile
from osp.run import Runner

SUPPORTED: dict[str, set[str]] = {"debian": {"bookworm", "trixie"}, "ubuntu": {"noble"}}


@dataclass(frozen=True)
class Flavor:
    mirror: str
    security_mirror: str
    components: tuple[str, ...]
    kernel: str
    keyring: str | None


FLAVORS: dict[str, Flavor] = {
    "debian": Flavor("http://deb.debian.org/debian", "http://security.debian.org/debian-security",
                     ("main", "non-free-firmware"), "linux-image-amd64", None),
    "ubuntu": Flavor("http://archive.ubuntu.com/ubuntu", "http://security.ubuntu.com/ubuntu",
                     ("main", "universe"), "linux-image-generic",
                     "/usr/share/keyrings/ubuntu-archive-keyring.gpg"),
}

# Everything a live image needs to boot and to install itself with GRUB (BIOS + UEFI).
BASE_PACKAGES = (
    "systemd-sysv", "udev", "kmod", "initramfs-tools", "live-boot",
    "grub-pc-bin", "grub-efi-amd64-bin", "grub2-common",
    "apt", "sudo", "locales", "ca-certificates",
)


def sources(profile: Profile) -> list[str]:
    flavor = FLAVORS[profile.flavor]
    distro = profile.data["distro"]
    mirror = distro.get("mirror", flavor.mirror)
    comps = " ".join(distro.get("components", flavor.components))
    r = profile.release
    return [
        f"deb {mirror} {r} {comps}",
        f"deb {mirror} {r}-updates {comps}",
        f"deb {flavor.security_mirror} {r}-security {comps}",
    ]


def bootstrap_packages(profile: Profile) -> list[str]:
    return [*BASE_PACKAGES, FLAVORS[profile.flavor].kernel]


# This module's own source is part of the bootstrap key, so a change to the
# mmdebstrap invocation below never reuses a cache built by the old one.
_SOURCE = Path(__file__)


def bootstrap_key(profile: Profile) -> str:
    material = {
        "backend_source": hashlib.sha256(_SOURCE.read_bytes()).hexdigest(),
        "flavor": profile.flavor,
        "release": profile.release,
        "arch": profile.arch,
        "sources": sources(profile),
        "packages": sorted(bootstrap_packages(profile)),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def bootstrap(profile: Profile, rootfs: Path, runner: Runner) -> None:
    flavor = FLAVORS[profile.flavor]
    cmd = ["mmdebstrap", "--mode=root", "--variant=minbase", f"--arch={profile.arch}",
           f"--include={','.join(bootstrap_packages(profile))}"]
    if flavor.keyring:
        cmd.append(f"--keyring={flavor.keyring}")
    cmd += [profile.release, str(rootfs), *sources(profile)]
    runner.run(cmd)
