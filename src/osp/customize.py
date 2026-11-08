"""Customize stage: turn a bootstrapped rootfs into the profile's live system."""
from __future__ import annotations

import os
import shlex
import shutil
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Mapping

from osp.mounts import chroot_mounts
from osp.profile import Profile
from osp.run import CommandError, Runner
from osp.stages import StageError
from osp.validate import load_install_config

_INSTALL_TOOLS = ["gdisk", "dosfstools", "e2fsprogs", "squashfs-tools", "util-linux"]
INSTALLER_PACKAGES: dict[str, list[str]] = {
    "none": [],
    "unattended": _INSTALL_TOOLS,
    "tui": [*_INSTALL_TOOLS, "whiptail"],
    "calamares": ["calamares", "squashfs-tools", "dosfstools", "e2fsprogs", "efibootmgr", "pkexec"],
}
LIVE_PACKAGES = ["live-boot", "live-boot-initramfs-tools"]
UNITS: dict[str, list[str]] = {
    "none": ["osp-ready.service", "osp-ssh-hostkeys.service"],
    "unattended": ["osp-ready.service", "osp-ssh-hostkeys.service", "osp-installer.service"],
    "tui": ["osp-ready.service", "osp-ssh-hostkeys.service", "osp-installer-tui.service"],
    "calamares": ["osp-ready.service", "osp-ssh-hostkeys.service"],
}
# (payload path, destination, mode, installer modes or None for all)
PAYLOAD_FILES = [
    ("osp-ready", "/usr/local/lib/osp/osp-ready", 0o755, None),
    ("osp-ready.service", "/etc/systemd/system/osp-ready.service", 0o644, None),
    ("osp-ssh-hostkeys.service", "/etc/systemd/system/osp-ssh-hostkeys.service", 0o644, None),
    ("grub-serial.cfg", "/etc/default/grub.d/osp-serial.cfg", 0o644, None),
    ("install-to-disk", "/usr/local/sbin/install-to-disk", 0o755, {"unattended", "tui"}),
    ("osp-installer.service", "/etc/systemd/system/osp-installer.service", 0o644, {"unattended"}),
    ("osp-installer-tui.service", "/etc/systemd/system/osp-installer-tui.service", 0o644, {"tui"}),
    ("osp-calamares.desktop", "/etc/xdg/autostart/osp-calamares.desktop", 0o644, {"calamares"}),
    ("49-osp-live.rules", "/etc/polkit-1/rules.d/49-osp-live.rules", 0o644, {"calamares"}),
    ("calamares", "/etc/calamares", 0o644, {"calamares"}),
]


def chroot(runner: Runner, rootfs: Path, *cmd: str, input: str | None = None):
    return runner.run(["chroot", str(rootfs), *cmd], input=input)


def _inside(rootfs: Path, path: Path) -> bool:
    root, resolved = Path(rootfs).resolve(), Path(path).resolve()
    return resolved == root or root in resolved.parents


