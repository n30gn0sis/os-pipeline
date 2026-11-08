"""Boot ISOs (and installed disks) in QEMU and read serial-console markers."""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from osp.assemble import LIVE_CMDLINE
from osp.run import Runner

OVMF_CODE = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
OVMF_VARS = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")
INSTALL_ARGS = "osp.install=unattended osp.target=/dev/vda osp.confirm=0 osp.keep_test_marker=1"
POWEROFF_TIMEOUT = 120
READY = "OSP-READY"
PASS = "OSP-CHECK-PASS "
FAIL = "OSP-CHECK-FAIL "
DONE_RE = re.compile(r"^OSP-CHECKS-DONE (\d+)$")
INSTALL_DONE = "osp-install: OSP-INSTALL-DONE"
ANSI_LEAD = re.compile(r"^(?:\x1b\[[0-9;?]*[A-Za-z])+")


@dataclass
class BootResult:
    ok: bool
    reason: str
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    log_path: Path | None = None


class CheckParser:
    def __init__(self):
        self.ready = False
        self.rc: int | None = None
        self.passed: list[str] = []
        self.failed: list[str] = []

    def feed(self, line: str) -> str | None:
        line = ANSI_LEAD.sub("", line.strip()).strip()
        if line == READY:
            self.ready = True
            return None
        if line.startswith(PASS):
            self.passed.append(line[len(PASS):])
            return None
        if line.startswith(FAIL):
            self.failed.append(line[len(FAIL):])
            return None
        m = DONE_RE.match(line)
        if m and self.ready:
            self.rc = int(m.group(1))
            return "done"
        return None

    def result(self, verdict: str, log_path: Path | None) -> BootResult:
        def fail(reason: str) -> BootResult:
            return BootResult(False, reason, self.passed, self.failed, log_path)

        if verdict != "done":
            seen = "seen" if self.ready else "not seen"
            return fail(f"no completion marker ({verdict}); ready marker {seen}")
        if not self.ready:
            return fail("ready marker OSP-READY not seen")
        if self.rc is None:
            return fail("no completion rc")
        if self.rc != 0:
            return fail(f"checks reported rc={self.rc}")
        if self.failed:
            return fail(f"{len(self.failed)} check(s) failed")
        return BootResult(True, "all checks passed", self.passed, self.failed, log_path)


def kvm_available() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def qemu_command(*, firmware: str, kvm: bool, vars_path: Path | None = None, cdrom: Path | None = None,
                 disk: Path | None = None, kernel: Path | None = None, initrd: Path | None = None,
                 append: str | None = None) -> list[str]:
    cmd = ["qemu-system-x86_64", "-m", "2048", "-smp", "2", "-display", "none", "-vga", "std",
           "-serial", "stdio", "-monitor", "none", "-no-reboot"]
    cmd += ["-enable-kvm", "-cpu", "host"] if kvm else ["-cpu", "max"]
    if firmware == "uefi":
        if vars_path is None:
            raise ValueError("uefi firmware needs vars_path")
        cmd += ["-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
                "-drive", f"if=pflash,format=raw,file={vars_path}"]
    elif firmware != "bios":
        raise ValueError(f"unknown firmware {firmware!r}")
    if cdrom:
        cmd += ["-drive", f"file={cdrom},media=cdrom,readonly=on"]
    if disk:
        cmd += ["-drive", f"file={disk},if=virtio,format=qcow2"]
    if kernel:
        cmd += ["-kernel", str(kernel), "-initrd", str(initrd), "-append", append or ""]
    return cmd


def _read_until(lines: "queue.Queue[str | None]", timeout: float, log, feed) -> str:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        try:
            line = lines.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            return "exited"
        log.write(line)
        log.flush()
        result = feed(line)
        if result is not None:
            return result


