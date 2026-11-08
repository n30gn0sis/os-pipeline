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


@needs_bash
def test_osp_ready_checks_do_not_consume_checks_file(tmp_path):
    checks = tmp_path / "checks"
    checks.write_text("cat >/dev/null\nfalse\n")
    out = tmp_path / "serial"
    env = {**os.environ, "OSP_READY_OUT": str(out), "OSP_CHECKS_FILE": str(checks),
           "OSP_CHECK_TRIES": "1", "OSP_CHECK_SLEEP": "0"}
    subprocess.run(["bash", str(PAYLOAD.joinpath("osp-ready"))], check=True, env=env)
    assert out.read_text().splitlines() == [
        "OSP-READY", "OSP-CHECK-PASS cat >/dev/null", "OSP-CHECK-FAIL false", "OSP-CHECKS-DONE 1"]


def run_check_target(tmp_path, mode="--unattended", target="/dev/vdb", **stub):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "lsblk").write_text(
        '#!/bin/bash\n'
        'case "$*" in\n'
        '  "-dno TYPE "*) echo "${STUB_TYPE-disk}" ;;\n'
        '  "-no PKNAME "*) echo "${STUB_PK-}" ;;\n'
        '  "-nrso NAME "*) printf "%s\n" ${STUB_ANC-} ;;\n'
        '  "-nro MOUNTPOINT "*) echo "${STUB_MNT-}" ;;\n'
        'esac\n', newline="\n")
    (bindir / "findmnt").write_text('#!/bin/bash\necho "${STUB_SRC-}"\n', newline="\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           **{f"STUB_{k.upper()}": v for k, v in stub.items()}}
    script = (f'source "{PAYLOAD.joinpath("install-to-disk")}"; OSP_TARGET_DISK={target}; '
              f'check_target {mode}; echo "ok $OSP_TARGET_DISK"')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


@needs_bash
def test_check_target_accepts_other_disk(tmp_path):
    r = run_check_target(tmp_path, src="/dev/sda1", pk="sda", anc="sda1 sda")
    assert r.returncode == 0 and "ok /dev/vdb" in r.stdout


@needs_bash
def test_check_target_refuses_live_disk_and_ancestors(tmp_path):
    assert "live medium" in run_check_target(tmp_path, target="/dev/sda", src="/dev/sda1", pk="sda", anc="sda1 sda").stderr
    assert "live medium" in run_check_target(tmp_path, src="/dev/mapper/x", pk="", anc="x vdb").stderr


@needs_bash
def test_check_target_refuses_partition_and_mounted(tmp_path):
    assert "whole disk" in run_check_target(tmp_path, type="part", src="/dev/sda1", pk="sda").stderr
    assert "mounted" in run_check_target(tmp_path, src="/dev/sda1", pk="sda", mnt="/mnt").stderr


@needs_bash
def test_check_target_fails_closed_without_live_medium(tmp_path):
    assert "cannot determine" in run_check_target(tmp_path).stderr
    assert run_check_target(tmp_path, mode="--tui").returncode == 0


def test_unattended_installer_owns_tty1_for_input():
    unit = PAYLOAD.joinpath("osp-installer.service").read_text()
    for line in ["StandardInput=tty", "TTYPath=/dev/tty1", "Conflicts=getty@tty1.service",
                 "StandardOutput=journal+console", "StandardError=journal+console"]:
        assert line in unit.splitlines(), line


def run_confirm(tmp_path, confirm="1", answer="YES\n", active="tty0 ttyS0"):
    tty, console, act = tmp_path / "tty1", tmp_path / "console", tmp_path / "active"
    if answer is not None:
        tty.write_text(answer, newline="\n")
    act.write_text(active + "\n", newline="\n")
    script = (f'source "{PAYLOAD.joinpath("install-to-disk")}"; CONFIRM_TTY="{tty}"; CONSOLE="{console}"; '
              f'CONSOLE_ACTIVE="{act}"; OSP_TARGET_DISK=/dev/vdb; OSP_CONFIRM="{confirm}"; '
              'confirm; echo confirmed')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r, _text_or_none(tty), _text_or_none(console)


def _text_or_none(path):
    return path.read_text() if path.exists() else None


def _fresh(tmp_path, name):
    d = tmp_path / "".join(c if c.isalnum() else "_" for c in name)
    d.mkdir()
    return d


@needs_bash
def test_confirm_prompts_on_tty1_and_serial_console_and_reads_tty1(tmp_path):
    r, tty, console = run_confirm(tmp_path)
    assert r.returncode == 0 and "confirmed" in r.stdout
    assert "ERASE /dev/vdb" in tty and "ERASE /dev/vdb" in console


@needs_bash
def test_confirm_skips_console_when_it_is_tty1(tmp_path):
    r, tty, console = run_confirm(tmp_path, active="tty1")
    assert r.returncode == 0 and "ERASE /dev/vdb" in tty and console is None


@needs_bash
def test_confirm_rejects_other_answers(tmp_path):
    for answer in ["no\n", "yes\n", "", None]:
        r, _, _ = run_confirm(_fresh(tmp_path, repr(answer)), answer=answer)
        assert r.returncode != 0 and "not confirmed" in r.stderr, answer


@needs_bash
def test_confirm_zero_skips_prompt_and_other_values_still_prompt(tmp_path):
    r, tty, console = run_confirm(_fresh(tmp_path, "zero"), confirm="0", answer=None)
    assert r.returncode == 0 and tty is None and console is None
    for value in ["false", "no", "00", ""]:
        r, tty, _ = run_confirm(_fresh(tmp_path, f"v{value}"), confirm=value, answer="nope\n")
        assert r.returncode != 0 and "ERASE" in tty, value
