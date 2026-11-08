import sys
from pathlib import Path

import pytest

from osp.qemu import INSTALL_ARGS, OVMF_CODE, CheckParser, qemu_command, watch


def test_qemu_command_bios_cdrom_tcg():
    cmd = qemu_command(firmware="bios", kvm=False, cdrom=Path("/i.iso"))
    assert cmd[0] == "qemu-system-x86_64"
    assert ["-serial", "stdio"] == cmd[cmd.index("-serial"):cmd.index("-serial") + 2]
    assert "-no-reboot" in cmd and "-enable-kvm" not in cmd and ["-cpu", "max"] == cmd[cmd.index("-cpu"):cmd.index("-cpu") + 2]
    assert f"id=osp-cd,if=none,file={Path('/i.iso')},media=cdrom,readonly=on" in cmd
    assert not any("pflash" in a for a in cmd)


def test_qemu_command_uses_virtio_gpu():
    # wlroots 0.16 (cage on bookworm) needs DRM PRIME import, which bochs-drm (-vga std) lacks.
    cmd = qemu_command(firmware="bios", kvm=False, cdrom=Path("/i.iso"))
    assert ["-vga", "virtio"] == cmd[cmd.index("-vga"):cmd.index("-vga") + 2]


def test_qemu_command_attaches_cdrom_via_ahci():
    # OVMF + direct kernel boot never finds a CD-ROM on the legacy PIIX IDE bus; AHCI works
    # for both firmwares and both boot paths.
    cmd = qemu_command(firmware="uefi", kvm=True, vars_path=Path("/w/vars.fd"), cdrom=Path("/i.iso"),
                       kernel=Path("/w/k"), initrd=Path("/w/i"), append="boot=live")
    assert ["-device", "ahci,id=osp-ahci"] == cmd[cmd.index("ahci,id=osp-ahci") - 1:cmd.index("ahci,id=osp-ahci") + 1]
    assert "ide-cd,drive=osp-cd,bus=osp-ahci.0" in cmd
    assert not any(a.startswith("file=") and "media=cdrom" in a for a in cmd)


def test_qemu_command_uefi_kvm_disk_and_kernel():
    cmd = qemu_command(firmware="uefi", kvm=True, vars_path=Path("/w/vars.fd"), disk=Path("/w/d.qcow2"),
                       kernel=Path("/w/k"), initrd=Path("/w/i"), append="boot=live x")
    assert "-enable-kvm" in cmd and ["-cpu", "host"] == cmd[cmd.index("-cpu"):cmd.index("-cpu") + 2]
    assert f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}" in cmd
    assert f"if=pflash,format=raw,file={Path('/w/vars.fd')}" in cmd
    assert f"file={Path('/w/d.qcow2')},if=virtio,format=qcow2" in cmd
    assert cmd[cmd.index("-append") + 1] == "boot=live x"


def test_qemu_command_validates_firmware():
    with pytest.raises(ValueError):
        qemu_command(firmware="uefi", kvm=False)
    with pytest.raises(ValueError):
        qemu_command(firmware="coreboot", kvm=False)


def test_install_args_target_virtio_and_keep_marker():
    assert "osp.install=unattended" in INSTALL_ARGS and "osp.target=/dev/vda" in INSTALL_ARGS
    assert "osp.confirm=0" in INSTALL_ARGS and "osp.keep_test_marker=1" in INSTALL_ARGS


def test_check_parser_pass():
    p = CheckParser()
    for line in ["[  OK  ] Started x\r\n", "OSP-READY\r\n", "OSP-CHECK-PASS true\r\n"]:
        assert p.feed(line) is None
    assert p.feed("OSP-CHECKS-DONE 0\r\n") == "done"
    r = p.result("done", Path("l"))
    assert r.ok and r.passed == ["true"] and r.failed == []


def test_check_parser_failures_and_timeouts():
    p = CheckParser()
    p.feed("OSP-READY")
    p.feed("OSP-CHECK-FAIL systemctl is-active ssh")
    p.feed("OSP-CHECKS-DONE 1")
    r = p.result("done", Path("l"))
    assert not r.ok and r.failed == ["systemctl is-active ssh"]
    r2 = CheckParser().result("timeout", Path("l"))
    assert not r2.ok and "timeout" in r2.reason and "not seen" in r2.reason


def test_check_parser_done_before_ready_is_not_terminal():
    p = CheckParser()
    assert p.feed("OSP-CHECKS-DONE 0") is None
    assert not p.result("timeout", Path("l")).ok
    assert not p.result("done", Path("l")).ok


def test_check_parser_nonzero_rc_without_failures_is_not_ok():
    p = CheckParser()
    p.feed("OSP-READY")
    assert p.feed("OSP-CHECKS-DONE 1") == "done"
    r = p.result("done", Path("l"))
    assert not r.ok and "rc=1" in r.reason and r.failed == []


def test_check_parser_ignores_markers_inside_check_commands():
    p = CheckParser()
    p.feed("OSP-READY")
    assert p.feed("OSP-CHECK-PASS echo OSP-CHECKS-DONE 0") is None
    assert p.passed == ["echo OSP-CHECKS-DONE 0"]
    assert p.feed("OSP-CHECKS-DONE 0") == "done"


def test_check_parser_accepts_leading_ansi_before_ready():
    p = CheckParser()
    assert p.feed("\x1b[0;32mOSP-READY") is None
    assert p.ready


def test_check_parser_finds_markers_after_a_login_prompt():
    # osp-ready writes to /dev/console; the serial getty's prompt can share the line.
    p = CheckParser()
    p.feed("OSP-READY")
    assert p.feed("kiosk login: OSP-CHECK-FAIL pgrep -x cage") is None
    assert p.failed == ["pgrep -x cage"]
    assert p.feed("host login: OSP-CHECK-PASS echo OSP-CHECKS-DONE 0") is None
    assert p.passed == ["echo OSP-CHECKS-DONE 0"]
    assert p.feed("host login: OSP-CHECKS-DONE 1") == "done" and p.rc == 1
    q = CheckParser()
    q.feed("host login: OSP-READY")
    assert q.ready


def py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def test_watch_returns_feed_verdict_and_logs(tmp_path):
    log = tmp_path / "serial.log"
    verdict = watch(py("print('OSP-READY'); print('OSP-CHECKS-DONE 0'); import time; time.sleep(30)"),
                    log_path=log, timeout=20, feed=CheckParser().feed)
    assert verdict == "done"
    assert "OSP-CHECKS-DONE 0" in log.read_text()


def test_watch_exited_and_timeout(tmp_path):
    assert watch(py("print('x')"), log_path=tmp_path / "a.log", timeout=20, feed=lambda l: None) == "exited"
    assert watch(py("import time; time.sleep(30)"), log_path=tmp_path / "b.log", timeout=1,
                 feed=lambda l: None) == "timeout"


def test_watch_hung_when_process_outlives_wait_exit(tmp_path):
    verdict = watch(py("print('MARK'); import time; time.sleep(30)"), log_path=tmp_path / "c.log",
                    timeout=20, feed=lambda l: "done" if "MARK" in l else None, wait_exit=1)
    assert verdict == "hung"


def test_watch_returns_feed_verdict_when_process_exits_after_marker(tmp_path):
    verdict = watch(py("print('MARK')"), log_path=tmp_path / "d.log", timeout=20,
                    feed=lambda l: "done" if "MARK" in l else None, wait_exit=10)
    assert verdict == "done"
