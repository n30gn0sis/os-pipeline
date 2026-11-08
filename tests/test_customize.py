import os
import sys
from pathlib import Path

import pytest

from helpers import profile_data, write_yaml
from osp import customize as cz
from osp.profile import Profile
from osp.run import RecordingRunner, Runner
from osp.stages import StageError


def make(**kw) -> Profile:
    return Profile(profile_data(**kw), Path("x"))


def chroot_cmds(runner, rootfs):
    return [c[2:] for c in runner.calls if c[:2] == ["chroot", str(rootfs)]]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_rootfs_path_refuses_symlink_escape(tmp_path):
    rootfs, outside = tmp_path / "r", tmp_path / "outside"
    rootfs.mkdir(); outside.mkdir()
    (rootfs / "etc").symlink_to(outside)
    with pytest.raises(StageError, match="outside the rootfs"):
        cz.rootfs_path(rootfs, "/etc/passwd")


def test_rootfs_path_creates_parents(tmp_path):
    p = cz.rootfs_path(tmp_path, "/etc/osp/checks")
    assert p == tmp_path / "etc" / "osp" / "checks" and p.parent.is_dir()


def test_install_packages_adds_installer_tools(tmp_path):
    r = RecordingRunner()
    cz.install_packages(make(packages=["vim"], installer={"mode": "tui", "config": "i.yaml"}), tmp_path, r)
    cmds = chroot_cmds(r, tmp_path)
    assert cmds[0] == ["apt-get", "update"]
    assert cmds[1][:6] == ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "--no-install-recommends"]
    assert {"vim", "whiptail", "gdisk", "squashfs-tools"} <= set(cmds[1])


def test_install_packages_noop_without_packages(tmp_path):
    r = RecordingRunner()
    cz.install_packages(make(), tmp_path, r)
    assert r.calls == []


def test_create_users_with_secrets(tmp_path):
    r = RecordingRunner()
    p = make(users=[{"name": "admin", "groups": ["sudo"], "ssh_keys_from": "env:K", "password_hash_from": "env:P"}])
    cz.create_users(p, {"env:K": "ssh-ed25519 AAAA", "env:P": "$6$x"}, tmp_path, r)
    cmds = chroot_cmds(r, tmp_path)
    assert cmds[0] == ["useradd", "--create-home", "--shell", "/bin/bash", "--groups", "sudo", "admin"]
    assert cmds[1] == ["chpasswd", "--encrypted"]
    assert "admin:$6$x\n" in r.inputs
    assert (tmp_path / "home/admin/.ssh/authorized_keys").read_text() == "ssh-ed25519 AAAA\n"
    assert cmds[2] == ["chown", "-R", "admin:admin", "/home/admin/.ssh"]


def test_copy_files_dir_and_file(tmp_path):
    repo, rootfs = tmp_path / "repo", tmp_path / "r"
    (repo / "files/d/sub").mkdir(parents=True)
    (repo / "files/d/sub/a.conf").write_text("a")
    (repo / "files/one.txt").write_text("1")
    p = make(files=[{"src": "files/d/", "dest": "/etc/d/"},
                    {"src": "files/one.txt", "dest": "/opt/"},
                    {"src": "files/one.txt", "dest": "/opt/renamed.txt"}])
    cz.copy_files(p, repo, rootfs)
    assert (rootfs / "etc/d/sub/a.conf").read_text() == "a"
    assert (rootfs / "opt/one.txt").read_text() == "1"
    assert (rootfs / "opt/renamed.txt").read_text() == "1"


def test_set_services(tmp_path):
    r = RecordingRunner()
    cz.set_services(make(services={"enable": ["ssh"], "disable": ["apt-daily.timer"]}), tmp_path, r)
    assert chroot_cmds(r, tmp_path) == [["systemctl", "enable", "ssh"], ["systemctl", "disable", "apt-daily.timer"]]


def test_render_install_conf_quotes_and_defaults():
    assert cz.render_install_conf({"target_disk": "/dev/sda", "hostname": "h"}) == (
        "OSP_TARGET_DISK=/dev/sda\nOSP_HOSTNAME=h\nOSP_CONFIRM=1\n")
    assert "OSP_CONFIRM=0" in cz.render_install_conf({"confirm": False})
    assert "OSP_TARGET_DISK=''" in cz.render_install_conf({})


def test_install_payload_unattended(tmp_path):
    repo, rootfs = tmp_path / "repo", tmp_path / "r"
    write_yaml(repo / "installer/i.yaml", {"target_disk": "/dev/sda", "confirm": False})
    p = make(installer={"mode": "unattended", "config": "installer/i.yaml"}, test={"checks": ["true", "systemctl is-active ssh"]})
    r = RecordingRunner()
    cz.install_payload(p, repo, rootfs, r)
    assert (rootfs / "usr/local/sbin/install-to-disk").read_bytes().startswith(b"#!/bin/bash")
    assert (rootfs / "etc/systemd/system/osp-installer.service").is_file()
    assert not (rootfs / "etc/systemd/system/osp-installer-tui.service").exists()
    assert (rootfs / "etc/osp/checks").read_text() == "true\nsystemctl is-active ssh\n"
    assert "OSP_TARGET_DISK=/dev/sda" in (rootfs / "etc/osp/install.conf").read_text()
    assert (rootfs / "etc/osp/live-packages").read_text().split() == cz.LIVE_PACKAGES
    assert chroot_cmds(r, rootfs) == [["systemctl", "enable", "osp-ready.service"],
                                      ["systemctl", "enable", "osp-installer.service"]]
    if sys.platform != "win32":
        assert os.stat(rootfs / "usr/local/sbin/install-to-disk").st_mode & 0o777 == 0o755


