import sys
from pathlib import Path

import pytest

from osp.qemu import INSTALL_ARGS, OVMF_CODE, CheckParser, qemu_command, watch


def test_qemu_command_bios_cdrom_tcg():
    cmd = qemu_command(firmware="bios", kvm=False, cdrom=Path("/i.iso"))
    assert cmd[0] == "qemu-system-x86_64"
    assert ["-serial", "stdio"] == cmd[cmd.index("-serial"):cmd.index("-serial") + 2]
    assert "-no-reboot" in cmd and "-enable-kvm" not in cmd and ["-cpu", "max"] == cmd[cmd.index("-cpu"):cmd.index("-cpu") + 2]
    assert f"file={Path('/i.iso')},media=cdrom,readonly=on" in cmd
    assert not any("pflash" in a for a in cmd)


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


def py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


def test_watch_returns_feed_verdict_and_logs(tmp_path):
    log = tmp_path / "serial.log"
    verdict = watch(py("print('a'); print('OSP-CHECKS-DONE 0'); import time; time.sleep(30)"),
                    log_path=log, timeout=20, feed=CheckParser().feed)
    assert verdict == "done"
    assert "OSP-CHECKS-DONE 0" in log.read_text()


def test_watch_exited_and_timeout(tmp_path):
    assert watch(py("print('x')"), log_path=tmp_path / "a.log", timeout=20, feed=lambda l: None) == "exited"
    assert watch(py("import time; time.sleep(30)"), log_path=tmp_path / "b.log", timeout=1,
                 feed=lambda l: None) == "timeout"
