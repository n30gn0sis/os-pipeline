from importlib import resources

import yaml

CAL = resources.files("osp").joinpath("payload", "calamares")


def load(*parts):
    return yaml.safe_load(CAL.joinpath(*parts).read_text(encoding="utf-8"))


def test_settings_sequence_and_instances():
    s = load("settings.conf")
    exec_steps = next(step["exec"] for step in s["sequence"] if "exec" in step)
    for module in ["partition", "mount", "unpackfs", "fstab", "users", "packages",
                   "shellprocess@removelive", "initramfs", "bootloader", "umount"]:
        assert module in exec_steps, module
    assert exec_steps.index("unpackfs") < exec_steps.index("bootloader")
    inst = {i["id"]: i for i in s["instances"]}
    assert inst["removelive"]["config"] == "shellprocess_removelive.conf"
    assert CAL.joinpath("modules", "shellprocess_removelive.conf").is_file()


def test_unpackfs_reads_live_squashfs():
    u = load("modules", "unpackfs.conf")
    assert u["unpack"][0]["source"] == "/run/live/medium/live/filesystem.squashfs"
    assert u["unpack"][0]["sourcefs"] == "squashfs"


def test_packages_remove_live_only_packages():
    ops = load("modules", "packages.conf")["operations"]
    removed = next(op["remove"] for op in ops if "remove" in op)
    assert {"live-boot", "calamares"} <= set(removed)


def test_removelive_drops_osp_live_files():
    script = "\n".join(load("modules", "shellprocess_removelive.conf")["script"])
    for path in ["/etc/xdg/autostart/osp-calamares.desktop", "/etc/polkit-1/rules.d/49-osp-live.rules",
                 "/etc/systemd/system/osp-ready.service", "/etc/osp"]:
        assert path in script


def test_autostart_is_gated_on_cmdline():
    desktop = resources.files("osp").joinpath("payload", "osp-calamares.desktop").read_text()
    assert "osp.install=calamares" in desktop and "pkexec calamares" in desktop
