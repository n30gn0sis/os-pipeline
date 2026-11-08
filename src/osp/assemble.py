"""Assemble stage: rootfs -> squashfs + kernel/initrd + GRUB config -> hybrid BIOS/UEFI ISO."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from osp.profile import Profile
from osp.run import Runner
from osp.stages import StageError, tool_versions

LIVE_CMDLINE = "boot=live console=tty0 console=ttyS0,115200"
SQUASHFS_EXCLUDES = ["proc/*", "sys/*", "dev/*", "run/*", "tmp/*"]


def render_grub_cfg(profile: Profile) -> str:
    entries = [("Live", ""), ("Live (safe graphics)", "nomodeset")]
    if profile.persistence:
        entries.append(("Live (persistent)", "persistence"))
    if profile.installer_mode != "none":
        entries.append(("Install", f"osp.install={profile.installer_mode}"))
    lines = [
        "set timeout=5",
        "set default=0",
        "serial --unit=0 --speed=115200",
        "terminal_input console serial",
        "terminal_output console serial",
        "",
    ]
    for title, extra in entries:
        args = f"{LIVE_CMDLINE} {extra}".strip()
        lines += [f'menuentry "{title}" {{', f"  linux /live/vmlinuz {args}", "  initrd /live/initrd.img", "}", ""]
    return "\n".join(lines)


def find_kernel(rootfs: Path) -> tuple[Path, Path]:
    kernels = sorted((Path(rootfs) / "boot").glob("vmlinuz-*"))
    if len(kernels) != 1:
        raise StageError(f"expected exactly one kernel in {rootfs}/boot, found {[k.name for k in kernels]}")
    version = kernels[0].name[len("vmlinuz-"):]
    initrd = kernels[0].parent / f"initrd.img-{version}"
    if not initrd.is_file():
        raise StageError(f"missing initrd for kernel {version}: {initrd}")
    return kernels[0], initrd


def installed_packages(rootfs: Path, runner: Runner) -> dict[str, str]:
    admindir = Path(rootfs) / "var/lib/dpkg"
    if not (admindir / "status").is_file():
        return {}
    res = runner.run(["dpkg-query", f"--admindir={rootfs}/var/lib/dpkg", "-W",
                      "--showformat=${db:Status-Status}\t${Package}\t${Version}\n"])
    packages = {}
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "installed":
            packages[parts[1]] = parts[2]
    return packages


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(profile: Profile, build_hash: str, stem: str,
                   packages: dict[str, str], tools: dict[str, str]) -> dict:
    # profile.data holds secret *references* only; resolved values never reach it.
    return {
        "artifact": f"{stem}.iso",
        "build_hash": build_hash,
        "built": datetime.now(timezone.utc).isoformat(),
        "profile": profile.data,
        "packages": packages,
        "tool_versions": tools,
    }


def assemble(profile: Profile, rootfs: Path, out_dir: Path, stem: str, build_hash: str,
             runner: Runner) -> Path:
    kernel, initrd = find_kernel(rootfs)
    iso_root = Path(out_dir) / "iso"
    live = iso_root / "live"
    live.mkdir(parents=True, exist_ok=True)
    shutil.copy2(kernel, live / "vmlinuz")
    shutil.copy2(initrd, live / "initrd.img")
    runner.run(["mksquashfs", str(rootfs), str(live / "filesystem.squashfs"),
                "-comp", "xz", "-noappend", "-wildcards", "-e", *SQUASHFS_EXCLUDES])
    grub_dir = iso_root / "boot" / "grub"
    grub_dir.mkdir(parents=True, exist_ok=True)
    (grub_dir / "grub.cfg").write_text(render_grub_cfg(profile))
    packages = installed_packages(rootfs, runner)
    iso = Path(out_dir) / f"{stem}.iso"
    runner.run(["grub-mkrescue", "-o", str(iso), str(iso_root)])
    (Path(out_dir) / f"{stem}.iso.sha256").write_text(f"{sha256_file(iso)}  {iso.name}\n")
    manifest = build_manifest(profile, build_hash, stem, packages, tool_versions())
    (Path(out_dir) / f"{stem}.build.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return iso
