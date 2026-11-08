import os
import shutil
import subprocess
import sys
from importlib import resources

import pytest

PAYLOAD = resources.files("osp").joinpath("payload")
SCRIPTS = ["osp-ready", "install-to-disk"]
UNITS = ["osp-ready.service", "osp-installer.service", "osp-installer-tui.service"]

needs_bash = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"),
                                reason="needs a POSIX bash")


def test_payload_files_exist():
    for name in [*SCRIPTS, *UNITS, "grub-serial.cfg"]:
        assert PAYLOAD.joinpath(name).is_file(), name


def test_scripts_use_lf_and_shebang():
    for name in SCRIPTS:
        data = PAYLOAD.joinpath(name).read_bytes()
        assert data.startswith(b"#!/bin/bash\n") and b"\r\n" not in data


def test_installer_units_are_gated_on_cmdline():
    assert "ConditionKernelCommandLine=osp.install=unattended" in PAYLOAD.joinpath("osp-installer.service").read_text()
    assert "ConditionKernelCommandLine=osp.install=tui" in PAYLOAD.joinpath("osp-installer-tui.service").read_text()


@needs_bash
@pytest.mark.parametrize("name", SCRIPTS)
def test_bash_syntax(name):
    subprocess.run(["bash", "-n", str(PAYLOAD.joinpath(name))], check=True)


def source_and_run(snippet: str) -> str:
    script = f'source "{PAYLOAD.joinpath("install-to-disk")}"; {snippet}'
    return subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout


@needs_bash
def test_part_naming():
    assert source_and_run("part /dev/sda 2; part /dev/nvme0n1 3; part /dev/vda 1").split() == [
        "/dev/sda2", "/dev/nvme0n1p3", "/dev/vda1"]


@needs_bash
def test_apply_cmdline_overrides():
    out = source_and_run(
        'apply_cmdline "boot=live osp.target=/dev/vda osp.confirm=0 osp.keep_test_marker=1 osp.hostname=h1";'
        ' echo "$OSP_TARGET_DISK $OSP_CONFIRM $OSP_KEEP_TEST_MARKER $OSP_HOSTNAME"')
    assert out.strip() == "/dev/vda 0 1 h1"


@needs_bash
def test_osp_ready_reports_checks(tmp_path):
    checks = tmp_path / "checks"
    checks.write_text("true\n\nfalse\n")
    out = tmp_path / "serial"
    env = {**os.environ, "OSP_READY_OUT": str(out), "OSP_CHECKS_FILE": str(checks),
           "OSP_CHECK_TRIES": "1", "OSP_CHECK_SLEEP": "0"}
    subprocess.run(["bash", str(PAYLOAD.joinpath("osp-ready"))], check=True, env=env)
    assert out.read_text().splitlines() == [
        "OSP-READY", "OSP-CHECK-PASS true", "OSP-CHECK-FAIL false", "OSP-CHECKS-DONE 1"]
