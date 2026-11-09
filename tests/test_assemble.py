import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from helpers import profile_data
from osp import assemble as asm
from osp.profile import Profile
from osp.run import RecordingRunner, Runner
from osp.stages import StageError


def make(**kw) -> Profile:
    return Profile(profile_data(**kw), Path("x"))


def fake_rootfs(root: Path, versions=("6.1.0-1",)) -> Path:
    (root / "boot").mkdir(parents=True)
    for v in versions:
        (root / "boot" / f"vmlinuz-{v}").write_bytes(b"kernel")
        (root / "boot" / f"initrd.img-{v}").write_bytes(b"initrd")
    (root / "etc").mkdir()
    (root / "etc" / "hostname").write_text("t\n")
    return root


def test_grub_cfg_minimal():
    cfg = asm.render_grub_cfg(make())
    assert "serial --unit=0 --speed=115200" in cfg
    assert cfg.count("menuentry") == 2
    assert f"linux /live/vmlinuz {asm.LIVE_CMDLINE}\n" in cfg
    assert f"linux /live/vmlinuz {asm.LIVE_CMDLINE} nomodeset" in cfg
    assert "Install" not in cfg and "persistence" not in cfg


def test_grub_cfg_persistence_and_install():
    cfg = asm.render_grub_cfg(make(live={"persistence": True}, installer={"mode": "tui", "config": "i"}))
    assert 'menuentry "Live (persistent)"' in cfg and f"{asm.LIVE_CMDLINE} persistence" in cfg
    assert 'menuentry "Install"' in cfg and f"{asm.LIVE_CMDLINE} osp.install=tui" in cfg


def test_find_kernel(tmp_path):
    k, i = asm.find_kernel(fake_rootfs(tmp_path))
    assert k.name == "vmlinuz-6.1.0-1" and i.name == "initrd.img-6.1.0-1"


def test_find_kernel_rejects_zero_or_many(tmp_path):
    with pytest.raises(StageError, match="exactly one kernel"):
        asm.find_kernel(fake_rootfs(tmp_path / "a", versions=()))
    with pytest.raises(StageError, match="exactly one kernel"):
        asm.find_kernel(fake_rootfs(tmp_path / "b", versions=("1", "2")))


def test_find_kernel_requires_initrd(tmp_path):
    root = fake_rootfs(tmp_path)
    (root / "boot" / "initrd.img-6.1.0-1").unlink()
    with pytest.raises(StageError, match="initrd"):
        asm.find_kernel(root)


def test_installed_packages_parses_dpkg_query(tmp_path):
    (tmp_path / "var/lib/dpkg").mkdir(parents=True)
    (tmp_path / "var/lib/dpkg/status").write_text("")
    r = RecordingRunner(results={"dpkg-query": (0, "installed\tbash\t5.2\ninstalled\tlive-boot\t1:20230131\nconfig-files\told\t1.0\n")})
    assert asm.installed_packages(tmp_path, r) == {"bash": "5.2", "live-boot": "1:20230131"}
    assert r.calls[0][1] == f"--admindir={tmp_path}/var/lib/dpkg"


def test_installed_packages_without_dpkg_status(tmp_path):
    assert asm.installed_packages(tmp_path, RecordingRunner()) == {}


def test_build_manifest_keeps_refs_not_values():
    p = make(users=[{"name": "a", "ssh_keys_from": "env:K"}])
    m = asm.build_manifest(p, "h" * 64, "stem", {"bash": "5"}, {"xorriso": "1"})
    assert m["artifact"] == "stem.iso" and m["build_hash"] == "h" * 64
    assert m["profile"]["users"][0]["ssh_keys_from"] == "env:K"
    assert m["packages"] == {"bash": "5"} and m["tool_versions"] == {"xorriso": "1"} and "built" in m


class IsoWritingRunner(RecordingRunner):
    def run(self, cmd, **kw):
        res = super().run(cmd, **kw)
        if cmd[0] == "grub-mkrescue":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"iso-bytes")
        return res


def test_assemble_lays_out_iso_tree_and_metadata(tmp_path):
    rootfs = fake_rootfs(tmp_path / "rootfs")
    out = tmp_path / "out"
    out.mkdir()
    r = IsoWritingRunner()
    iso = asm.assemble(make(), rootfs, out, "stem", "f" * 64, r)
    assert iso == out / "stem.iso"
    assert (out / "iso/live/vmlinuz").read_bytes() == b"kernel"
    assert (out / "iso/live/initrd.img").read_bytes() == b"initrd"
    assert "menuentry" in (out / "iso/boot/grub/grub.cfg").read_text()
    squash = next(c for c in r.calls if c[0] == "mksquashfs")
    assert squash[1:3] == [str(rootfs), str(out / "iso/live/filesystem.squashfs")]
    assert ["grub-mkrescue", "-o", str(iso), str(out / "iso")] in r.calls
    digest = hashlib.sha256(b"iso-bytes").hexdigest()
    assert (out / "stem.iso.sha256").read_text() == f"{digest}  stem.iso\n"
    assert json.loads((out / "stem.build.json").read_text())["artifact"] == "stem.iso"


@pytest.mark.container
def test_assemble_builds_real_iso(tmp_path):
    rootfs = fake_rootfs(tmp_path / "rootfs")
    out = tmp_path / "out"
    out.mkdir()
    iso = asm.assemble(make(installer={"mode": "tui", "config": "i"}), rootfs, out, "stem", "f" * 64,
                       Runner(log_path=tmp_path / "a.log"))
    listing = subprocess.run(["xorriso", "-indev", str(iso), "-find", "/", "-type", "f"],
                             capture_output=True, text=True, check=True).stdout
    for path in ["/live/filesystem.squashfs", "/live/vmlinuz", "/live/initrd.img", "/boot/grub/grub.cfg"]:
        assert f"'{path}'" in listing


@pytest.mark.container
def test_installed_packages_real_dpkg_query(tmp_path):
    dpkg = tmp_path / "var/lib/dpkg"
    (dpkg / "info").mkdir(parents=True)
    (dpkg / "available").write_text("")
    (dpkg / "status").write_text(
        "Package: keep\nStatus: install ok installed\nVersion: 1.2\nArchitecture: amd64\n\n"
        "Package: gone\nStatus: deinstall ok config-files\nVersion: 3.4\nArchitecture: amd64\n\n")
    assert asm.installed_packages(tmp_path, Runner(log_path=tmp_path / "d.log")) == {"keep": "1.2"}