def _await_exit(proc: subprocess.Popen, lines: "queue.Queue[str | None]", log, wait_exit: float) -> bool:
    """Drain output until EOF and the process exits. False if the wait deadline passes first."""
    deadline = time.monotonic() + wait_exit
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            line = lines.get(timeout=min(remaining, 1.0))
        except queue.Empty:
            continue
        if line is None:
            break
        log.write(line)
        log.flush()
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return True


def watch(cmd: list[str], *, log_path: Path, timeout: float,
          feed: Callable[[str], str | None], wait_exit: float = 0) -> str:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines: "queue.Queue[str | None]" = queue.Queue()
    verdict = "timeout"
    with open(log_path, "w", encoding="utf-8") as log:
        proc = None
        pump = None
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

            def pump_lines():
                for line in proc.stdout:
                    lines.put(line)
                lines.put(None)

            pump = threading.Thread(target=pump_lines, daemon=True)
            pump.start()
            verdict = _read_until(lines, timeout, log, feed)
            if wait_exit and verdict not in ("timeout", "exited"):
                if not _await_exit(proc, lines, log, wait_exit):
                    verdict = "hung"
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                if pump is not None:
                    pump.join(timeout=5)
                proc.stdout.close()
    return verdict


def _fresh_vars(workdir: Path) -> Path:
    dst = workdir / "OVMF_VARS.fd"
    shutil.copyfile(OVMF_VARS, dst)
    return dst


def boot_test(iso: Path, firmware: str, workdir: Path, log_path: Path, *,
              timeout: float = 300, kvm: bool | None = None) -> BootResult:
    kvm = kvm_available() if kvm is None else kvm
    workdir.mkdir(parents=True, exist_ok=True)
    vars_path = _fresh_vars(workdir) if firmware == "uefi" else None
    parser = CheckParser()
    verdict = watch(qemu_command(firmware=firmware, kvm=kvm, vars_path=vars_path, cdrom=iso),
                    log_path=log_path, timeout=timeout, feed=parser.feed)
    return parser.result(verdict, log_path)


def install_test(iso: Path, firmware: str, workdir: Path, log_dir: Path, runner: Runner, *,
                 timeout: float = 300, install_timeout: float = 1800, kvm: bool | None = None) -> BootResult:
    kvm = kvm_available() if kvm is None else kvm
    workdir.mkdir(parents=True, exist_ok=True)
    kernel, initrd, disk = workdir / "vmlinuz", workdir / "initrd.img", workdir / "disk.qcow2"
    for path in (kernel, initrd, disk):
        path.unlink(missing_ok=True)
    runner.run(["xorriso", "-osirrox", "on", "-indev", str(iso),
                "-extract", "/live/vmlinuz", str(kernel), "-extract", "/live/initrd.img", str(initrd)])
    runner.run(["qemu-img", "create", "-f", "qcow2", str(disk), "8G"])
    vars_path = _fresh_vars(workdir) if firmware == "uefi" else None

    install_log = log_dir / f"install-{firmware}.log"
    verdict = watch(
        qemu_command(firmware=firmware, kvm=kvm, vars_path=vars_path, cdrom=iso, disk=disk,
                     kernel=kernel, initrd=initrd, append=f"{LIVE_CMDLINE} {INSTALL_ARGS}"),
        log_path=install_log, timeout=install_timeout,
        feed=lambda line: "installed" if INSTALL_DONE in line else None, wait_exit=POWEROFF_TIMEOUT)
    if verdict == "hung":
        return BootResult(False, f"installer finished but VM did not power off within {POWEROFF_TIMEOUT}s",
                          log_path=install_log)
    if verdict != "installed":
        return BootResult(False, f"installer did not finish ({verdict})", log_path=install_log)

    parser = CheckParser()
    boot_log = log_dir / f"installed-{firmware}.log"
    verdict = watch(qemu_command(firmware=firmware, kvm=kvm, vars_path=vars_path, disk=disk),
                    log_path=boot_log, timeout=timeout, feed=parser.feed)
    return parser.result(verdict, boot_log)