def test_install_payload_calamares(tmp_path):
    rootfs = tmp_path / "r"
    cz.install_payload(make(installer={"mode": "calamares"}, desktop={"display_manager": "lightdm"}), tmp_path, rootfs, RecordingRunner())
    assert (rootfs / "etc/calamares/settings.conf").is_file()
    assert (rootfs / "etc/calamares/modules/unpackfs.conf").is_file()
    assert (rootfs / "etc/xdg/autostart/osp-calamares.desktop").is_file()
    assert not (rootfs / "usr/local/sbin/install-to-disk").exists()


def test_configure_live_autologin_and_hostname(tmp_path):
    p = make(users=[{"name": "live"}], live={"autologin": True}, desktop={"display_manager": "lightdm"})
    cz.configure_live(p, tmp_path)
    assert (tmp_path / "etc/hostname").read_text() == "t\n"
    assert "127.0.1.1\tt" in (tmp_path / "etc/hosts").read_text()
    assert "--autologin live" in (tmp_path / "etc/systemd/system/getty@tty1.service.d/autologin.conf").read_text()
    assert "autologin-user=live" in (tmp_path / "etc/lightdm/lightdm.conf.d/50-osp-autologin.conf").read_text()


def test_configure_live_without_autologin(tmp_path):
    cz.configure_live(make(users=[{"name": "u"}]), tmp_path)
    assert not (tmp_path / "etc/systemd/system/getty@tty1.service.d").exists()


def test_run_hooks_runs_in_chroot_and_cleans_up(tmp_path):
    repo, rootfs = tmp_path / "repo", tmp_path / "r"
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks/a.sh").write_text("echo a")
    r = RecordingRunner()
    cz.run_hooks(make(hooks={"customize": ["hooks/a.sh"]}), repo, rootfs, r)
    assert chroot_cmds(r, rootfs) == [["env", "OSP_PROFILE=t", "bash", "-euo", "pipefail", "/tmp/osp-hooks/00-a.sh"]]
    assert not (rootfs / "tmp/osp-hooks").exists()


def test_run_hooks_failure_names_hook(tmp_path):
    repo = tmp_path / "repo"
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks/bad.sh").write_text("exit 1")
    r = RecordingRunner(results={"chroot": (1, "")})
    with pytest.raises(StageError, match="hooks/bad.sh"):
        cz.run_hooks(make(hooks={"customize": ["hooks/bad.sh"]}), repo, tmp_path / "r", r)


def test_host_resolv_is_restored(tmp_path):
    rootfs = tmp_path / "r"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "etc/resolv.conf").write_text("original")
    host = tmp_path / "host-resolv"
    host.write_text("nameserver 1.1.1.1\n")
    with cz._host_resolv(rootfs, host):
        assert (rootfs / "etc/resolv.conf").read_text() == "nameserver 1.1.1.1\n"
    assert (rootfs / "etc/resolv.conf").read_text() == "original"
    assert not (rootfs / "etc/resolv.conf.osp-bak").exists()


def test_customize_orchestration_order(tmp_path):
    src, rootfs = tmp_path / "src", tmp_path / "r"
    src.mkdir()
    r = RecordingRunner()
    p = make(packages=["vim"], users=[{"name": "u"}], services={"enable": ["ssh"]})
    cz.customize(p, {}, src, rootfs, tmp_path, r)
    assert r.calls[0] == ["cp", "-a", f"{src}/.", str(rootfs)]
    programs = [c[2] if c[0] == "chroot" else c[0] for c in r.calls]
    assert programs.index("mount") < programs.index("apt-get") < programs.index("useradd") < programs.index("systemctl")
    assert programs.index("update-initramfs") > programs.index("systemctl")
    assert programs[-1] == "umount"
    assert not (rootfs / "usr/sbin/policy-rc.d").exists()


@pytest.mark.container
def test_customize_real_rootfs(tmp_path):
    from osp.backends import debian

    repo = tmp_path / "repo"
    (repo / "files").mkdir(parents=True)
    (repo / "files/motd").write_text("hello osp\n")
    (repo / "hooks").mkdir()
    (repo / "hooks/mark.sh").write_text('echo "$OSP_PROFILE" > /etc/osp-hook-ran\n')
    p = make(packages=["less"], users=[{"name": "admin", "groups": ["sudo"]}],
             files=[{"src": "files/motd", "dest": "/etc/motd"}],
             services={"enable": ["systemd-networkd"]}, hooks={"customize": ["hooks/mark.sh"]},
             test={"checks": ["true"]})
    base = tmp_path / "base"
    debian.bootstrap(p, base, Runner(log_path=tmp_path / "b.log"))
    rootfs = tmp_path / "rootfs"
    cz.customize(p, {}, base, rootfs, repo, Runner(log_path=tmp_path / "c.log"))
    assert "admin:" in (rootfs / "etc/passwd").read_text()
    assert (rootfs / "etc/motd").read_text() == "hello osp\n"
    assert (rootfs / "etc/osp-hook-ran").read_text() == "t\n"
    assert (rootfs / "etc/systemd/system/multi-user.target.wants/osp-ready.service").is_symlink()
    assert (rootfs / "usr/bin/less").exists()
    assert not (rootfs / "usr/sbin/policy-rc.d").exists()
    assert (rootfs / "etc/machine-id").read_text() == ""
