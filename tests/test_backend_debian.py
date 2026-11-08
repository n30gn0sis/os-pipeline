from pathlib import Path

import pytest

from helpers import profile_data
from osp.backends import debian
from osp.profile import Profile
from osp.run import RecordingRunner, Runner


def make(flavor="debian", release="bookworm", **distro_extra):
    d = profile_data()
    d["distro"].update({"flavor": flavor, "release": release, **distro_extra})
    return Profile(d, Path("x"))


def test_sources_debian_defaults():
    assert debian.sources(make()) == [
        "deb http://deb.debian.org/debian bookworm main non-free-firmware",
        "deb http://deb.debian.org/debian bookworm-updates main non-free-firmware",
        "deb http://security.debian.org/debian-security bookworm-security main non-free-firmware",
    ]


def test_sources_respect_mirror_and_components():
    s = debian.sources(make(mirror="http://mirror.local/debian", components=["main"]))
    assert s[0] == "deb http://mirror.local/debian bookworm main"


def test_ubuntu_packages_and_keyring():
    p = make("ubuntu", "noble")
    assert "linux-image-generic" in debian.bootstrap_packages(p)
    runner = RecordingRunner()
    debian.bootstrap(p, Path("/tmp/r"), runner)
    cmd = runner.calls[0]
    assert cmd[:3] == ["mmdebstrap", "--mode=root", "--variant=minbase"]
    assert "--keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg" in cmd
    assert cmd[cmd.index("noble") + 1] == str(Path("/tmp/r"))
    assert cmd[-1].startswith("deb http://security.ubuntu.com/ubuntu noble-security")


@pytest.mark.parametrize("flavor,release", [("debian", "bookworm"), ("ubuntu", "noble")])
def test_bootstrap_includes_apt(flavor, release):
    # minbase installs Priority:required packages; apt is only 'important' on Ubuntu,
    # but customize needs apt-get on every flavor.
    assert "apt" in debian.bootstrap_packages(make(flavor, release))


def test_debian_bootstrap_includes_base_and_kernel():
    runner = RecordingRunner()
    debian.bootstrap(make(), Path("/tmp/r"), runner)
    include = next(a for a in runner.calls[0] if a.startswith("--include="))
    pkgs = include.split("=", 1)[1].split(",")
    assert "live-boot" in pkgs and "linux-image-amd64" in pkgs and "systemd-sysv" in pkgs
    assert not any(a.startswith("--keyring") for a in runner.calls[0])


def test_bootstrap_key_stable_and_sensitive():
    assert debian.bootstrap_key(make()) == debian.bootstrap_key(make())
    assert len(debian.bootstrap_key(make())) == 16
    assert debian.bootstrap_key(make()) != debian.bootstrap_key(make(mirror="http://m/debian"))
    assert debian.bootstrap_key(make()) != debian.bootstrap_key(make("ubuntu", "noble"))


@pytest.mark.container
def test_bootstrap_bookworm_creates_live_capable_rootfs(tmp_path):
    rootfs = tmp_path / "rootfs"
    debian.bootstrap(make(), rootfs, Runner(log_path=tmp_path / "bootstrap.log"))
    assert list((rootfs / "boot").glob("vmlinuz-*"))
    status = (rootfs / "var/lib/dpkg/status").read_text()
    assert "Package: live-boot\n" in status