def rootfs_path(rootfs: Path, dest: str) -> Path:
    target = Path(rootfs) / dest.lstrip("/")
    if not _inside(rootfs, target.parent):
        raise StageError(f"refusing to write outside the rootfs: {dest}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    return target


def install_packages(profile: Profile, rootfs: Path, runner: Runner) -> None:
    packages = list(dict.fromkeys([*profile.packages, *INSTALLER_PACKAGES[profile.installer_mode]]))
    if not packages:
        return
    chroot(runner, rootfs, "apt-get", "update")
    chroot(runner, rootfs, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
           "--no-install-recommends", *packages)


def create_users(profile: Profile, secrets: Mapping[str, str], rootfs: Path, runner: Runner) -> None:
    for user in profile.users:
        name = user["name"]
        cmd = ["useradd", "--create-home", "--shell", user.get("shell", "/bin/bash")]
        if user.get("groups"):
            cmd += ["--groups", ",".join(user["groups"])]
        chroot(runner, rootfs, *cmd, name)
        if "password_hash_from" in user:
            pw_hash = secrets[user["password_hash_from"]]
            if "\n" in pw_hash or ":" in pw_hash:
                raise StageError(f"password hash for user {name} must be a single line without ':'")
            chroot(runner, rootfs, "chpasswd", "--encrypted", input=f"{name}:{pw_hash}\n")
        if "ssh_keys_from" in user:
            keys = rootfs_path(rootfs, f"/home/{name}/.ssh/authorized_keys")
            keys.write_text(secrets[user["ssh_keys_from"]].strip() + "\n", encoding="utf-8")
            keys.parent.chmod(0o700)
            keys.chmod(0o600)
            chroot(runner, rootfs, "chown", "-R", f"{name}:{name}", f"/home/{name}/.ssh")


def copy_files(profile: Profile, repo_root: Path, rootfs: Path) -> None:
    for entry in profile.files:
        src, dest = Path(repo_root) / entry["src"], entry["dest"]
        if src.is_dir():
            for f in sorted(p for p in src.rglob("*") if p.is_file()):
                rel = f.relative_to(src).as_posix()
                _copy_file(f, rootfs_path(rootfs, f"{dest.rstrip('/')}/{rel}"))
        else:
            target = f"{dest}{src.name}" if dest.endswith("/") else dest
            _copy_file(src, rootfs_path(rootfs, target))


def _copy_file(src: Path, target: Path) -> None:
    shutil.copyfile(src, target)
    target.chmod(0o644)


def fix_home_ownership(profile: Profile, rootfs: Path, runner: Runner) -> None:
    for user in profile.users:
        name = user["name"]
        if (Path(rootfs) / "home" / name).exists():
            chroot(runner, rootfs, "chown", "-R", f"{name}:{name}", f"/home/{name}")


def set_services(profile: Profile, rootfs: Path, runner: Runner) -> None:
    for unit in profile.services_enable:
        chroot(runner, rootfs, "systemctl", "enable", unit)
    for unit in profile.services_disable:
        chroot(runner, rootfs, "systemctl", "disable", unit)


def render_install_conf(cfg: dict) -> str:
    return (f"OSP_TARGET_DISK={shlex.quote(cfg.get('target_disk', ''))}\n"
            f"OSP_HOSTNAME={shlex.quote(cfg.get('hostname', ''))}\n"
            f"OSP_CONFIRM={'1' if cfg.get('confirm', True) else '0'}\n")


def _payload(rel: str):
    return resources.files("osp").joinpath("payload", *rel.split("/"))


def _install_node(node, rootfs: Path, dest: str, mode: int) -> None:
    if node.is_dir():
        for child in node.iterdir():
            _install_node(child, rootfs, f"{dest}/{child.name}", mode)
    else:
        target = rootfs_path(rootfs, dest)
        target.write_bytes(node.read_bytes())
        target.chmod(mode)


def install_payload(profile: Profile, repo_root: Path, rootfs: Path, runner: Runner) -> None:
    mode = profile.installer_mode
    for rel, dest, perm, modes in PAYLOAD_FILES:
        if modes is None or mode in modes:
            _install_node(_payload(rel), rootfs, dest, perm)
    rootfs_path(rootfs, "/etc/osp/checks").write_text("".join(f"{c}\n" for c in profile.checks))
    if mode in ("unattended", "tui"):
        cfg = load_install_config(Path(repo_root) / profile.installer_config) if profile.installer_config else {}
        rootfs_path(rootfs, "/etc/osp/install.conf").write_text(render_install_conf(cfg))
        rootfs_path(rootfs, "/etc/osp/live-packages").write_text(" ".join(LIVE_PACKAGES) + "\n")
    for unit in UNITS[mode]:
        chroot(runner, rootfs, "systemctl", "enable", unit)


def configure_live(profile: Profile, rootfs: Path) -> None:
    rootfs_path(rootfs, "/etc/hostname").write_text(f"{profile.name}\n")
    rootfs_path(rootfs, "/etc/hosts").write_text(
        f"127.0.0.1\tlocalhost\n127.0.1.1\t{profile.name}\n::1\tlocalhost ip6-localhost ip6-loopback\n")
    user = profile.autologin_user
    if user is None:
        return
    rootfs_path(rootfs, "/etc/systemd/system/getty@tty1.service.d/autologin.conf").write_text(
        f"[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin {user} --noclear %I $TERM\n")
    if profile.desktop and profile.desktop["display_manager"] == "lightdm":
        rootfs_path(rootfs, "/etc/lightdm/lightdm.conf.d/50-osp-autologin.conf").write_text(
            f"[Seat:*]\nautologin-user={user}\n")


def run_hooks(profile: Profile, repo_root: Path, rootfs: Path, runner: Runner) -> None:
    if not profile.hooks:
        return
    hook_dir = Path(rootfs) / "tmp" / "osp-hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    try:
        for i, rel in enumerate(profile.hooks):
            src = Path(repo_root) / rel
            name = f"{i:02d}-{src.name}"
            shutil.copyfile(src, hook_dir / name)
            try:
                chroot(runner, rootfs, "env", f"OSP_PROFILE={profile.name}",
                       "bash", "-euo", "pipefail", f"/tmp/osp-hooks/{name}")
            except CommandError as e:
                raise StageError(f"customize hook failed: {rel} (exit {e.returncode})", e.log_path) from e
    finally:
        shutil.rmtree(hook_dir, ignore_errors=True)


def finalize(rootfs: Path, runner: Runner) -> None:
    chroot(runner, rootfs, "update-initramfs", "-u", "-k", "all")
    chroot(runner, rootfs, "apt-get", "clean")
    chroot(runner, rootfs, "sh", "-c", "rm -rf /var/lib/apt/lists/*")
    for key in (Path(rootfs) / "etc/ssh").glob("ssh_host_*"):
        key.unlink()
    rootfs_path(rootfs, "/etc/machine-id").write_text("")
    dbus_id = Path(rootfs) / "var/lib/dbus/machine-id"
    if os.path.lexists(dbus_id):
        dbus_id.unlink()


@contextmanager
def _no_service_start(rootfs: Path):
    """policy-rc.d keeps package postinst scripts from starting services in the chroot."""
    path = Path(rootfs) / "usr/sbin/policy-rc.d"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 101\n")
    path.chmod(0o755)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def _host_resolv(rootfs: Path, host_resolv: Path = Path("/etc/resolv.conf")):
    """Give the chroot working DNS, then put the original resolv.conf back."""
    target = Path(rootfs) / "etc/resolv.conf"
    backup = Path(rootfs) / "etc/resolv.conf.osp-bak"
    had_original = os.path.lexists(target)
    if had_original:
        os.replace(target, backup)
    content = host_resolv.read_text() if host_resolv.is_file() else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    try:
        yield
    finally:
        ours = target.is_file() and not target.is_symlink() and target.read_text() == content
        if ours:
            target.unlink()
            if had_original:
                os.replace(backup, target)
        elif had_original and os.path.lexists(backup):
            backup.unlink()  # a package replaced resolv.conf on purpose; keep its version


def customize(profile: Profile, secrets: Mapping[str, str], src_rootfs: Path, rootfs: Path,
              repo_root: Path, runner: Runner) -> None:
    rootfs = Path(rootfs)
    rootfs.mkdir(parents=True)
    runner.run(["cp", "-a", f"{src_rootfs}/.", str(rootfs)])
    with chroot_mounts(rootfs, runner), _no_service_start(rootfs), _host_resolv(rootfs):
        install_packages(profile, rootfs, runner)
        create_users(profile, secrets, rootfs, runner)
        copy_files(profile, repo_root, rootfs)
        fix_home_ownership(profile, rootfs, runner)
        set_services(profile, rootfs, runner)
        install_payload(profile, repo_root, rootfs, runner)
        configure_live(profile, rootfs)
        run_hooks(profile, repo_root, rootfs, runner)
        finalize(rootfs, runner)
