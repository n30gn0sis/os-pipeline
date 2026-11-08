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
READY = "OSP-READY"
PASS = "OSP-CHECK-PASS "
FAIL = "OSP-CHECK-FAIL "
DONE_RE = re.compile(r"OSP-CHECKS-DONE (\d+)")
INSTALL_DONE = "OSP-INSTALL-DONE"


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
        self.passed: list[str] = []
        self.failed: list[str] = []

    def feed(self, line: str) -> str | None:
        line = line.strip()
        if READY in line:
            self.ready = True
        if PASS in line:
            self.passed.append(line.split(PASS, 1)[1])
        elif FAIL in line:
            self.failed.append(line.split(FAIL, 1)[1])
        return "done" if DONE_RE.search(line) else None

    def result(self, verdict: str, log_path: Path | None) -> BootResult:
        if verdict != "done":
            seen = "seen" if self.ready else "not seen"
            return BootResult(False, f"no completion marker ({verdict}); ready marker {seen}",
                              self.passed, self.failed, log_path)
        if self.failed:
            return BootResult(False, f"{len(self.failed)} check(s) failed", self.passed, self.failed, log_path)
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


def watch(cmd: list[str], *, log_path: Path, timeout: float,
          feed: Callable[[str], str | None], wait_exit: float = 0) -> str:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    lines: "queue.Queue[str | None]" = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    verdict = "timeout"
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    line = lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    verdict = "exited"
                    break
                log.write(line)
                log.flush()
                result = feed(line)
                if result is not None:
                    verdict = result
                    break
            if wait_exit and verdict not in ("timeout", "exited"):
                try:
                    proc.wait(timeout=wait_exit)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
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
        feed=lambda line: "installed" if INSTALL_DONE in line else None, wait_exit=120)
    if verdict != "installed":
        return BootResult(False, f"installer did not finish ({verdict})", log_path=install_log)

    parser = CheckParser()
    boot_log = log_dir / f"installed-{firmware}.log"
    verdict = watch(qemu_command(firmware=firmware, kvm=kvm, vars_path=vars_path, disk=disk),
                    log_path=boot_log, timeout=timeout, feed=parser.feed)
    return parser.result(verdict, boot_log)
