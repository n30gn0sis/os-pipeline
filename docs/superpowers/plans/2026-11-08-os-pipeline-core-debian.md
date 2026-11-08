# os-pipeline Core + Debian/Ubuntu Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `osp`, a Python CLI that turns declarative YAML profiles into bootable, testable Debian/Ubuntu live ISOs with an optional install-to-disk path.

**Architecture:** A host launcher runs everything inside a privileged builder container. A build runs four stages, each cached by a `stage.json` manifest: bootstrap (mmdebstrap, the only family-specific stage) → customize (chroot) → assemble (squashfs + GRUB + `grub-mkrescue`) → test (QEMU, serial console markers). The installer is a bash tool (`install-to-disk`) shipped inside the image, with unattended/TUI front ends plus a Calamares config.

**Tech Stack:** Python 3.11, PyYAML, jsonschema, pytest, argparse; Docker or Podman; mmdebstrap, squashfs-tools, GRUB, xorriso, QEMU + OVMF; bash, whiptail, Calamares.

**Spec:** `docs/superpowers/specs/2026-11-08-os-pipeline-core-debian-design.md`

## Global Constraints

- Python `>=3.11`. Runtime dependencies are limited to `PyYAML>=6` and `jsonschema>=4.10`; the CLI uses stdlib `argparse`. Dev dependency: `pytest>=7`.
- Builder image: `FROM docker.io/library/debian:bookworm-slim`, tagged `osp-builder:latest`.
- Architecture `amd64` only. Supported releases: Debian `bookworm`, `trixie`; Ubuntu `noble`.
- Profile `schema_version: 1`; the JSON Schema is Draft 2020-12.
- Artifact name: `<name>-<release>-<arch>-<YYYYMMDD>-<hash6>.iso`, plus `.iso.sha256` and `.build.json` alongside it.
- Secrets enter only as `env:VAR` or `file:path` references. Resolved values never appear in `stage.json`, `build.json`, or logs.
- Live kernel command line: `boot=live console=tty0 console=ttyS0,115200`.
- Boot test timeout: 300 s per boot (configurable with `--timeout`). The unattended install phase allows 1800 s.
- Serial markers: `OSP-READY`, `OSP-CHECK-PASS <cmd>`, `OSP-CHECK-FAIL <cmd>`, `OSP-CHECKS-DONE <rc>`, `OSP-INSTALL-DONE`.
- Line endings: LF everywhere except `*.ps1`, `*.bat`, `*.cmd` (already enforced by `.gitattributes`).
- Run every command from the repository root. Host commands are written for PowerShell (the user's primary shell); container commands run inside `osp-builder`.
- Commits end with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Deliberate deviations from the spec (approved design, refined during planning)

1. **State directory.** Work and cache directories live in `OSP_STATE_DIR` (`/var/lib/osp` inside the container, backed by the named volume `osp-state`), not in `output/work`. A Windows bind mount (NTFS) can't hold a Debian rootfs: no device nodes, no POSIX ownership. Published artifacts and logs still land in `output/` and `output/logs/` on the host.
2. **Customize order.** Users are created *before* files are copied, so profile files can target home directories.
3. **Installer config location.** The validated installer config is rendered to `/etc/osp/install.conf` in the rootfs, not placed loose on the ISO. Kernel command-line parameters (`osp.target=`, `osp.hostname=`, `osp.confirm=`, `osp.keep_test_marker=`) override it; the install test uses them. Profile installer configs still reject `keep_test_marker` through the schema.
4. **Install test boot.** The install phase boots the ISO's kernel/initrd directly (`qemu -kernel/-initrd/-append`) to set the installer cmdline, then reboots from the disk.
5. **`osp build`** runs boot tests (BIOS + UEFI). The install test runs only through `osp test --install`.

## File structure

```
pyproject.toml                       package metadata, pytest config
builder/Containerfile                builder image
spikes/live-boot-spike.sh            Task 3 throwaway spike
docs/superpowers/spikes/2026-11-08-live-boot.md   spike result
src/osp/__init__.py
src/osp/__main__.py                  python -m osp
src/osp/run.py                       Runner / RecordingRunner / CommandError
src/osp/launcher.py                  container launch
src/osp/profile.py                   YAML load, extends merge, Profile
src/osp/validate.py                  schema + semantic validation, load_profile
src/osp/schema/profile.schema.json
src/osp/schema/install.schema.json
src/osp/secrets.py                   secret refs → values
src/osp/buildid.py                   build hash, artifact stem
src/osp/mounts.py                    mount inspection, safe_rmtree, chroot_mounts
src/osp/stages.py                    run_stage, StageError, tool_versions
src/osp/backends/__init__.py         backend registry
src/osp/backends/debian.py           mmdebstrap bootstrap (Debian + Ubuntu)
src/osp/payload/...                  files shipped into images (installer, markers, calamares)
src/osp/customize.py                 customize stage
src/osp/assemble.py                  assemble stage
src/osp/qemu.py                      boot/install tests
src/osp/cli.py                       argparse CLI + orchestration
profiles/*.yaml, files/, hooks/, installer/   layers and example profiles
tests/...                            unit, container and boot tests
```

Test markers: `container` (needs the privileged builder container) and `boot` (boots QEMU, slow). Plain `python -m pytest` on the host deselects both.

Container test command (PowerShell, used throughout):

```powershell
docker run --rm --privileged -v "${PWD}:/work" -v osp-state:/var/lib/osp osp-builder:latest python3 -m pytest -m container -v
```

---

### Task 1: Project scaffold and command runner

**Files:**
- Create: `pyproject.toml`, `src/osp/__init__.py`, `src/osp/__main__.py`, `src/osp/run.py`
- Test: `tests/test_run.py`

**Interfaces:**
- Produces:
  - `osp.run.CommandError(cmd: Sequence[str], returncode: int, log_path: Path | None)` with attributes `.cmd`, `.returncode`, `.log_path`
  - `osp.run.Runner(log_path: Path | None = None, secrets: Sequence[str] = ())`, with:
    - `.run(cmd, *, cwd=None, env=None, input: str | None = None, check=True) -> subprocess.CompletedProcess[str]` (stdout and stderr merged into `.stdout`)
    - `.with_log(log_path) -> Runner`
    - `.redact(text) -> str`
  - `osp.run.RecordingRunner(results: Mapping[str, tuple[int, str]] | None = None)`, a `Runner` subclass with `.calls: list[list[str]]` and `.inputs: list[str | None]`. `results` is keyed by `cmd[0]`.

- [ ] **Step 1: Create the package skeleton**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "os-pipeline"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["PyYAML>=6", "jsonschema>=4.10"]

[project.optional-dependencies]
dev = ["pytest>=7"]

[project.scripts]
osp = "osp.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
osp = ["schema/*.json", "payload/**/*", "payload/*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src", "tests"]
markers = [
  "container: needs the privileged osp-builder container",
  "boot: boots an ISO in QEMU (slow)",
]
addopts = "-m 'not container and not boot'"
```

`src/osp/__init__.py`:

```python
"""os-pipeline: build purpose-built OS images from declarative profiles."""
```

`src/osp/__main__.py` (`osp.cli` arrives in Task 15; until then `python -m osp` is not expected to work):

```python
from osp.cli import main

raise SystemExit(main())
```

Install for development on the host:

```powershell
python -m pip install -e ".[dev]"
```

- [ ] **Step 2: Write the failing tests**

`tests/test_run.py`:

```python
import sys

import pytest

from osp.run import CommandError, RecordingRunner, Runner

PY = sys.executable


def test_run_captures_output_and_logs(tmp_path):
    log = tmp_path / "logs" / "x.log"
    res = Runner(log_path=log).run([PY, "-c", "print('hello')"])
    assert res.returncode == 0
    assert "hello" in res.stdout
    text = log.read_text(encoding="utf-8")
    assert text.startswith("$ ")
    assert "hello" in text


def test_log_is_appended_across_calls(tmp_path):
    log = tmp_path / "x.log"
    r = Runner(log_path=log)
    r.run([PY, "-c", "print('one')"])
    r.run([PY, "-c", "print('two')"])
    text = log.read_text(encoding="utf-8")
    assert "one" in text and "two" in text


def test_run_redacts_secrets_in_log_and_command_line(tmp_path):
    log = tmp_path / "x.log"
    Runner(log_path=log, secrets=["s3cret"]).run([PY, "-c", "print('value=s3cret')", "s3cret"])
    text = log.read_text(encoding="utf-8")
    assert "s3cret" not in text
    assert "value=***" in text


def test_run_raises_command_error_with_log_path(tmp_path):
    log = tmp_path / "x.log"
    with pytest.raises(CommandError) as ei:
        Runner(log_path=log).run([PY, "-c", "import sys; sys.exit(3)"])
    assert ei.value.returncode == 3
    assert ei.value.log_path == log


def test_run_check_false_returns_returncode():
    res = Runner().run([PY, "-c", "import sys; sys.exit(2)"], check=False)
    assert res.returncode == 2


def test_run_passes_input():
    res = Runner().run([PY, "-c", "import sys; print(sys.stdin.read().upper())"], input="abc")
    assert "ABC" in res.stdout


def test_with_log_keeps_secrets(tmp_path):
    r = Runner(secrets=["pw"]).with_log(tmp_path / "y.log")
    assert r.redact("pw!") == "***!"


def test_recording_runner_records_and_fakes_results():
    r = RecordingRunner(results={"false": (1, ""), "dpkg-query": (0, "a\t1\n")})
    r.run(["echo", "hi"], input="x")
    assert r.calls == [["echo", "hi"]]
    assert r.inputs == ["x"]
    assert r.run(["dpkg-query"]).stdout == "a\t1\n"
    with pytest.raises(CommandError):
        r.run(["false"])
    assert r.run(["false"], check=False).returncode == 1
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_run.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'osp.run'`.

- [ ] **Step 4: Implement `src/osp/run.py`**

```python
"""Single choke point for running external commands: logging, redaction, errors."""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import IO, Mapping, Sequence


class CommandError(Exception):
    def __init__(self, cmd: Sequence[str], returncode: int, log_path: Path | None):
        self.cmd = [str(c) for c in cmd]
        self.returncode = returncode
        self.log_path = log_path
        super().__init__(f"command failed (exit {returncode}): {shlex.join(self.cmd)}")


class Runner:
    def __init__(self, log_path: Path | None = None, secrets: Sequence[str] = ()):
        self.log_path = Path(log_path) if log_path else None
        self._secrets = [s for s in secrets if s]

    def with_log(self, log_path: Path) -> "Runner":
        return Runner(log_path, self._secrets)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def _open_log(self) -> IO[str] | None:
        if self.log_path is None:
            return None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        return open(self.log_path, "a", encoding="utf-8")

    def run(self, cmd, *, cwd=None, env=None, input: str | None = None, check: bool = True):
        cmd = [str(c) for c in cmd]
        log = self._open_log()
        lines: list[str] = []
        try:
            if log:
                log.write(f"$ {self.redact(shlex.join(cmd))}\n")
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            if input is not None:
                proc.stdin.write(input)
                proc.stdin.close()
            for line in proc.stdout:
                lines.append(line)
                if log:
                    log.write(self.redact(line))
            rc = proc.wait()
        finally:
            if log:
                log.close()
        if check and rc != 0:
            raise CommandError(cmd, rc, self.log_path)
        return subprocess.CompletedProcess(cmd, rc, "".join(lines), "")


class RecordingRunner(Runner):
    """Test double: records commands instead of executing them."""

    def __init__(self, results: Mapping[str, tuple[int, str]] | None = None):
        super().__init__(None)
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self._results = dict(results or {})

    def with_log(self, log_path: Path) -> "RecordingRunner":
        return self

    def run(self, cmd, *, cwd=None, env=None, input: str | None = None, check: bool = True):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        self.inputs.append(input)
        rc, out = self._results.get(cmd[0], (0, ""))
        if check and rc != 0:
            raise CommandError(cmd, rc, None)
        return subprocess.CompletedProcess(cmd, rc, out, "")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_run.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/osp/__init__.py src/osp/__main__.py src/osp/run.py tests/test_run.py
git commit -m "feat: add package scaffold and command runner"
```

---

### Task 2: Builder container and launcher

**Files:**
- Create: `builder/Containerfile`, `src/osp/launcher.py`
- Test: `tests/test_launcher.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. `FILE_MAP_ENV` is defined here and reused by `osp.secrets` in Task 6.
- Produces:
  - constants `IMAGE = "osp-builder:latest"`, `STATE_VOLUME = "osp-state"`, `CONTAINER_STATE_DIR = "/var/lib/osp"`, `IN_CONTAINER_ENV = "OSP_IN_CONTAINER"`, `FILE_MAP_ENV = "OSP_SECRET_FILE_MAP"`
  - `LauncherError(Exception)`
  - `in_container(environ: Mapping[str, str] = os.environ) -> bool`
  - `find_engine(which=shutil.which) -> str`
  - `container_command(engine: str, repo_root: Path, osp_args: list[str], env_names: list[str], secret_files: list[str], tty: bool) -> list[str]`
  - `launch(cmd: list[str]) -> int`

- [ ] **Step 1: Write the Containerfile**

`builder/Containerfile`:

```dockerfile
FROM docker.io/library/debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      mmdebstrap debian-archive-keyring ubuntu-keyring ca-certificates gpgv \
      squashfs-tools xorriso mtools grub-common grub-pc-bin grub-efi-amd64-bin \
      qemu-system-x86 qemu-utils ovmf \
      python3 python3-yaml python3-jsonschema python3-pytest \
      bash util-linux procps \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONPATH=/work/src \
    OSP_IN_CONTAINER=1 \
    OSP_STATE_DIR=/var/lib/osp
WORKDIR /work
```

- [ ] **Step 2: Build the image and check the toolchain**

```powershell
docker build -t osp-builder:latest -f builder/Containerfile builder
```

```powershell
docker run --rm osp-builder:latest sh -c "mmdebstrap --version && grub-mkrescue --version && qemu-system-x86_64 --version && python3 -c 'import yaml, jsonschema; print(jsonschema.__version__)' && ls /usr/share/OVMF /usr/share/keyrings"
```

Expected: versions print without error, the jsonschema version is ≥ 4.10, `/usr/share/OVMF` contains `OVMF_CODE_4M.fd` and `OVMF_VARS_4M.fd`, and `/usr/share/keyrings` contains `ubuntu-archive-keyring.gpg`. **If the OVMF file names differ, note the actual names. Task 3 and Task 14 must use them.**

- [ ] **Step 3: Write the failing tests**

`tests/test_launcher.py`:

```python
import json
from pathlib import Path

import pytest

from osp.launcher import (
    CONTAINER_STATE_DIR,
    FILE_MAP_ENV,
    IMAGE,
    STATE_VOLUME,
    LauncherError,
    container_command,
    find_engine,
    in_container,
)


def test_in_container_reads_env():
    assert in_container({"OSP_IN_CONTAINER": "1"}) is True
    assert in_container({}) is False


def test_find_engine_prefers_docker_then_podman():
    assert find_engine(lambda n: "/x/docker" if n == "docker" else None) == "docker"
    assert find_engine(lambda n: "/x/podman" if n == "podman" else None) == "podman"
    with pytest.raises(LauncherError, match="docker or podman"):
        find_engine(lambda n: None)


def test_container_command_mounts_repo_and_state(tmp_path):
    cmd = container_command("docker", tmp_path, ["build", "profiles/x.yaml"], [], [], tty=False)
    assert cmd[:4] == ["docker", "run", "--rm", "--privileged"]
    assert cmd[cmd.index(f"{tmp_path}:/work") - 1] == "-v"
    assert f"{STATE_VOLUME}:{CONTAINER_STATE_DIR}" in cmd
    assert "-it" not in cmd
    assert cmd[cmd.index(IMAGE):] == [IMAGE, "python3", "-m", "osp", "build", "profiles/x.yaml"]


def test_container_command_tty():
    assert "-it" in container_command("podman", Path("/r"), ["test", "a.iso"], [], [], tty=True)


def test_container_command_forwards_env_by_name_only(tmp_path):
    cmd = container_command("docker", tmp_path, ["build", "p.yaml"], ["OSP_ADMIN_SSH_KEY"], [], tty=False)
    i = cmd.index("OSP_ADMIN_SSH_KEY")
    assert cmd[i - 1] == "-e"


def test_container_command_mounts_secret_files_read_only(tmp_path):
    secret = tmp_path / "key.pub"
    secret.write_text("ssh-ed25519 AAAA")
    cmd = container_command("docker", tmp_path, ["build", "p.yaml"], [], [str(secret)], tty=False)
    assert f"{secret.resolve()}:/run/osp-secrets/0:ro" in cmd
    env_arg = next(a for a in cmd if a.startswith(f"{FILE_MAP_ENV}="))
    assert json.loads(env_arg.split("=", 1)[1]) == {str(secret): "/run/osp-secrets/0"}
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `python -m pytest tests/test_launcher.py -v`
Expected: `ModuleNotFoundError: No module named 'osp.launcher'`.

- [ ] **Step 5: Implement `src/osp/launcher.py`**

```python
"""Host side: run osp commands inside the privileged builder container."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Mapping

IMAGE = "osp-builder:latest"
STATE_VOLUME = "osp-state"
CONTAINER_STATE_DIR = "/var/lib/osp"
IN_CONTAINER_ENV = "OSP_IN_CONTAINER"
FILE_MAP_ENV = "OSP_SECRET_FILE_MAP"


class LauncherError(Exception):
    pass


def in_container(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(IN_CONTAINER_ENV) == "1"


def find_engine(which: Callable[[str], str | None] = shutil.which) -> str:
    for engine in ("docker", "podman"):
        if which(engine):
            return engine
    raise LauncherError("neither docker nor podman found on PATH")


def container_command(
    engine: str,
    repo_root: Path,
    osp_args: list[str],
    env_names: list[str],
    secret_files: list[str],
    tty: bool,
) -> list[str]:
    cmd = [engine, "run", "--rm", "--privileged",
           "-v", f"{repo_root}:/work",
           "-v", f"{STATE_VOLUME}:{CONTAINER_STATE_DIR}",
           "-w", "/work"]
    if tty:
        cmd.append("-it")
    for name in env_names:
        cmd += ["-e", name]  # value is taken from the host env, never on the command line
    file_map: dict[str, str] = {}
    for i, original in enumerate(secret_files):
        host_path = Path(original).expanduser().resolve()
        target = f"/run/osp-secrets/{i}"
        cmd += ["-v", f"{host_path}:{target}:ro"]
        file_map[original] = target
    if file_map:
        cmd += ["-e", f"{FILE_MAP_ENV}={json.dumps(file_map)}"]
    return cmd + [IMAGE, "python3", "-m", "osp", *osp_args]


def launch(cmd: list[str]) -> int:
    rc = subprocess.call(cmd)
    if rc == 125:  # docker/podman: the container itself failed to start
        raise LauncherError(
            "could not start the builder container. Build the image with:\n"
            "  docker build -t osp-builder:latest -f builder/Containerfile builder\n"
            "and make sure privileged containers are allowed."
        )
    return rc
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_launcher.py -v`
Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add builder/Containerfile src/osp/launcher.py tests/test_launcher.py
git commit -m "feat: add builder container and launcher"
```

---

### Task 3: Spike — live-boot on Debian and Ubuntu

The spec names Ubuntu-on-`live-boot` as the main technical risk. Prove it before building the pipeline around it. This is throwaway code: no TDD, just one script and a recorded result.

**Files:**
- Create: `spikes/live-boot-spike.sh`, `docs/superpowers/spikes/2026-11-08-live-boot.md`

**Interfaces:** none (this task produces a go/no-go decision).

- [ ] **Step 1: Write the spike script**

`spikes/live-boot-spike.sh`:

```bash
#!/bin/bash
# Spike: does a minimal rootfs boot via live-boot from a grub-mkrescue ISO (BIOS + UEFI)?
# Usage (inside osp-builder): bash spikes/live-boot-spike.sh debian bookworm
#                             bash spikes/live-boot-spike.sh ubuntu noble
set -euo pipefail
flavor=$1 release=$2
work=/var/lib/osp/spike/$flavor-$release
rm -rf "$work"
mkdir -p "$work/iso/live" "$work/iso/boot/grub"

case $flavor in
  debian) mirror="deb http://deb.debian.org/debian $release main"; kernel=linux-image-amd64; keyring=() ;;
  ubuntu) mirror="deb http://archive.ubuntu.com/ubuntu $release main universe"; kernel=linux-image-generic
          keyring=(--keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg) ;;
  *) echo "unknown flavor $flavor" >&2; exit 2 ;;
esac

mmdebstrap --mode=root --variant=minbase --arch=amd64 "${keyring[@]}" \
  --include="systemd-sysv,udev,live-boot,initramfs-tools,$kernel" \
  --customize-hook='chroot "$1" sh -c "echo root:spike | chpasswd"' \
  "$release" "$work/rootfs" "$mirror"

cp "$work"/rootfs/boot/vmlinuz-* "$work/iso/live/vmlinuz"
cp "$work"/rootfs/boot/initrd.img-* "$work/iso/live/initrd.img"
mksquashfs "$work/rootfs" "$work/iso/live/filesystem.squashfs" -comp xz -noappend
cat > "$work/iso/boot/grub/grub.cfg" <<'EOF'
set timeout=1
serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial
menuentry "Live" {
  linux /live/vmlinuz boot=live console=tty0 console=ttyS0,115200
  initrd /live/initrd.img
}
EOF
grub-mkrescue -o "$work/spike.iso" "$work/iso"

for fw in bios uefi; do
  args=(-m 2048 -smp 2 -display none -serial stdio -monitor none -no-reboot -cdrom "$work/spike.iso")
  if [ -w /dev/kvm ]; then args+=(-enable-kvm -cpu host); else args+=(-cpu max); fi
  if [ "$fw" = uefi ]; then
    cp /usr/share/OVMF/OVMF_VARS_4M.fd "$work/vars.fd"
    args+=(-drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd
           -drive if=pflash,format=raw,file="$work/vars.fd")
  fi
  qemu-system-x86_64 "${args[@]}" < /dev/null > "$work/$fw.log" 2>&1 &
  pid=$!
  result=FAIL
  for _ in $(seq 1 600); do
    if grep -q "login:" "$work/$fw.log"; then result=PASS; break; fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  echo "SPIKE $flavor/$release/$fw: $result (log: $work/$fw.log)"
done
```

- [ ] **Step 2: Run the Debian baseline**

```powershell
docker run --rm --privileged -v "${PWD}:/work" -v osp-state:/var/lib/osp osp-builder:latest bash spikes/live-boot-spike.sh debian bookworm
```

Expected: `SPIKE debian/bookworm/bios: PASS` and `SPIKE debian/bookworm/uefi: PASS`. Without KVM this takes several minutes.

- [ ] **Step 3: Run the Ubuntu risk case**

```powershell
docker run --rm --privileged -v "${PWD}:/work" -v osp-state:/var/lib/osp osp-builder:latest bash spikes/live-boot-spike.sh ubuntu noble
```

Expected: PASS for both. **If either Ubuntu run FAILs:** copy the last 60 lines of the log (`docker run --rm -v osp-state:/var/lib/osp osp-builder:latest tail -n 60 /var/lib/osp/spike/ubuntu-noble/bios.log`) into the spike doc, **stop, and report to the user.** The spec's fallback is a flavor-specific initramfs/boot-parameter branch inside `osp.assemble`, which needs a spec amendment before Task 13.

- [ ] **Step 4: Record the result**

`docs/superpowers/spikes/2026-11-08-live-boot.md`: fill the table with the observed results and timing.

```markdown
# Spike: live-boot from grub-mkrescue ISO (2026-11-08)

Script: `spikes/live-boot-spike.sh`. Minimal rootfs (systemd-sysv, udev, live-boot, initramfs-tools, kernel), xz squashfs, grub-mkrescue hybrid ISO, QEMU serial console, success = `login:` prompt.

| Flavor/release | BIOS | UEFI | KVM? | Notes |
|---|---|---|---|---|
| debian/bookworm | PASS/FAIL | PASS/FAIL | yes/no | |
| ubuntu/noble | PASS/FAIL | PASS/FAIL | yes/no | |

Decision: shared live-boot assemble path for both flavors — GO / NO-GO (if NO-GO: reason and proposed amendment).
```

- [ ] **Step 5: Commit**

```bash
git add spikes/live-boot-spike.sh docs/superpowers/spikes/2026-11-08-live-boot.md
git commit -m "spike: verify live-boot ISO boots for Debian and Ubuntu"
```

---

### Task 4: Profile loading, inheritance, and the `Profile` model

**Files:**
- Create: `src/osp/profile.py`, `tests/helpers.py`
- Test: `tests/test_profile.py`

**Interfaces:**
- Produces:
  - `ProfileError(Exception)`
  - `merge(base: dict, override: dict) -> dict`. Lists concatenate with order-preserving de-duplication, nested dicts merge recursively, and anything else is overridden. Neither input is mutated.
  - `load_merged(path: Path, profiles_dir: Path) -> dict`. Resolves `extends` (a name means `profiles_dir/<name>.yaml`, merged left→right, then the child). The `extends` key is removed from the result.
  - `Profile(data: dict, source: Path)` (frozen dataclass) with read-only properties:
    - `name`, `family`, `flavor`, `release`, `arch`: `str`
    - `packages`, `services_enable`, `services_disable`, `hooks`, `checks`: `list[str]`
    - `files`, `users`: `list[dict]`
    - `persistence`, `autologin`: `bool`
    - `autologin_user`: `str | None`, the first user's name when autologin is on
    - `installer_mode`: `str`, default `"none"`
    - `installer_config`: `str | None`
    - `desktop`: `dict | None`
  - `tests/helpers.py`: `BASE` (a minimal valid profile dict), `profile_data(**overrides) -> dict` (a deep copy of `BASE` with `overrides` applied), `write_yaml(path, data) -> Path`

- [ ] **Step 1: Write the test helpers**

`tests/helpers.py`:

```python
import copy
from pathlib import Path

import yaml

BASE = {
    "schema_version": 1,
    "name": "t",
    "distro": {"family": "debian", "flavor": "debian", "release": "bookworm", "arch": "amd64"},
}


def profile_data(**overrides) -> dict:
    data = copy.deepcopy(BASE)
    data.update(copy.deepcopy(overrides))
    return data


def write_yaml(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path
```

- [ ] **Step 2: Write the failing tests**

`tests/test_profile.py`:

```python
from pathlib import Path

import pytest

from helpers import profile_data, write_yaml
from osp.profile import Profile, ProfileError, load_merged, merge


def test_merge_lists_concatenate_and_dedupe():
    assert merge({"a": [1, 2]}, {"a": [2, 3]}) == {"a": [1, 2, 3]}


def test_merge_dedupes_dict_items():
    u = {"name": "x"}
    assert merge({"users": [u]}, {"users": [dict(u)]}) == {"users": [u]}


def test_merge_scalars_override_and_dicts_recurse():
    assert merge({"s": 1, "d": {"x": 1, "y": 2}}, {"s": 2, "d": {"y": 3}}) == {"s": 2, "d": {"x": 1, "y": 3}}


def test_merge_does_not_mutate_inputs():
    a, b = {"l": [1], "d": {"x": 1}}, {"l": [2], "d": {"y": 2}}
    merge(a, b)
    assert a == {"l": [1], "d": {"x": 1}} and b == {"l": [2], "d": {"y": 2}}


def test_load_merged_resolves_extends_left_to_right(tmp_path):
    pdir = tmp_path / "profiles"
    write_yaml(pdir / "a.yaml", {"name": "a", "packages": ["a"], "live": {"autologin": False}})
    write_yaml(pdir / "b.yaml", {"name": "b", "packages": ["b"], "live": {"autologin": True}})
    child = write_yaml(pdir / "child.yaml", {"name": "child", "extends": ["a", "b"], "packages": ["c"]})
    data = load_merged(child, pdir)
    assert data["packages"] == ["a", "b", "c"]
    assert data["live"]["autologin"] is True
    assert data["name"] == "child"
    assert "extends" not in data


def test_load_merged_is_transitive_and_accepts_string_extends(tmp_path):
    pdir = tmp_path / "profiles"
    write_yaml(pdir / "a.yaml", {"packages": ["a"]})
    write_yaml(pdir / "b.yaml", {"extends": "a", "packages": ["b"]})
    child = write_yaml(pdir / "c.yaml", {"extends": ["b"], "packages": ["c"]})
    assert load_merged(child, pdir)["packages"] == ["a", "b", "c"]


def test_extends_cycle_raises(tmp_path):
    pdir = tmp_path / "profiles"
    write_yaml(pdir / "a.yaml", {"extends": ["b"]})
    write_yaml(pdir / "b.yaml", {"extends": ["a"]})
    with pytest.raises(ProfileError, match="cycle"):
        load_merged(pdir / "a.yaml", pdir)


def test_missing_parent_raises(tmp_path):
    pdir = tmp_path / "profiles"
    child = write_yaml(pdir / "c.yaml", {"extends": ["nope"]})
    with pytest.raises(ProfileError, match="not found"):
        load_merged(child, pdir)


def test_non_mapping_profile_raises(tmp_path):
    p = tmp_path / "profiles" / "x.yaml"
    p.parent.mkdir()
    p.write_text("- just\n- a list\n")
    with pytest.raises(ProfileError, match="mapping"):
        load_merged(p, p.parent)


def test_profile_accessors_and_defaults():
    p = Profile(profile_data(), Path("x.yaml"))
    assert (p.name, p.family, p.flavor, p.release, p.arch) == ("t", "debian", "debian", "bookworm", "amd64")
    assert p.packages == [] and p.files == [] and p.users == [] and p.hooks == [] and p.checks == []
    assert p.services_enable == [] and p.services_disable == []
    assert p.installer_mode == "none" and p.installer_config is None
    assert p.persistence is False and p.autologin is False and p.autologin_user is None
    assert p.desktop is None


def test_profile_autologin_user_is_first_user():
    p = Profile(profile_data(users=[{"name": "a"}, {"name": "b"}], live={"autologin": True}), Path("x"))
    assert p.autologin_user == "a"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_profile.py -v`
Expected: `ModuleNotFoundError: No module named 'osp.profile'`.

- [ ] **Step 4: Implement `src/osp/profile.py`**

```python
"""Profile files: YAML loading, `extends` inheritance and the Profile model."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class ProfileError(Exception):
    pass


def _dedupe(items: list) -> list:
    out: list = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = merge(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            out[key] = _dedupe(current + value)
        else:
            out[key] = value
    return out


def load_merged(path: Path, profiles_dir: Path) -> dict:
    return _load(Path(path), Path(profiles_dir), ())


def _load(path: Path, profiles_dir: Path, chain: tuple[Path, ...]) -> dict:
    if not path.is_file():
        raise ProfileError(f"profile not found: {path}")
    key = path.resolve()
    if key in chain:
        names = " -> ".join(p.stem for p in (*chain, key))
        raise ProfileError(f"extends cycle: {names}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: top level must be a mapping")
    parents = data.pop("extends", None) or []
    if isinstance(parents, str):
        parents = [parents]
    merged: dict = {}
    for name in parents:
        merged = merge(merged, _load(profiles_dir / f"{name}.yaml", profiles_dir, (*chain, key)))
    return merge(merged, data)


@dataclass(frozen=True)
class Profile:
    data: dict
    source: Path

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def family(self) -> str:
        return self.data["distro"]["family"]

    @property
    def flavor(self) -> str:
        return self.data["distro"]["flavor"]

    @property
    def release(self) -> str:
        return self.data["distro"]["release"]

    @property
    def arch(self) -> str:
        return self.data["distro"]["arch"]

    @property
    def packages(self) -> list[str]:
        return list(self.data.get("packages", []))

    @property
    def files(self) -> list[dict]:
        return list(self.data.get("files", []))

    @property
    def users(self) -> list[dict]:
        return list(self.data.get("users", []))

    @property
    def services_enable(self) -> list[str]:
        return list(self.data.get("services", {}).get("enable", []))

    @property
    def services_disable(self) -> list[str]:
        return list(self.data.get("services", {}).get("disable", []))

    @property
    def hooks(self) -> list[str]:
        return list(self.data.get("hooks", {}).get("customize", []))

    @property
    def persistence(self) -> bool:
        return bool(self.data.get("live", {}).get("persistence", False))

    @property
    def autologin(self) -> bool:
        return bool(self.data.get("live", {}).get("autologin", False))

    @property
    def autologin_user(self) -> str | None:
        if self.autologin and self.users:
            return self.users[0]["name"]
        return None

    @property
    def installer_mode(self) -> str:
        return self.data.get("installer", {}).get("mode", "none")

    @property
    def installer_config(self) -> str | None:
        return self.data.get("installer", {}).get("config")

    @property
    def checks(self) -> list[str]:
        return list(self.data.get("test", {}).get("checks", []))

    @property
    def desktop(self) -> dict | None:
        return self.data.get("desktop")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_profile.py -v`
Expected: 11 passed.

- [ ] **Step 6: Commit**

```bash
git add src/osp/profile.py tests/helpers.py tests/test_profile.py
git commit -m "feat: load profiles with extends inheritance"
```

---

### Task 5: Schema and semantic validation

**Files:**
- Create: `src/osp/schema/profile.schema.json`, `src/osp/schema/install.schema.json`, `src/osp/validate.py`, `src/osp/backends/__init__.py`, `src/osp/backends/debian.py` (only `SUPPORTED` for now; Task 9 adds the rest)
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `osp.profile.load_merged`, `Profile`, `ProfileError` (Task 4); `tests/helpers.profile_data`, `write_yaml`.
- Produces:
  - `osp.validate.validate(data: dict, repo_root: Path) -> list[str]`. Each error is formatted `"<location>: <message>"`; an empty list means the profile is valid.
  - `osp.validate.load_profile(path, repo_root) -> Profile`. Raises `ProfileError`, with all errors listed, when invalid.
  - `osp.validate.load_install_config(path: Path) -> dict`
  - `osp.backends.supported() -> dict[str, dict[str, set[str]]]` (family → flavor → releases)
  - `osp.backends.get_backend(family: str)` (returns the backend module)
  - `osp.backends.debian.SUPPORTED = {"debian": {"bookworm", "trixie"}, "ubuntu": {"noble"}}`

- [ ] **Step 1: Write the failing tests**

`tests/test_validate.py`:

```python
import pytest

from helpers import profile_data, write_yaml
from osp.profile import ProfileError
from osp.validate import load_profile, validate


def errs(data, repo):
    return "\n".join(validate(data, repo))


def test_minimal_profile_is_valid(tmp_path):
    assert validate(profile_data(), tmp_path) == []


def test_schema_error_reports_location(tmp_path):
    assert "packages:" in errs(profile_data(packages="vim"), tmp_path)


def test_unknown_top_level_key_rejected(tmp_path):
    assert "bogus" in errs(profile_data(bogus=1), tmp_path)


def test_secret_fields_require_references(tmp_path):
    out = errs(profile_data(users=[{"name": "a", "ssh_keys_from": "ssh-ed25519 AAAA"}]), tmp_path)
    assert "users/0/ssh_keys_from" in out


def test_unknown_family(tmp_path):
    d = profile_data()
    d["distro"]["family"] = "gentoo"
    assert "unknown backend" in errs(d, tmp_path)


def test_unsupported_release(tmp_path):
    d = profile_data()
    d["distro"]["release"] = "sid"
    assert "unsupported" in errs(d, tmp_path)


def test_calamares_requires_desktop(tmp_path):
    assert "requires a desktop" in errs(profile_data(installer={"mode": "calamares"}), tmp_path)


def test_unattended_requires_config(tmp_path):
    assert "installer/config: required" in errs(profile_data(installer={"mode": "unattended"}), tmp_path)


def test_unattended_config_requires_target_disk(tmp_path):
    write_yaml(tmp_path / "installer" / "i.yaml", {"hostname": "h"})
    out = errs(profile_data(installer={"mode": "unattended", "config": "installer/i.yaml"}), tmp_path)
    assert "target_disk is required" in out


def test_install_config_rejects_keep_test_marker(tmp_path):
    write_yaml(tmp_path / "installer" / "i.yaml", {"target_disk": "/dev/sda", "keep_test_marker": True})
    out = errs(profile_data(installer={"mode": "unattended", "config": "installer/i.yaml"}), tmp_path)
    assert "keep_test_marker" in out


def test_install_config_missing_file(tmp_path):
    out = errs(profile_data(installer={"mode": "tui", "config": "installer/nope.yaml"}), tmp_path)
    assert "file not found" in out


def test_valid_unattended(tmp_path):
    write_yaml(tmp_path / "installer" / "i.yaml", {"target_disk": "/dev/sda", "hostname": "h", "confirm": True})
    assert validate(profile_data(installer={"mode": "unattended", "config": "installer/i.yaml"}), tmp_path) == []


def test_duplicate_user_names(tmp_path):
    out = errs(profile_data(users=[{"name": "a"}, {"name": "a", "groups": ["sudo"]}]), tmp_path)
    assert "duplicate user" in out


def test_autologin_requires_user(tmp_path):
    assert "autologin" in errs(profile_data(live={"autologin": True}), tmp_path)


def test_missing_file_and_hook_sources(tmp_path):
    out = errs(profile_data(files=[{"src": "files/x", "dest": "/etc/x"}], hooks={"customize": ["hooks/h.sh"]}), tmp_path)
    assert "files/x" in out and "hooks/h.sh" in out


def test_load_profile_raises_with_all_errors(tmp_path):
    p = write_yaml(tmp_path / "profiles" / "bad.yaml", profile_data(live={"autologin": True}, installer={"mode": "calamares"}))
    with pytest.raises(ProfileError) as ei:
        load_profile(p, tmp_path)
    assert "autologin" in str(ei.value) and "desktop" in str(ei.value)


def test_load_profile_returns_profile(tmp_path):
    p = write_yaml(tmp_path / "profiles" / "ok.yaml", profile_data())
    assert load_profile(p, tmp_path).name == "t"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_validate.py -v`
Expected: `ModuleNotFoundError: No module named 'osp.validate'`.

- [ ] **Step 3: Write the schemas**

`src/osp/schema/profile.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://os-pipeline.local/schema/profile/1",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "name", "distro"],
  "$defs": {
    "secretRef": {"type": "string", "pattern": "^(env:[A-Za-z_][A-Za-z0-9_]*|file:.+)$"},
    "strList": {"type": "array", "items": {"type": "string", "minLength": 1}}
  },
  "properties": {
    "schema_version": {"const": 1},
    "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
    "distro": {
      "type": "object",
      "additionalProperties": false,
      "required": ["family", "flavor", "release", "arch"],
      "properties": {
        "family": {"type": "string"},
        "flavor": {"type": "string"},
        "release": {"type": "string"},
        "arch": {"enum": ["amd64"]},
        "mirror": {"type": "string", "pattern": "^https?://"},
        "components": {"$ref": "#/$defs/strList"}
      }
    },
    "packages": {"$ref": "#/$defs/strList"},
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["src", "dest"],
        "properties": {"src": {"type": "string"}, "dest": {"type": "string", "pattern": "^/"}}
      }
    },
    "users": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["name"],
        "properties": {
          "name": {"type": "string", "pattern": "^[a-z_][a-z0-9_-]*$"},
          "groups": {"$ref": "#/$defs/strList"},
          "shell": {"type": "string", "pattern": "^/"},
          "ssh_keys_from": {"$ref": "#/$defs/secretRef"},
          "password_hash_from": {"$ref": "#/$defs/secretRef"}
        }
      }
    },
    "services": {
      "type": "object",
      "additionalProperties": false,
      "properties": {"enable": {"$ref": "#/$defs/strList"}, "disable": {"$ref": "#/$defs/strList"}}
    },
    "hooks": {
      "type": "object",
      "additionalProperties": false,
      "properties": {"customize": {"$ref": "#/$defs/strList"}}
    },
    "live": {
      "type": "object",
      "additionalProperties": false,
      "properties": {"persistence": {"type": "boolean"}, "autologin": {"type": "boolean"}}
    },
    "desktop": {
      "type": "object",
      "additionalProperties": false,
      "required": ["display_manager"],
      "properties": {"display_manager": {"enum": ["lightdm"]}}
    },
    "installer": {
      "type": "object",
      "additionalProperties": false,
      "required": ["mode"],
      "properties": {
        "mode": {"enum": ["unattended", "tui", "calamares", "none"]},
        "config": {"type": "string"}
      }
    },
    "test": {
      "type": "object",
      "additionalProperties": false,
      "properties": {"checks": {"$ref": "#/$defs/strList"}}
    }
  }
}
```

`src/osp/schema/install.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://os-pipeline.local/schema/install/1",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "target_disk": {"type": "string", "pattern": "^/dev/[A-Za-z0-9/_-]+$"},
    "hostname": {"type": "string", "pattern": "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"},
    "confirm": {"type": "boolean"}
  }
}
```

- [ ] **Step 4: Implement the backend registry stub**

`src/osp/backends/debian.py`:

```python
"""Debian-family backend: bootstraps Debian and Ubuntu root filesystems with mmdebstrap."""
from __future__ import annotations

SUPPORTED: dict[str, set[str]] = {"debian": {"bookworm", "trixie"}, "ubuntu": {"noble"}}
```

`src/osp/backends/__init__.py`:

```python
"""Backend registry. A backend module exposes SUPPORTED, bootstrap_key(profile) and
bootstrap(profile, rootfs, runner)."""
from __future__ import annotations

from types import ModuleType

from osp.backends import debian

_BACKENDS: dict[str, ModuleType] = {"debian": debian}


def get_backend(family: str) -> ModuleType:
    try:
        return _BACKENDS[family]
    except KeyError:
        raise KeyError(f"unknown backend {family!r}") from None


def supported() -> dict[str, dict[str, set[str]]]:
    return {family: module.SUPPORTED for family, module in _BACKENDS.items()}
```

- [ ] **Step 5: Implement `src/osp/validate.py`**

```python
"""Profile validation: JSON Schema first, then cross-field and filesystem rules."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from osp.backends import supported
from osp.profile import Profile, ProfileError, load_merged


def _schema(name: str) -> dict:
    return json.loads(resources.files("osp").joinpath("schema", name).read_text(encoding="utf-8"))


def _schema_errors(schema_name: str, data, prefix: str = "") -> list[str]:
    validator = Draft202012Validator(_schema(schema_name))
    out = []
    for e in sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path]):
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        out.append(f"{prefix}{loc}: {e.message}")
    return out


def load_install_config(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def validate(data: dict, repo_root: Path) -> list[str]:
    errors = _schema_errors("profile.schema.json", data)
    if errors:
        return errors
    return _semantic_errors(data, Path(repo_root))


def _semantic_errors(data: dict, repo_root: Path) -> list[str]:
    errors: list[str] = []
    distro = data["distro"]
    flavors = supported().get(distro["family"])
    if flavors is None:
        errors.append(f"distro/family: unknown backend {distro['family']!r}")
    elif distro["release"] not in flavors.get(distro["flavor"], set()):
        options = ", ".join(f"{f}/{r}" for f, rs in sorted(flavors.items()) for r in sorted(rs))
        errors.append(f"distro: unsupported {distro['flavor']}/{distro['release']} (supported: {options})")

    names = [u["name"] for u in data.get("users", [])]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        errors.append(f"users: duplicate user names {dups}")
    if data.get("live", {}).get("autologin") and not names:
        errors.append("live/autologin: requires at least one user")

    installer = data.get("installer", {"mode": "none"})
    mode, config = installer["mode"], installer.get("config")
    if mode == "calamares" and "desktop" not in data:
        errors.append("installer/mode: calamares requires a desktop layer (a profile providing 'desktop')")
    if mode == "unattended" and not config:
        errors.append("installer/config: required when mode is unattended")
    if config:
        path = repo_root / config
        if not path.is_file():
            errors.append(f"installer/config: file not found: {config}")
        else:
            cfg = load_install_config(path)
            cfg_errors = _schema_errors("install.schema.json", cfg, prefix=f"installer/config ({config}) ")
            errors += cfg_errors
            if not cfg_errors and mode == "unattended" and "target_disk" not in cfg:
                errors.append(f"installer/config ({config}): target_disk is required for unattended mode")

    for f in data.get("files", []):
        if not (repo_root / f["src"]).exists():
            errors.append(f"files: source not found: {f['src']}")
    for hook in data.get("hooks", {}).get("customize", []):
        if not (repo_root / hook).is_file():
            errors.append(f"hooks/customize: script not found: {hook}")
    return errors


def load_profile(path, repo_root) -> Profile:
    repo_root = Path(repo_root)
    data = load_merged(Path(path), repo_root / "profiles")
    errors = validate(data, repo_root)
    if errors:
        raise ProfileError(f"invalid profile {path}:\n  " + "\n  ".join(errors))
    return Profile(data=data, source=Path(path))
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_validate.py -v`
Expected: 17 passed.

- [ ] **Step 7: Commit**

```bash
git add src/osp/schema src/osp/validate.py src/osp/backends tests/test_validate.py
git commit -m "feat: validate profiles against schema and semantic rules"
```

---

### Task 6: Secret resolution

**Files:**
- Create: `src/osp/secrets.py`
- Test: `tests/test_secrets.py`

**Interfaces:**
- Consumes: `osp.profile.Profile` (Task 4), `osp.launcher.FILE_MAP_ENV` (Task 2).
- Produces:
  - `SecretError(Exception)`
  - `SECRET_FIELDS = ("ssh_keys_from", "password_hash_from")`
  - `collect_refs(profile: Profile) -> list[str]` (unique, in order of appearance)
  - `env_names(refs) -> list[str]`, `file_paths(refs) -> list[str]`
  - `resolve(refs, environ: Mapping[str, str], file_map: Mapping[str, str] | None = None) -> dict[str, str]`. The result maps each ref to its value (file contents are stripped). All missing secrets are reported together in one `SecretError`.
  - `file_map_from_env(environ) -> dict[str, str]`
  - `secret_strings(secrets: Mapping[str, str]) -> list[str]`: every non-blank line of every value, for `Runner` redaction

- [ ] **Step 1: Write the failing tests**

`tests/test_secrets.py`:

```python
import json
from pathlib import Path

import pytest

from helpers import profile_data
from osp.launcher import FILE_MAP_ENV
from osp.profile import Profile
from osp.secrets import (
    SecretError,
    collect_refs,
    env_names,
    file_map_from_env,
    file_paths,
    resolve,
    secret_strings,
)


def make(users):
    return Profile(profile_data(users=users), Path("x"))


def test_collect_refs_unique_in_order():
    p = make([
        {"name": "a", "ssh_keys_from": "env:K", "password_hash_from": "file:/s/pw"},
        {"name": "b", "ssh_keys_from": "env:K"},
    ])
    assert collect_refs(p) == ["env:K", "file:/s/pw"]
    assert env_names(collect_refs(p)) == ["K"]
    assert file_paths(collect_refs(p)) == ["/s/pw"]


def test_resolve_env_and_file(tmp_path):
    f = tmp_path / "pw"
    f.write_text("$6$hash\n")
    out = resolve(["env:K", f"file:{f}"], {"K": "ssh-ed25519 AAAA"})
    assert out == {"env:K": "ssh-ed25519 AAAA", f"file:{f}": "$6$hash"}


def test_resolve_uses_file_map(tmp_path):
    real = tmp_path / "mounted"
    real.write_text("v")
    assert resolve(["file:~/orig"], {}, {"~/orig": str(real)}) == {"file:~/orig": "v"}


def test_resolve_reports_all_missing():
    with pytest.raises(SecretError) as ei:
        resolve(["env:A", "env:B", "file:/nope/x"], {"A": ""})
    msg = str(ei.value)
    assert "A" in msg and "B" in msg and "/nope/x" in msg


def test_file_map_from_env():
    assert file_map_from_env({FILE_MAP_ENV: json.dumps({"a": "/run/osp-secrets/0"})}) == {"a": "/run/osp-secrets/0"}
    assert file_map_from_env({}) == {}


def test_secret_strings_splits_lines():
    assert secret_strings({"r": "key1\n\nkey2\n"}) == ["key1", "key2"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_secrets.py -v`
Expected: `ModuleNotFoundError: No module named 'osp.secrets'`.

- [ ] **Step 3: Implement `src/osp/secrets.py`**

```python
"""Resolve `env:`/`file:` secret references at build time. Values never go into manifests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

from osp.launcher import FILE_MAP_ENV
from osp.profile import Profile

SECRET_FIELDS = ("ssh_keys_from", "password_hash_from")


class SecretError(Exception):
    pass


def collect_refs(profile: Profile) -> list[str]:
    refs: list[str] = []
    for user in profile.users:
        for field in SECRET_FIELDS:
            ref = user.get(field)
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def env_names(refs: Iterable[str]) -> list[str]:
    return [r[len("env:"):] for r in refs if r.startswith("env:")]


def file_paths(refs: Iterable[str]) -> list[str]:
    return [r[len("file:"):] for r in refs if r.startswith("file:")]


def resolve(refs: Iterable[str], environ: Mapping[str, str],
            file_map: Mapping[str, str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    missing: list[str] = []
    for ref in refs:
        kind, _, value = ref.partition(":")
        if kind == "env":
            if environ.get(value):
                out[ref] = environ[value]
            else:
                missing.append(f"environment variable {value}")
        else:
            path = Path((file_map or {}).get(value, value)).expanduser()
            if path.is_file():
                out[ref] = path.read_text(encoding="utf-8").strip()
            else:
                missing.append(f"file {value}")
    if missing:
        raise SecretError("missing secrets: " + ", ".join(missing))
    return out


def file_map_from_env(environ: Mapping[str, str]) -> dict[str, str]:
    return json.loads(environ.get(FILE_MAP_ENV, "{}"))


def secret_strings(secrets: Mapping[str, str]) -> list[str]:
    return [line.strip() for value in secrets.values() for line in value.splitlines() if line.strip()]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_secrets.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/osp/secrets.py tests/test_secrets.py
git commit -m "feat: resolve env and file secret references"
```

---

### Task 7: Build hash and artifact naming

**Files:**
- Create: `src/osp/buildid.py`
- Test: `tests/test_buildid.py`

**Interfaces:**
- Consumes: `Profile` (Task 4).
- Produces:
  - `referenced_paths(profile) -> list[str]`: repo-relative paths of file sources, hooks, and the installer config
  - `compute_build_hash(profile, repo_root: Path, secrets: Mapping[str, str]) -> str` (64 hex chars). It covers the merged profile, the content of every referenced path (directories are walked), a sha256 of each secret value (never the value itself), and the shipped `osp/payload` tree.
  - `artifact_stem(profile, build_hash: str, day: date) -> str` = `f"{name}-{release}-{arch}-{day:%Y%m%d}-{build_hash[:6]}"`

- [ ] **Step 1: Write the failing tests**

`tests/test_buildid.py`:

```python
from datetime import date
from pathlib import Path

from helpers import profile_data
from osp.buildid import artifact_stem, compute_build_hash, referenced_paths
from osp.profile import Profile


def make(repo: Path) -> Profile:
    (repo / "files" / "d").mkdir(parents=True)
    (repo / "files" / "d" / "a.conf").write_text("a=1")
    (repo / "hooks").mkdir()
    (repo / "hooks" / "h.sh").write_text("echo hi")
    return Profile(profile_data(
        files=[{"src": "files/d/", "dest": "/etc/d/"}],
        hooks={"customize": ["hooks/h.sh"]},
    ), repo / "profiles" / "t.yaml")


def test_referenced_paths(tmp_path):
    assert referenced_paths(make(tmp_path)) == ["files/d/", "hooks/h.sh"]


def test_hash_is_stable(tmp_path):
    p = make(tmp_path)
    assert compute_build_hash(p, tmp_path, {}) == compute_build_hash(p, tmp_path, {})
    assert len(compute_build_hash(p, tmp_path, {})) == 64


def test_hash_changes_with_referenced_file_content(tmp_path):
    p = make(tmp_path)
    before = compute_build_hash(p, tmp_path, {})
    (tmp_path / "files" / "d" / "a.conf").write_text("a=2")
    assert compute_build_hash(p, tmp_path, {}) != before


def test_hash_changes_with_secret_value_but_does_not_contain_it(tmp_path):
    p = make(tmp_path)
    h1 = compute_build_hash(p, tmp_path, {"env:K": "one"})
    h2 = compute_build_hash(p, tmp_path, {"env:K": "two"})
    assert h1 != h2


def test_artifact_stem(tmp_path):
    p = make(tmp_path)
    assert artifact_stem(p, "abcdef0123", date(2026, 11, 8)) == "t-bookworm-amd64-20261108-abcdef"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_buildid.py -v`
Expected: `ModuleNotFoundError: No module named 'osp.buildid'`.

- [ ] **Step 3: Implement `src/osp/buildid.py`**

```python
"""Deterministic build hash (cache key + artifact suffix) and artifact naming."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Mapping

from osp.profile import Profile


def referenced_paths(profile: Profile) -> list[str]:
    paths = [f["src"] for f in profile.files] + profile.hooks
    if profile.installer_config:
        paths.append(profile.installer_config)
    return paths


def _files_under(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file())
    return [path] if path.is_file() else []


def _update_with_traversable(h, node, prefix: str) -> None:
    if node.is_dir():
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            _update_with_traversable(h, child, f"{prefix}/{child.name}")
    elif node.is_file():
        h.update(prefix.encode() + b"\0" + node.read_bytes() + b"\0")


def compute_build_hash(profile: Profile, repo_root: Path, secrets: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(profile.data, sort_keys=True).encode())
    for rel in sorted(referenced_paths(profile)):
        for f in _files_under(repo_root / rel):
            h.update(f.relative_to(repo_root).as_posix().encode() + b"\0")
            h.update(f.read_bytes() + b"\0")
    for ref in sorted(secrets):
        h.update(ref.encode() + b"\0" + hashlib.sha256(secrets[ref].encode()).digest())
    payload = resources.files("osp").joinpath("payload")
    if payload.is_dir():
        _update_with_traversable(h, payload, "payload")
    return h.hexdigest()


def artifact_stem(profile: Profile, build_hash: str, day: date) -> str:
    return f"{profile.name}-{profile.release}-{profile.arch}-{day:%Y%m%d}-{build_hash[:6]}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_buildid.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/osp/buildid.py tests/test_buildid.py
git commit -m "feat: compute build hash and artifact names"
```

---

### Task 8: Stage runner and mount safety

**Files:**
- Create: `src/osp/mounts.py`, `src/osp/stages.py`
- Test: `tests/test_mounts.py`, `tests/test_stages.py`

**Interfaces:**
- Consumes: `Runner`, `RecordingRunner`, `CommandError` (Task 1).
- Produces:
  - `osp.mounts.mounts_under(path: Path, mounts_file: Path = Path("/proc/self/mounts")) -> list[str]`
  - `osp.mounts.safe_rmtree(path: Path, mounts_file=...) -> None`. Raises `RuntimeError` if anything is still mounted below `path`.
  - `osp.mounts.chroot_mounts(rootfs: Path, runner: Runner)` context manager. It mounts proc, sysfs, `/dev` (bind) and devpts, and always unmounts them in reverse order, falling back to `umount -l`.
  - `osp.stages.STAGES = ("bootstrap", "customize", "assemble", "test")`
  - `osp.stages.StageError(message: str, log_path: Path | None = None)` with attribute `.log_path`
  - `osp.stages.stages_to_force(from_stage: str | None) -> set[str]`
  - `osp.stages.run_stage(name, key, out_dir: Path, fn: Callable[[Path], None], *, force=False, keep_failed=True, log_path=None, echo=print) -> Path`
  - `osp.stages.tool_versions() -> dict[str, str]`

- [ ] **Step 1: Write the failing tests**

`tests/test_mounts.py`:

```python
import pytest

from osp.mounts import chroot_mounts, mounts_under, safe_rmtree
from osp.run import RecordingRunner


def fake_mounts(tmp_path, *mountpoints):
    f = tmp_path / "mounts"
    f.write_text("".join(f"x {m} ext4 rw 0 0\n" for m in mountpoints))
    return f


def test_mounts_under_matches_path_and_children_only(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    r = str(root.resolve())
    mf = fake_mounts(tmp_path, f"{r}/proc", f"{r}2/x", "/other")
    assert mounts_under(root, mf) == [f"{r}/proc"]


def test_mounts_under_without_mounts_file(tmp_path):
    assert mounts_under(tmp_path, tmp_path / "missing") == []


def test_safe_rmtree_refuses_when_mounted(tmp_path):
    root = tmp_path / "r"
    (root / "proc").mkdir(parents=True)
    mf = fake_mounts(tmp_path, f"{root.resolve()}/proc")
    with pytest.raises(RuntimeError, match="still mounted"):
        safe_rmtree(root, mf)
    assert root.exists()


def test_safe_rmtree_removes(tmp_path):
    root = tmp_path / "r"
    (root / "a").mkdir(parents=True)
    safe_rmtree(root, tmp_path / "none")
    assert not root.exists()


def test_chroot_mounts_order_and_cleanup_on_error(tmp_path):
    runner = RecordingRunner()
    with pytest.raises(ValueError):
        with chroot_mounts(tmp_path, runner):
            raise ValueError("boom")
    mounts = [c for c in runner.calls if c[0] == "mount"]
    umounts = [c for c in runner.calls if c[0] == "umount"]
    assert [c[-1] for c in mounts] == [str(tmp_path / p) for p in ("proc", "sys", "dev", "dev/pts")]
    assert [c[-1] for c in umounts] == [str(tmp_path / p) for p in ("dev/pts", "dev", "sys", "proc")]


def test_chroot_mounts_lazy_fallback(tmp_path):
    runner = RecordingRunner(results={"umount": (1, "busy")})
    with chroot_mounts(tmp_path, runner):
        pass
    assert ["umount", "-l", str(tmp_path / "proc")] in runner.calls
```

`tests/test_stages.py`:

```python
import json

import pytest

from osp.run import CommandError
from osp.stages import StageError, run_stage, stages_to_force


def quiet(_msg):
    pass


def test_stages_to_force():
    assert stages_to_force(None) == set()
    assert stages_to_force("customize") == {"customize", "assemble", "test"}
    with pytest.raises(ValueError):
        stages_to_force("nope")


def test_run_stage_runs_and_writes_manifest(tmp_path):
    out = tmp_path / "s"
    calls = []
    run_stage("bootstrap", "k1", out, lambda d: calls.append(d), echo=quiet)
    assert calls == [out]
    m = json.loads((out / "stage.json").read_text())
    assert m["stage"] == "bootstrap" and m["key"] == "k1" and "completed" in m and "tool_versions" in m


def test_run_stage_uses_cache_for_same_key(tmp_path):
    out = tmp_path / "s"
    run_stage("bootstrap", "k1", out, lambda d: None, echo=quiet)
    calls = []
    run_stage("bootstrap", "k1", out, lambda d: calls.append(d), echo=quiet)
    assert calls == []


def test_run_stage_reruns_on_new_key_or_force_and_clears_dir(tmp_path):
    out = tmp_path / "s"
    run_stage("bootstrap", "k1", out, lambda d: (d / "old").write_text("x"), echo=quiet)
    run_stage("bootstrap", "k2", out, lambda d: None, echo=quiet)
    assert not (out / "old").exists()
    calls = []
    run_stage("bootstrap", "k2", out, lambda d: calls.append(d), force=True, echo=quiet)
    assert calls == [out]


def test_failed_stage_wraps_error_and_keeps_dir(tmp_path):
    out = tmp_path / "s"
    log = tmp_path / "b.log"

    def boom(d):
        (d / "partial").write_text("x")
        raise CommandError(["mmdebstrap"], 1, log)

    with pytest.raises(StageError) as ei:
        run_stage("bootstrap", "k", out, boom, echo=quiet)
    assert "stage 'bootstrap' failed" in str(ei.value)
    assert ei.value.log_path == log
    assert (out / "partial").exists() and not (out / "stage.json").exists()


def test_failed_stage_removed_without_keep_failed(tmp_path):
    out = tmp_path / "s"

    def boom(d):
        raise RuntimeError("x")

    with pytest.raises(StageError) as ei:
        run_stage("customize", "k", out, boom, keep_failed=False, log_path=tmp_path / "c.log", echo=quiet)
    assert not out.exists()
    assert ei.value.log_path == tmp_path / "c.log"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_mounts.py tests/test_stages.py -v`
Expected: `ModuleNotFoundError` for `osp.mounts` and `osp.stages`.

- [ ] **Step 3: Implement `src/osp/mounts.py`**

```python
"""Mount helpers. Nothing under a work directory is deleted while something is mounted in it."""
from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path

from osp.run import Runner

PROC_MOUNTS = Path("/proc/self/mounts")


def mounts_under(path: Path, mounts_file: Path = PROC_MOUNTS) -> list[str]:
    if not mounts_file.exists():
        return []
    root = str(Path(path).resolve()).rstrip("/")
    found = []
    for line in mounts_file.read_text().splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        mountpoint = fields[1].replace("\\040", " ")
        if mountpoint == root or mountpoint.startswith(root + "/"):
            found.append(mountpoint)
    return found


def safe_rmtree(path: Path, mounts_file: Path = PROC_MOUNTS) -> None:
    still = mounts_under(path, mounts_file)
    if still:
        raise RuntimeError(f"refusing to delete {path}: still mounted: {still}")
    shutil.rmtree(path)


_CHROOT_MOUNTS = (
    (["-t", "proc", "proc"], "proc"),
    (["-t", "sysfs", "sysfs"], "sys"),
    (["--bind", "/dev"], "dev"),
    (["-t", "devpts", "devpts"], "dev/pts"),
)


@contextmanager
def chroot_mounts(rootfs: Path, runner: Runner):
    mounted: list[Path] = []
    try:
        for args, rel in _CHROOT_MOUNTS:
            target = Path(rootfs) / rel
            target.mkdir(parents=True, exist_ok=True)
            runner.run(["mount", *args, str(target)])
            mounted.append(target)
        yield
    finally:
        for target in reversed(mounted):
            if runner.run(["umount", str(target)], check=False).returncode != 0:
                runner.run(["umount", "-l", str(target)], check=False)
```

- [ ] **Step 4: Implement `src/osp/stages.py`**

```python
"""Stage execution with stage.json manifests for caching and resume."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from osp.mounts import safe_rmtree

STAGES = ("bootstrap", "customize", "assemble", "test")
TOOL_PACKAGES = ("mmdebstrap", "squashfs-tools", "xorriso", "grub-common", "qemu-system-x86")


class StageError(Exception):
    def __init__(self, message: str, log_path: Path | None = None):
        super().__init__(message)
        self.log_path = log_path


def stages_to_force(from_stage: str | None) -> set[str]:
    if from_stage is None:
        return set()
    if from_stage not in STAGES:
        raise ValueError(f"unknown stage {from_stage!r}; expected one of {STAGES}")
    return set(STAGES[STAGES.index(from_stage):])


def tool_versions() -> dict[str, str]:
    if shutil.which("dpkg-query") is None:
        return {}
    res = subprocess.run(["dpkg-query", "-W", "-f=${Package}\t${Version}\n", *TOOL_PACKAGES],
                         capture_output=True, text=True)
    return dict(line.split("\t", 1) for line in res.stdout.splitlines() if "\t" in line)


def run_stage(name: str, key: str, out_dir: Path, fn: Callable[[Path], None], *,
              force: bool = False, keep_failed: bool = True, log_path: Path | None = None,
              echo: Callable[[str], None] = print) -> Path:
    out_dir = Path(out_dir)
    manifest = out_dir / "stage.json"
    if not force and manifest.is_file():
        previous = json.loads(manifest.read_text())
        if previous.get("stage") == name and previous.get("key") == key:
            echo(f"[{name}] cached: {out_dir}")
            return out_dir
    if out_dir.exists():
        safe_rmtree(out_dir)
    out_dir.mkdir(parents=True)
    echo(f"[{name}] running")
    try:
        fn(out_dir)
    except BaseException as e:
        if not keep_failed and out_dir.exists():
            safe_rmtree(out_dir)
        if isinstance(e, Exception):
            raise StageError(f"stage '{name}' failed: {e}",
                             getattr(e, "log_path", None) or log_path) from e
        raise
    manifest.write_text(json.dumps({
        "stage": name,
        "key": key,
        "completed": datetime.now(timezone.utc).isoformat(),
        "tool_versions": tool_versions(),
    }, indent=2))
    echo(f"[{name}] done")
    return out_dir
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_mounts.py tests/test_stages.py -v`
Expected: 12 passed.

- [ ] **Step 6: Commit**

```bash
git add src/osp/mounts.py src/osp/stages.py tests/test_mounts.py tests/test_stages.py
git commit -m "feat: add cached stage runner and mount safety"
```

---

### Task 9: Debian/Ubuntu bootstrap backend

**Files:**
- Modify: `src/osp/backends/debian.py` (extend the Task 5 stub)
- Test: `tests/test_backend_debian.py`

**Interfaces:**
- Consumes: `Profile` (Task 4), `Runner`, `RecordingRunner` (Task 1); `tests/helpers.profile_data`.
- Produces (module `osp.backends.debian`):
  - `SUPPORTED` (unchanged)
  - `Flavor` dataclass and `FLAVORS: dict[str, Flavor]`
  - `BASE_PACKAGES: tuple[str, ...]`
  - `sources(profile) -> list[str]` (full one-line `deb …` entries: release, `-updates`, `-security`)
  - `bootstrap_packages(profile) -> list[str]`
  - `bootstrap_key(profile) -> str` (16 hex chars)
  - `bootstrap(profile, rootfs: Path, runner: Runner) -> None` (`rootfs` must not exist yet)

- [ ] **Step 1: Write the failing tests**

`tests/test_backend_debian.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_backend_debian.py -v`
Expected: `AttributeError: module 'osp.backends.debian' has no attribute 'sources'`.

- [ ] **Step 3: Implement the backend**

Replace `src/osp/backends/debian.py` with:

```python
"""Debian-family backend: bootstraps Debian and Ubuntu root filesystems with mmdebstrap."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from osp.profile import Profile
from osp.run import Runner

SUPPORTED: dict[str, set[str]] = {"debian": {"bookworm", "trixie"}, "ubuntu": {"noble"}}


@dataclass(frozen=True)
class Flavor:
    mirror: str
    security_mirror: str
    components: tuple[str, ...]
    kernel: str
    keyring: str | None


FLAVORS: dict[str, Flavor] = {
    "debian": Flavor("http://deb.debian.org/debian", "http://security.debian.org/debian-security",
                     ("main", "non-free-firmware"), "linux-image-amd64", None),
    "ubuntu": Flavor("http://archive.ubuntu.com/ubuntu", "http://security.ubuntu.com/ubuntu",
                     ("main", "universe"), "linux-image-generic",
                     "/usr/share/keyrings/ubuntu-archive-keyring.gpg"),
}

# Everything a live image needs to boot and to install itself with GRUB (BIOS + UEFI).
BASE_PACKAGES = (
    "systemd-sysv", "udev", "kmod", "initramfs-tools", "live-boot",
    "grub-pc-bin", "grub-efi-amd64-bin", "grub2-common",
    "sudo", "locales", "ca-certificates",
)


def sources(profile: Profile) -> list[str]:
    flavor = FLAVORS[profile.flavor]
    distro = profile.data["distro"]
    mirror = distro.get("mirror", flavor.mirror)
    comps = " ".join(distro.get("components", flavor.components))
    r = profile.release
    return [
        f"deb {mirror} {r} {comps}",
        f"deb {mirror} {r}-updates {comps}",
        f"deb {flavor.security_mirror} {r}-security {comps}",
    ]


def bootstrap_packages(profile: Profile) -> list[str]:
    return [*BASE_PACKAGES, FLAVORS[profile.flavor].kernel]


def bootstrap_key(profile: Profile) -> str:
    material = {
        "flavor": profile.flavor,
        "release": profile.release,
        "arch": profile.arch,
        "sources": sources(profile),
        "packages": sorted(bootstrap_packages(profile)),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def bootstrap(profile: Profile, rootfs: Path, runner: Runner) -> None:
    flavor = FLAVORS[profile.flavor]
    cmd = ["mmdebstrap", "--mode=root", "--variant=minbase", f"--arch={profile.arch}",
           f"--include={','.join(bootstrap_packages(profile))}"]
    if flavor.keyring:
        cmd.append(f"--keyring={flavor.keyring}")
    cmd += [profile.release, str(rootfs), *sources(profile)]
    runner.run(cmd)
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `python -m pytest tests/test_backend_debian.py -v`
Expected: 5 passed, 1 deselected (the container test).

- [ ] **Step 5: Run the container test**

```powershell
docker run --rm --privileged -v "${PWD}:/work" -v osp-state:/var/lib/osp osp-builder:latest python3 -m pytest -m container tests/test_backend_debian.py -v
```

Expected: `test_bootstrap_bookworm_creates_live_capable_rootfs PASSED` (a few minutes; it needs network).

- [ ] **Step 6: Commit**

```bash
git add src/osp/backends/debian.py tests/test_backend_debian.py
git commit -m "feat: bootstrap Debian and Ubuntu rootfs with mmdebstrap"
```

---

### Task 10: Image payload — readiness marker, installer services, `install-to-disk`

These files ship inside images. Task 12 copies them into the rootfs.

**Files:**
- Create in `src/osp/payload/`: `osp-ready`, `osp-ready.service`, `osp-installer.service`, `osp-installer-tui.service`, `grub-serial.cfg`, `install-to-disk`
- Test: `tests/test_payload.py`

**Interfaces:**
- Produces:
  - `osp-ready` (bash). Writes `OSP-READY`, one `OSP-CHECK-PASS|FAIL <cmd>` line per line of `/etc/osp/checks`, then `OSP-CHECKS-DONE <0|1>`. Each check is retried up to `OSP_CHECK_TRIES` (default 30) times, 2 s apart. Test overrides: `OSP_READY_OUT` (default `/dev/ttyS0`), `OSP_CHECKS_FILE`, `OSP_CHECK_SLEEP`.
  - `install-to-disk --unattended|--tui` (bash). Defines `part DISK N` and `apply_cmdline "<cmdline>"`, and runs `main` only when executed, not when sourced. Reads `/etc/osp/install.conf` (`OSP_TARGET_DISK`, `OSP_HOSTNAME`, `OSP_CONFIRM`) and `/etc/osp/live-packages`. Prints `osp-install: OSP-INSTALL-DONE` when it succeeds.
  - Units: `osp-ready.service` and `osp-installer.service` (enabled when installer mode is `unattended`; `ConditionKernelCommandLine=osp.install=unattended`). `osp-installer-tui.service` (enabled for `tui`; `ConditionKernelCommandLine=osp.install=tui`).

- [ ] **Step 1: Write the failing tests**

`tests/test_payload.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_payload.py -v`
Expected: `test_payload_files_exist` FAILS (`osp-ready` missing).

- [ ] **Step 3: Write the readiness marker**

`src/osp/payload/osp-ready`:

```bash
#!/bin/bash
# osp-ready: print boot-test markers and check results on the serial console.
set -u
out=${OSP_READY_OUT:-/dev/ttyS0}
checks=${OSP_CHECKS_FILE:-/etc/osp/checks}
tries=${OSP_CHECK_TRIES:-30}
pause=${OSP_CHECK_SLEEP:-2}
[ -w "$out" ] || [ -n "${OSP_READY_OUT:-}" ] || out=/dev/console

{
  echo "OSP-READY"
  rc=0
  if [ -f "$checks" ]; then
    while IFS= read -r check || [ -n "$check" ]; do
      [ -z "$check" ] && continue
      ok=0
      for ((i = 1; i <= tries; i++)); do
        if bash -c "$check" >/dev/null 2>&1; then ok=1; break; fi
        [ "$i" -lt "$tries" ] && sleep "$pause"
      done
      if [ "$ok" = 1 ]; then echo "OSP-CHECK-PASS $check"; else echo "OSP-CHECK-FAIL $check"; rc=1; fi
    done < "$checks"
  fi
  echo "OSP-CHECKS-DONE $rc"
} >> "$out" 2>&1
exit 0
```

`src/osp/payload/osp-ready.service`:

```ini
[Unit]
Description=os-pipeline boot-test readiness marker
After=systemd-user-sessions.service

[Service]
Type=oneshot
ExecStart=/usr/local/lib/osp/osp-ready
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

`src/osp/payload/grub-serial.cfg` (installed into `/etc/default/grub.d/` so installed systems keep a serial console):

```sh
# Added by os-pipeline: mirror the console on the first serial port.
GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX console=tty0 console=ttyS0,115200"
GRUB_TERMINAL="console serial"
GRUB_SERIAL_COMMAND="serial --unit=0 --speed=115200"
```

- [ ] **Step 4: Write the installer units**

`src/osp/payload/osp-installer.service`:

```ini
[Unit]
Description=os-pipeline unattended installer
ConditionKernelCommandLine=osp.install=unattended
After=systemd-user-sessions.service
Conflicts=getty@tty1.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/install-to-disk --unattended
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
```

`src/osp/payload/osp-installer-tui.service`:

```ini
[Unit]
Description=os-pipeline text-mode installer
ConditionKernelCommandLine=osp.install=tui
After=systemd-user-sessions.service
Conflicts=getty@tty1.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/install-to-disk --tui
StandardInput=tty
StandardOutput=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 5: Write `install-to-disk`**

`src/osp/payload/install-to-disk`:

```bash
#!/bin/bash
# install-to-disk: install the running os-pipeline live system onto a disk.
# Usage: install-to-disk --unattended | --tui
# Config: /etc/osp/install.conf, overridable by kernel cmdline
#   osp.target= osp.hostname= osp.confirm= osp.keep_test_marker=
set -euo pipefail

CONF=/etc/osp/install.conf
SQUASHFS=/run/live/medium/live/filesystem.squashfs
TARGET_ROOT=/target
OSP_TARGET_DISK=""
OSP_HOSTNAME=""
OSP_CONFIRM=1
OSP_KEEP_TEST_MARKER=0

log() { echo "osp-install: $*"; }
die() { echo "osp-install: ERROR: $*" >&2; exit 1; }

part() {  # part DISK N -> partition device
  if [[ "$1" =~ [0-9]$ ]]; then echo "${1}p$2"; else echo "$1$2"; fi
}

apply_cmdline() {
  local word
  for word in $1; do
    case "$word" in
      osp.target=*) OSP_TARGET_DISK="${word#*=}" ;;
      osp.hostname=*) OSP_HOSTNAME="${word#*=}" ;;
      osp.confirm=*) OSP_CONFIRM="${word#*=}" ;;
      osp.keep_test_marker=*) OSP_KEEP_TEST_MARKER="${word#*=}" ;;
    esac
  done
}

load_config() {
  if [ -f "$CONF" ]; then
    # shellcheck source=/dev/null
    . "$CONF"
  fi
  apply_cmdline "$(cat /proc/cmdline)"
}

live_medium_disk() {
  local src pk
  src=$(findmnt -no SOURCE /run/live/medium 2>/dev/null || true)
  [ -n "$src" ] || return 0
  pk=$(lsblk -no PKNAME "$src" 2>/dev/null | head -n1 || true)
  if [ -n "$pk" ]; then echo "/dev/$pk"; else echo "$src"; fi
}

choose_tui() {
  local live items=() name size model
  live=$(live_medium_disk)
  while read -r name size model; do
    [ "$name" = "$live" ] && continue
    items+=("$name" "$size ${model:-disk}")
  done < <(lsblk -dnpo NAME,SIZE,MODEL -e 7,11)
  [ ${#items[@]} -gt 0 ] || die "no installable disks found"
  OSP_TARGET_DISK=$(whiptail --title "os-pipeline installer" --menu \
    "Install to which disk? ALL DATA ON IT WILL BE ERASED." 20 70 10 "${items[@]}" 3>&1 1>&2 2>&3) || die "cancelled"
  OSP_HOSTNAME=$(whiptail --inputbox "Hostname" 8 60 "${OSP_HOSTNAME:-$(hostname)}" 3>&1 1>&2 2>&3) || die "cancelled"
  whiptail --yesno "Erase $OSP_TARGET_DISK and install?" 8 60 || die "cancelled"
  OSP_CONFIRM=0
}

confirm() {
  [ "$OSP_CONFIRM" = "0" ] && return 0
  local answer
  { echo; echo "osp-install will ERASE $OSP_TARGET_DISK. Type YES to continue:"; } > /dev/tty1
  read -r answer < /dev/tty1
  [ "$answer" = "YES" ] || die "not confirmed"
}

partition() {
  local d=$1
  sgdisk --zap-all "$d"
  sgdisk -n1:0:+1M -t1:EF02 -c1:bios -n2:0:+512M -t2:EF00 -c2:esp -n3:0:0 -t3:8300 -c3:root "$d"
  udevadm settle
  mkfs.vfat -F32 -n ESP "$(part "$d" 2)"
  mkfs.ext4 -F -L root "$(part "$d" 3)"
}

copy_system() {
  local d=$1 m
  mkdir -p "$TARGET_ROOT"
  mount "$(part "$d" 3)" "$TARGET_ROOT"
  unsquashfs -f -d "$TARGET_ROOT" "$SQUASHFS"
  mkdir -p "$TARGET_ROOT/boot/efi"
  mount "$(part "$d" 2)" "$TARGET_ROOT/boot/efi"
  for m in dev dev/pts proc sys run; do mount --bind "/$m" "$TARGET_ROOT/$m"; done
}

configure_target() {
  local d=$1 root_uuid esp_uuid
  root_uuid=$(blkid -s UUID -o value "$(part "$d" 3)")
  esp_uuid=$(blkid -s UUID -o value "$(part "$d" 2)")
  cat > "$TARGET_ROOT/etc/fstab" <<EOF
UUID=$root_uuid / ext4 errors=remount-ro 0 1
UUID=$esp_uuid /boot/efi vfat umask=0077 0 1
EOF
  if [ -n "$OSP_HOSTNAME" ]; then
    echo "$OSP_HOSTNAME" > "$TARGET_ROOT/etc/hostname"
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$OSP_HOSTNAME/" "$TARGET_ROOT/etc/hosts"
  fi
  # shellcheck disable=SC2046
  chroot "$TARGET_ROOT" env DEBIAN_FRONTEND=noninteractive apt-get -y purge $(cat "$TARGET_ROOT/etc/osp/live-packages")
  rm -f "$TARGET_ROOT/usr/local/sbin/install-to-disk" \
        "$TARGET_ROOT"/etc/systemd/system/osp-installer*.service \
        "$TARGET_ROOT"/etc/systemd/system/multi-user.target.wants/osp-installer*.service
  if [ "$OSP_KEEP_TEST_MARKER" != "1" ]; then
    rm -f "$TARGET_ROOT/etc/systemd/system/osp-ready.service" \
          "$TARGET_ROOT/etc/systemd/system/multi-user.target.wants/osp-ready.service" \
          "$TARGET_ROOT/usr/local/lib/osp/osp-ready"
  fi
  chroot "$TARGET_ROOT" update-initramfs -u -k all
  chroot "$TARGET_ROOT" grub-install --target=i386-pc "$d"
  chroot "$TARGET_ROOT" grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable --no-nvram
  chroot "$TARGET_ROOT" update-grub
}

cleanup() {
  local m
  for m in run sys proc dev/pts dev boot/efi; do umount "$TARGET_ROOT/$m" 2>/dev/null || true; done
  umount "$TARGET_ROOT" 2>/dev/null || true
}

main() {
  local mode=${1:-} live
  case "$mode" in --unattended|--tui) ;; *) die "usage: install-to-disk --unattended|--tui" ;; esac
  [ "$(id -u)" = 0 ] || die "must run as root"
  load_config
  if [ "$mode" = --tui ]; then choose_tui; fi
  [ -n "$OSP_TARGET_DISK" ] || die "no target disk configured"
  [ -b "$OSP_TARGET_DISK" ] || die "target is not a block device: $OSP_TARGET_DISK"
  live=$(live_medium_disk)
  [ "$OSP_TARGET_DISK" != "$live" ] || die "refusing to install onto the live medium ($live)"
  confirm
  trap cleanup EXIT
  log "partitioning $OSP_TARGET_DISK"; partition "$OSP_TARGET_DISK"
  log "copying system"; copy_system "$OSP_TARGET_DISK"
  log "configuring target"; configure_target "$OSP_TARGET_DISK"
  cleanup
  trap - EXIT
  log "OSP-INSTALL-DONE"
  if [ "$mode" = --unattended ]; then
    systemctl poweroff
  else
    whiptail --msgbox "Installed. Remove the live medium and reboot." 8 60
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
```

- [ ] **Step 6: Run the tests**

Host: `python -m pytest tests/test_payload.py -v`. Expected on Windows: 3 passed, 5 skipped (the bash tests). On Linux: all passed.

The bash tests also have to run in the container:

```powershell
docker run --rm -v "${PWD}:/work" osp-builder:latest python3 -m pytest tests/test_payload.py -v
```

Expected: 8 passed (2 parametrized syntax checks included).

- [ ] **Step 7: Commit**

```bash
git add src/osp/payload tests/test_payload.py
git commit -m "feat: add readiness marker, installer services and install-to-disk"
```

---

### Task 11: Calamares payload

**Files:**
- Create in `src/osp/payload/`: `calamares/settings.conf`, `calamares/modules/unpackfs.conf`, `calamares/modules/packages.conf`, `calamares/modules/shellprocess_removelive.conf`, `calamares/modules/bootloader.conf`, `calamares/modules/displaymanager.conf`, `calamares/modules/users.conf`, `osp-calamares.desktop`, `49-osp-live.rules`
- Test: `tests/test_payload_calamares.py`

**Interfaces:**
- Produces: a `calamares/` tree that Task 12 copies to `/etc/calamares/` (calamares mode only); `osp-calamares.desktop` → `/etc/xdg/autostart/`; `49-osp-live.rules` → `/etc/polkit-1/rules.d/`. Calamares starts automatically only when the kernel cmdline contains `osp.install=calamares`.

- [ ] **Step 1: Write the failing tests**

`tests/test_payload_calamares.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_payload_calamares.py -v`
Expected: FAIL with `FileNotFoundError` for `settings.conf`.

- [ ] **Step 3: Write the Calamares configuration**

`src/osp/payload/calamares/settings.conf`:

```yaml
---
modules-search: [ local, /usr/lib/x86_64-linux-gnu/calamares/modules ]

instances:
  - id: removelive
    module: shellprocess
    config: shellprocess_removelive.conf

sequence:
  - show:
      - welcome
      - locale
      - keyboard
      - partition
      - users
      - summary
  - exec:
      - partition
      - mount
      - unpackfs
      - machineid
      - fstab
      - locale
      - keyboard
      - localecfg
      - users
      - displaymanager
      - networkcfg
      - hwclock
      - packages
      - shellprocess@removelive
      - initramfs
      - grubcfg
      - bootloader
      - umount
  - show:
      - finished

branding: default
prompt-install: true
dont-chroot: false
```

`src/osp/payload/calamares/modules/unpackfs.conf`:

```yaml
---
unpack:
  - source: /run/live/medium/live/filesystem.squashfs
    sourcefs: squashfs
    destination: ""
```

`src/osp/payload/calamares/modules/packages.conf`:

```yaml
---
backend: apt
skip_if_no_internet: false
update_db: false
operations:
  - remove:
      - live-boot
      - live-boot-initramfs-tools
      - calamares
```

`src/osp/payload/calamares/modules/shellprocess_removelive.conf`:

```yaml
---
dontChroot: false
timeout: 120
script:
  - "rm -f /usr/local/sbin/install-to-disk /etc/xdg/autostart/osp-calamares.desktop /etc/polkit-1/rules.d/49-osp-live.rules /etc/lightdm/lightdm.conf.d/50-osp-autologin.conf"
  - "rm -f /etc/systemd/system/osp-ready.service /etc/systemd/system/multi-user.target.wants/osp-ready.service /usr/local/lib/osp/osp-ready"
  - "rm -rf /etc/osp /etc/calamares"
```

`src/osp/payload/calamares/modules/bootloader.conf`:

```yaml
---
efiBootLoader: "grub"
kernelSearchPath: "/boot"
kernelPattern: "^vmlinuz.*"
loaderEntries: []
grubInstall: "grub-install"
grubMkconfig: "grub-mkconfig"
grubCfg: "/boot/grub/grub.cfg"
grubProbe: "grub-probe"
efiBootMgr: "efibootmgr"
installEFIFallback: true
```

`src/osp/payload/calamares/modules/displaymanager.conf`:

```yaml
---
displaymanagers:
  - lightdm
basicSetup: false
```

`src/osp/payload/calamares/modules/users.conf`:

```yaml
---
defaultGroups:
  - users
  - sudo
  - audio
  - video
sudoersGroup: sudo
setRootPassword: false
doAutologin: false
```

`src/osp/payload/osp-calamares.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Install os-pipeline image
Exec=sh -c 'grep -qw osp.install=calamares /proc/cmdline && pkexec calamares'
NoDisplay=true
X-GNOME-Autostart-enabled=true
```

`src/osp/payload/49-osp-live.rules`:

```javascript
// Live session only (removed on install): let the sudo group start Calamares without a password.
polkit.addRule(function (action, subject) {
  if (action.id == "com.github.calamares.calamares.pkexec.run" &&
      subject.isInGroup("sudo") && subject.local && subject.active) {
    return polkit.Result.YES;
  }
});
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_payload_calamares.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/osp/payload/calamares src/osp/payload/osp-calamares.desktop src/osp/payload/49-osp-live.rules tests/test_payload_calamares.py
git commit -m "feat: add Calamares installer configuration"
```

---

### Task 12: Customize stage

**Files:**
- Create: `src/osp/customize.py`
- Test: `tests/test_customize.py`

**Interfaces:**
- Consumes:
  - `Profile` (Task 4); `load_install_config` (Task 5)
  - `Runner`, `RecordingRunner`, `CommandError` (Task 1)
  - `chroot_mounts` (Task 8); `StageError` (Task 8)
  - payload files (Tasks 10–11)
  - `osp.backends.debian.bootstrap` (Task 9, container test only)
- Produces (module `osp.customize`):
  - `INSTALLER_PACKAGES: dict[str, list[str]]`, `LIVE_PACKAGES: list[str]`, `UNITS: dict[str, list[str]]`
  - `chroot(runner, rootfs, *cmd, input=None)`
  - `rootfs_path(rootfs: Path, dest: str) -> Path`. Returns a writable path inside the rootfs: parents are created, a symlink at the target is replaced, and `StageError` is raised if the target would escape the rootfs through a symlink.
  - `install_packages(profile, rootfs, runner)`
  - `create_users(profile, secrets, rootfs, runner)`
  - `copy_files(profile, repo_root, rootfs)`
  - `set_services(profile, rootfs, runner)`
  - `render_install_conf(cfg: dict) -> str`
  - `install_payload(profile, repo_root, rootfs, runner)`
  - `configure_live(profile, rootfs)`
  - `run_hooks(profile, repo_root, rootfs, runner)`
  - `finalize(rootfs, runner)`
  - `customize(profile, secrets: Mapping[str, str], src_rootfs: Path, rootfs: Path, repo_root: Path, runner) -> None`

Order inside the chroot (deviation 2): packages → users → files → services → payload → live config → hooks → finalize.

- [ ] **Step 1: Write the failing tests**

`tests/test_customize.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_customize.py -v`
Expected: `ImportError: cannot import name 'customize' from 'osp'`.

- [ ] **Step 3: Implement `src/osp/customize.py`**

```python
"""Customize stage: turn a bootstrapped rootfs into the profile's live system."""
from __future__ import annotations

import os
import shlex
import shutil
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Mapping

from osp.mounts import chroot_mounts
from osp.profile import Profile
from osp.run import CommandError, Runner
from osp.stages import StageError
from osp.validate import load_install_config

_INSTALL_TOOLS = ["gdisk", "dosfstools", "e2fsprogs", "squashfs-tools", "util-linux"]
INSTALLER_PACKAGES: dict[str, list[str]] = {
    "none": [],
    "unattended": _INSTALL_TOOLS,
    "tui": [*_INSTALL_TOOLS, "whiptail"],
    "calamares": ["calamares", "squashfs-tools", "dosfstools", "e2fsprogs", "efibootmgr", "pkexec"],
}
LIVE_PACKAGES = ["live-boot", "live-boot-initramfs-tools"]
UNITS: dict[str, list[str]] = {
    "none": ["osp-ready.service"],
    "unattended": ["osp-ready.service", "osp-installer.service"],
    "tui": ["osp-ready.service", "osp-installer-tui.service"],
    "calamares": ["osp-ready.service"],
}
# (payload path, destination, mode, installer modes or None for all)
PAYLOAD_FILES = [
    ("osp-ready", "/usr/local/lib/osp/osp-ready", 0o755, None),
    ("osp-ready.service", "/etc/systemd/system/osp-ready.service", 0o644, None),
    ("grub-serial.cfg", "/etc/default/grub.d/osp-serial.cfg", 0o644, None),
    ("install-to-disk", "/usr/local/sbin/install-to-disk", 0o755, {"unattended", "tui"}),
    ("osp-installer.service", "/etc/systemd/system/osp-installer.service", 0o644, {"unattended"}),
    ("osp-installer-tui.service", "/etc/systemd/system/osp-installer-tui.service", 0o644, {"tui"}),
    ("osp-calamares.desktop", "/etc/xdg/autostart/osp-calamares.desktop", 0o644, {"calamares"}),
    ("49-osp-live.rules", "/etc/polkit-1/rules.d/49-osp-live.rules", 0o644, {"calamares"}),
    ("calamares", "/etc/calamares", 0o644, {"calamares"}),
]


def chroot(runner: Runner, rootfs: Path, *cmd: str, input: str | None = None):
    return runner.run(["chroot", str(rootfs), *cmd], input=input)


def _inside(rootfs: Path, path: Path) -> bool:
    root, resolved = Path(rootfs).resolve(), Path(path).resolve()
    return resolved == root or root in resolved.parents


def rootfs_path(rootfs: Path, dest: str) -> Path:
    target = Path(rootfs) / dest.lstrip("/")
    if not _inside(rootfs, target.parent):
        raise StageError(f"refusing to write outside the rootfs: {dest}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    return target


def install_packages(profile: Profile, rootfs: Path, runner: Runner) -> None:
    packages = list(dict.fromkeys([*profile.packages, *INSTALLER_PACKAGES[profile.installer_mode]]))
    if not packages:
        return
    chroot(runner, rootfs, "apt-get", "update")
    chroot(runner, rootfs, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
           "--no-install-recommends", *packages)


def create_users(profile: Profile, secrets: Mapping[str, str], rootfs: Path, runner: Runner) -> None:
    for user in profile.users:
        name = user["name"]
        cmd = ["useradd", "--create-home", "--shell", user.get("shell", "/bin/bash")]
        if user.get("groups"):
            cmd += ["--groups", ",".join(user["groups"])]
        chroot(runner, rootfs, *cmd, name)
        if "password_hash_from" in user:
            chroot(runner, rootfs, "chpasswd", "--encrypted",
                   input=f"{name}:{secrets[user['password_hash_from']]}\n")
        if "ssh_keys_from" in user:
            keys = rootfs_path(rootfs, f"/home/{name}/.ssh/authorized_keys")
            keys.write_text(secrets[user["ssh_keys_from"]].strip() + "\n", encoding="utf-8")
            keys.parent.chmod(0o700)
            keys.chmod(0o600)
            chroot(runner, rootfs, "chown", "-R", f"{name}:{name}", f"/home/{name}/.ssh")


def copy_files(profile: Profile, repo_root: Path, rootfs: Path) -> None:
    for entry in profile.files:
        src, dest = Path(repo_root) / entry["src"], entry["dest"]
        if src.is_dir():
            for f in sorted(p for p in src.rglob("*") if p.is_file()):
                rel = f.relative_to(src).as_posix()
                shutil.copy2(f, rootfs_path(rootfs, f"{dest.rstrip('/')}/{rel}"))
        else:
            target = f"{dest}{src.name}" if dest.endswith("/") else dest
            shutil.copy2(src, rootfs_path(rootfs, target))


def set_services(profile: Profile, rootfs: Path, runner: Runner) -> None:
    for unit in profile.services_enable:
        chroot(runner, rootfs, "systemctl", "enable", unit)
    for unit in profile.services_disable:
        chroot(runner, rootfs, "systemctl", "disable", unit)


def render_install_conf(cfg: dict) -> str:
    return (f"OSP_TARGET_DISK={shlex.quote(cfg.get('target_disk', ''))}\n"
            f"OSP_HOSTNAME={shlex.quote(cfg.get('hostname', ''))}\n"
            f"OSP_CONFIRM={'1' if cfg.get('confirm', True) else '0'}\n")


def _payload(rel: str):
    return resources.files("osp").joinpath("payload", *rel.split("/"))


def _install_node(node, rootfs: Path, dest: str, mode: int) -> None:
    if node.is_dir():
        for child in node.iterdir():
            _install_node(child, rootfs, f"{dest}/{child.name}", mode)
    else:
        target = rootfs_path(rootfs, dest)
        target.write_bytes(node.read_bytes())
        target.chmod(mode)


def install_payload(profile: Profile, repo_root: Path, rootfs: Path, runner: Runner) -> None:
    mode = profile.installer_mode
    for rel, dest, perm, modes in PAYLOAD_FILES:
        if modes is None or mode in modes:
            _install_node(_payload(rel), rootfs, dest, perm)
    rootfs_path(rootfs, "/etc/osp/checks").write_text("".join(f"{c}\n" for c in profile.checks))
    if mode in ("unattended", "tui"):
        cfg = load_install_config(Path(repo_root) / profile.installer_config) if profile.installer_config else {}
        rootfs_path(rootfs, "/etc/osp/install.conf").write_text(render_install_conf(cfg))
        rootfs_path(rootfs, "/etc/osp/live-packages").write_text(" ".join(LIVE_PACKAGES) + "\n")
    for unit in UNITS[mode]:
        chroot(runner, rootfs, "systemctl", "enable", unit)


def configure_live(profile: Profile, rootfs: Path) -> None:
    rootfs_path(rootfs, "/etc/hostname").write_text(f"{profile.name}\n")
    rootfs_path(rootfs, "/etc/hosts").write_text(
        f"127.0.0.1\tlocalhost\n127.0.1.1\t{profile.name}\n::1\tlocalhost ip6-localhost ip6-loopback\n")
    user = profile.autologin_user
    if user is None:
        return
    rootfs_path(rootfs, "/etc/systemd/system/getty@tty1.service.d/autologin.conf").write_text(
        f"[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin {user} --noclear %I $TERM\n")
    if profile.desktop and profile.desktop["display_manager"] == "lightdm":
        rootfs_path(rootfs, "/etc/lightdm/lightdm.conf.d/50-osp-autologin.conf").write_text(
            f"[Seat:*]\nautologin-user={user}\n")


def run_hooks(profile: Profile, repo_root: Path, rootfs: Path, runner: Runner) -> None:
    if not profile.hooks:
        return
    hook_dir = Path(rootfs) / "tmp" / "osp-hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    try:
        for i, rel in enumerate(profile.hooks):
            src = Path(repo_root) / rel
            name = f"{i:02d}-{src.name}"
            shutil.copyfile(src, hook_dir / name)
            try:
                chroot(runner, rootfs, "env", f"OSP_PROFILE={profile.name}",
                       "bash", "-euo", "pipefail", f"/tmp/osp-hooks/{name}")
            except CommandError as e:
                raise StageError(f"customize hook failed: {rel} (exit {e.returncode})", e.log_path) from e
    finally:
        shutil.rmtree(hook_dir, ignore_errors=True)


def finalize(rootfs: Path, runner: Runner) -> None:
    chroot(runner, rootfs, "update-initramfs", "-u", "-k", "all")
    chroot(runner, rootfs, "apt-get", "clean")
    chroot(runner, rootfs, "sh", "-c", "rm -rf /var/lib/apt/lists/*")
    rootfs_path(rootfs, "/etc/machine-id").write_text("")
    dbus_id = Path(rootfs) / "var/lib/dbus/machine-id"
    if os.path.lexists(dbus_id):
        dbus_id.unlink()


@contextmanager
def _no_service_start(rootfs: Path):
    """policy-rc.d keeps package postinst scripts from starting services in the chroot."""
    path = Path(rootfs) / "usr/sbin/policy-rc.d"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 101\n")
    path.chmod(0o755)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def _host_resolv(rootfs: Path, host_resolv: Path = Path("/etc/resolv.conf")):
    """Give the chroot working DNS, then put the original resolv.conf back."""
    target = Path(rootfs) / "etc/resolv.conf"
    backup = Path(rootfs) / "etc/resolv.conf.osp-bak"
    had_original = os.path.lexists(target)
    if had_original:
        os.replace(target, backup)
    content = host_resolv.read_text() if host_resolv.is_file() else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    try:
        yield
    finally:
        ours = target.is_file() and not target.is_symlink() and target.read_text() == content
        if ours:
            target.unlink()
            if had_original:
                os.replace(backup, target)
        elif had_original and os.path.lexists(backup):
            backup.unlink()  # a package replaced resolv.conf on purpose; keep its version


def customize(profile: Profile, secrets: Mapping[str, str], src_rootfs: Path, rootfs: Path,
              repo_root: Path, runner: Runner) -> None:
    rootfs = Path(rootfs)
    rootfs.mkdir(parents=True)
    runner.run(["cp", "-a", f"{src_rootfs}/.", str(rootfs)])
    with chroot_mounts(rootfs, runner), _no_service_start(rootfs), _host_resolv(rootfs):
        install_packages(profile, rootfs, runner)
        create_users(profile, secrets, rootfs, runner)
        copy_files(profile, repo_root, rootfs)
        set_services(profile, rootfs, runner)
        install_payload(profile, repo_root, rootfs, runner)
        configure_live(profile, rootfs)
        run_hooks(profile, repo_root, rootfs, runner)
        finalize(rootfs, runner)
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `python -m pytest tests/test_customize.py -v`
Expected on Linux: 16 passed, 1 deselected. On Windows: 15 passed, 1 skipped (the symlink test), 1 deselected.

- [ ] **Step 5: Run the container test**

```powershell
docker run --rm --privileged -v "${PWD}:/work" -v osp-state:/var/lib/osp osp-builder:latest python3 -m pytest -m container tests/test_customize.py -v
```

Expected: `test_customize_real_rootfs PASSED`.

- [ ] **Step 6: Commit**

```bash
git add src/osp/customize.py tests/test_customize.py
git commit -m "feat: add customize stage"
```

---

### Task 13: Assemble stage

**Files:**
- Create: `src/osp/assemble.py`
- Test: `tests/test_assemble.py`

**Interfaces:**
- Consumes: `Profile` (Task 4); `Runner`, `RecordingRunner` (Task 1); `StageError`, `tool_versions` (Task 8).
- Produces (module `osp.assemble`):
  - `LIVE_CMDLINE = "boot=live console=tty0 console=ttyS0,115200"`
  - `render_grub_cfg(profile) -> str`. Entries, in order: `Live`, `Live (safe graphics)` (adds `nomodeset`), `Live (persistent)` (adds `persistence`; only when `live.persistence`), `Install` (adds `osp.install=<mode>`; only when mode ≠ `none`).
  - `find_kernel(rootfs: Path) -> tuple[Path, Path]`. Raises `StageError` unless there is exactly one `boot/vmlinuz-*` with a matching `initrd.img-*`.
  - `installed_packages(rootfs, runner) -> dict[str, str]`
  - `sha256_file(path) -> str`
  - `build_manifest(profile, build_hash, stem, packages, tools) -> dict`
  - `assemble(profile, rootfs: Path, out_dir: Path, stem: str, build_hash: str, runner) -> Path`. Writes `<stem>.iso`, `<stem>.iso.sha256` and `<stem>.build.json` into `out_dir` and returns the ISO path.

- [ ] **Step 1: Write the failing tests**

`tests/test_assemble.py`:

```python
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
    r = RecordingRunner(results={"dpkg-query": (0, "bash\t5.2\nlive-boot\t1:20230131\n")})
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_assemble.py -v`
Expected: `ImportError: cannot import name 'assemble' from 'osp'`.

- [ ] **Step 3: Implement `src/osp/assemble.py`**

```python
"""Assemble stage: rootfs -> squashfs + kernel/initrd + GRUB config -> hybrid BIOS/UEFI ISO."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from osp.profile import Profile
from osp.run import Runner
from osp.stages import StageError, tool_versions

LIVE_CMDLINE = "boot=live console=tty0 console=ttyS0,115200"
SQUASHFS_EXCLUDES = ["proc/*", "sys/*", "dev/*", "run/*", "tmp/*"]


def render_grub_cfg(profile: Profile) -> str:
    entries = [("Live", ""), ("Live (safe graphics)", "nomodeset")]
    if profile.persistence:
        entries.append(("Live (persistent)", "persistence"))
    if profile.installer_mode != "none":
        entries.append(("Install", f"osp.install={profile.installer_mode}"))
    lines = [
        "set timeout=5",
        "set default=0",
        "serial --unit=0 --speed=115200",
        "terminal_input console serial",
        "terminal_output console serial",
        "",
    ]
    for title, extra in entries:
        args = f"{LIVE_CMDLINE} {extra}".strip()
        lines += [f'menuentry "{title}" {{', f"  linux /live/vmlinuz {args}", "  initrd /live/initrd.img", "}", ""]
    return "\n".join(lines)


def find_kernel(rootfs: Path) -> tuple[Path, Path]:
    kernels = sorted((Path(rootfs) / "boot").glob("vmlinuz-*"))
    if len(kernels) != 1:
        raise StageError(f"expected exactly one kernel in {rootfs}/boot, found {[k.name for k in kernels]}")
    version = kernels[0].name[len("vmlinuz-"):]
    initrd = kernels[0].parent / f"initrd.img-{version}"
    if not initrd.is_file():
        raise StageError(f"missing initrd for kernel {version}: {initrd}")
    return kernels[0], initrd


def installed_packages(rootfs: Path, runner: Runner) -> dict[str, str]:
    admindir = Path(rootfs) / "var/lib/dpkg"
    if not (admindir / "status").is_file():
        return {}
    res = runner.run(["dpkg-query", f"--admindir={rootfs}/var/lib/dpkg", "-W", "-f=${Package}\t${Version}\n"])
    return dict(line.split("\t", 1) for line in res.stdout.splitlines() if "\t" in line)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(profile: Profile, build_hash: str, stem: str,
                   packages: dict[str, str], tools: dict[str, str]) -> dict:
    # profile.data holds secret *references* only; resolved values never reach it.
    return {
        "artifact": f"{stem}.iso",
        "build_hash": build_hash,
        "built": datetime.now(timezone.utc).isoformat(),
        "profile": profile.data,
        "packages": packages,
        "tool_versions": tools,
    }


def assemble(profile: Profile, rootfs: Path, out_dir: Path, stem: str, build_hash: str,
             runner: Runner) -> Path:
    iso_root = Path(out_dir) / "iso"
    live = iso_root / "live"
    live.mkdir(parents=True)
    kernel, initrd = find_kernel(rootfs)
    shutil.copy2(kernel, live / "vmlinuz")
    shutil.copy2(initrd, live / "initrd.img")
    runner.run(["mksquashfs", str(rootfs), str(live / "filesystem.squashfs"),
                "-comp", "xz", "-noappend", "-wildcards", "-e", *SQUASHFS_EXCLUDES])
    grub_dir = iso_root / "boot" / "grub"
    grub_dir.mkdir(parents=True)
    (grub_dir / "grub.cfg").write_text(render_grub_cfg(profile))
    iso = Path(out_dir) / f"{stem}.iso"
    runner.run(["grub-mkrescue", "-o", str(iso), str(iso_root)])
    (Path(out_dir) / f"{stem}.iso.sha256").write_text(f"{sha256_file(iso)}  {iso.name}\n")
    manifest = build_manifest(profile, build_hash, stem, installed_packages(rootfs, runner), tool_versions())
    (Path(out_dir) / f"{stem}.build.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return iso
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `python -m pytest tests/test_assemble.py -v`
Expected: 9 passed, 1 deselected.

- [ ] **Step 5: Run the container test**

```powershell
docker run --rm --privileged -v "${PWD}:/work" -v osp-state:/var/lib/osp osp-builder:latest python3 -m pytest -m container tests/test_assemble.py -v
```

Expected: `test_assemble_builds_real_iso PASSED`.

- [ ] **Step 6: Commit**

```bash
git add src/osp/assemble.py tests/test_assemble.py
git commit -m "feat: add assemble stage producing hybrid live ISO"
```

---

### Task 14: QEMU boot and install tests

**Files:**
- Create: `src/osp/qemu.py`
- Test: `tests/test_qemu.py`

**Interfaces:**
- Consumes: `LIVE_CMDLINE` (Task 13); `Runner` (Task 1); the serial markers produced by `osp-ready` and `install-to-disk` (Task 10).
- Produces (module `osp.qemu`):
  - `OVMF_CODE`, `OVMF_VARS` (use the names Task 2 Step 2 confirmed), `INSTALL_ARGS`
  - `BootResult(ok: bool, reason: str, passed: list[str], failed: list[str], log_path: Path | None)`
  - `CheckParser` with `.feed(line) -> str | None` (returns `"done"` once `OSP-CHECKS-DONE` is seen) and `.result(verdict, log_path) -> BootResult`
  - `kvm_available() -> bool`
  - `qemu_command(*, firmware: str, kvm: bool, vars_path=None, cdrom=None, disk=None, kernel=None, initrd=None, append=None) -> list[str]`
  - `watch(cmd, *, log_path: Path, timeout: float, feed: Callable[[str], str | None], wait_exit: float = 0) -> str`. Returns the string `feed` produced, or `"timeout"`, or `"exited"`.
  - `boot_test(iso, firmware, workdir, log_path, *, timeout=300, kvm=None) -> BootResult`
  - `install_test(iso, firmware, workdir, log_dir, runner, *, timeout=300, install_timeout=1800, kvm=None) -> BootResult`

- [ ] **Step 1: Write the failing tests**

`tests/test_qemu.py`:

```python
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
```

`boot_test` and `install_test` are exercised against real ISOs through `osp build` / `osp test --install` in Task 16. Unit tests cover the command line, the marker parsing and the process watcher.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_qemu.py -v`
Expected: `ModuleNotFoundError: No module named 'osp.qemu'`.

- [ ] **Step 3: Implement `src/osp/qemu.py`**

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_qemu.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/osp/qemu.py tests/test_qemu.py
git commit -m "feat: add QEMU boot and install tests"
```

---

### Task 15: CLI and build orchestration

**Files:**
- Create: `src/osp/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes:
  - `load_profile` (Task 5); `ProfileError` (Task 4)
  - secrets functions (Task 6); `compute_build_hash`, `artifact_stem` (Task 7)
  - `run_stage`, `stages_to_force`, `STAGES`, `StageError` (Task 8); `safe_rmtree` (Task 8)
  - `get_backend` (Task 5/9); `customize` (Task 12); `assemble` (Task 13)
  - `boot_test`, `install_test`, `kvm_available` (Task 14)
  - launcher functions (Task 2); `Runner`, `CommandError` (Task 1)
- Produces:
  - `osp.cli.main(argv: list[str] | None = None) -> int`. Exit codes: 0 ok, 1 build/test failure, 2 usage/profile/secret error.
  - `osp.cli.build_parser() -> argparse.ArgumentParser`
  - `osp.cli.publish(assemble_dir, out_dir, stem) -> Path`
  - Commands:
    - `osp build <profile> [--from STAGE] [--no-test] [--keep-failed/--no-keep-failed] [--timeout S]`
    - `osp test <iso> [--install] [--bios|--uefi] [--timeout S]`
    - `osp profile show|validate <profile>`
    - `osp clean [--cache] [--work]`
  - `build`, `test` and `clean` re-launch themselves in the container when `OSP_IN_CONTAINER` is not set. `profile` commands run wherever they're invoked.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import types
from pathlib import Path

import pytest

from helpers import profile_data, write_yaml
from osp import cli
from osp.launcher import IMAGE
from osp.run import CommandError


@pytest.fixture
def repo(tmp_path, monkeypatch):
    write_yaml(tmp_path / "profiles" / "p.yaml",
               profile_data(name="p", users=[{"name": "a", "ssh_keys_from": "env:OSP_K"}]))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OSP_IN_CONTAINER", raising=False)
    monkeypatch.setenv("OSP_K", "ssh-ed25519 AAAA")
    return tmp_path


@pytest.fixture
def captured_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "find_engine", lambda: "docker")
    monkeypatch.setattr(cli, "launch", lambda cmd: calls.append(cmd) or 0)
    return calls


def test_parser_build_defaults():
    a = cli.build_parser().parse_args(["build", "p.yaml"])
    assert (a.from_stage, a.no_test, a.keep_failed, a.timeout) == (None, False, True, 300)


def test_profile_show_prints_merged(repo, capsys):
    assert cli.main(["profile", "show", "profiles/p.yaml"]) == 0
    out = capsys.readouterr().out
    assert "name: p" in out and "env:OSP_K" in out and "AAAA" not in out


def test_profile_validate_ok_and_missing_secret(repo, capsys, monkeypatch):
    assert cli.main(["profile", "validate", "profiles/p.yaml"]) == 0
    assert "ok: p" in capsys.readouterr().out
    monkeypatch.delenv("OSP_K")
    assert cli.main(["profile", "validate", "profiles/p.yaml"]) == 2
    assert "OSP_K" in capsys.readouterr().err


def test_invalid_profile_exit_2(repo, capsys):
    write_yaml(repo / "profiles" / "bad.yaml", profile_data(bogus=1))
    assert cli.main(["profile", "validate", "profiles/bad.yaml"]) == 2
    assert "bogus" in capsys.readouterr().err


def test_requires_repo_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["profile", "show", "x.yaml"]) == 2
    assert "repository root" in capsys.readouterr().err


def test_build_on_host_launches_container(repo, captured_launch):
    assert cli.main(["build", str(repo / "profiles" / "p.yaml"), "--no-test"]) == 0
    cmd = captured_launch[0]
    assert cmd[cmd.index(IMAGE):] == [IMAGE, "python3", "-m", "osp", "build", "profiles/p.yaml",
                                      "--no-test", "--timeout", "300"]
    assert cmd[cmd.index("OSP_K") - 1] == "-e"
    assert "ssh-ed25519 AAAA" not in " ".join(cmd)


def test_build_on_host_fails_fast_on_missing_secret(repo, captured_launch, monkeypatch):
    monkeypatch.delenv("OSP_K")
    assert cli.main(["build", "profiles/p.yaml"]) == 2
    assert captured_launch == []


def test_test_on_host_forwards_flags(repo, captured_launch):
    (repo / "output").mkdir()
    assert cli.main(["test", "output/x.iso", "--uefi", "--install"]) == 0
    cmd = captured_launch[0]
    assert cmd[cmd.index(IMAGE) + 4:] == ["test", "output/x.iso", "--timeout", "300", "--install", "--uefi"]


@pytest.fixture
def in_container(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("OSP_IN_CONTAINER", "1")
    monkeypatch.setenv("OSP_STATE_DIR", str(tmp_path / "state"))
    calls = []

    def bootstrap(profile, rootfs, runner):
        calls.append("bootstrap")
        rootfs.mkdir(parents=True)

    def fake_customize(profile, secrets, src, rootfs, repo_root, runner):
        calls.append("customize")
        assert secrets == {"env:OSP_K": "ssh-ed25519 AAAA"}
        rootfs.mkdir(parents=True)

    def fake_assemble(profile, rootfs, out_dir, stem, build_hash, runner):
        calls.append("assemble")
        for suffix in (".iso", ".iso.sha256", ".build.json"):
            (out_dir / f"{stem}{suffix}").write_text(suffix)
        return out_dir / f"{stem}.iso"

    backend = types.SimpleNamespace(bootstrap_key=lambda p: "bk", bootstrap=bootstrap)
    monkeypatch.setattr(cli, "get_backend", lambda family: backend)
    monkeypatch.setattr(cli, "customize", fake_customize)
    monkeypatch.setattr(cli, "assemble", fake_assemble)
    return calls


def test_build_in_container_runs_stages_publishes_and_caches(repo, in_container, tmp_path):
    assert cli.main(["build", "profiles/p.yaml", "--no-test"]) == 0
    assert in_container == ["bootstrap", "customize", "assemble"]
    isos = list((repo / "output").glob("p-bookworm-amd64-*.iso"))
    assert len(isos) == 1
    assert isos[0].with_name(isos[0].stem + ".build.json").exists()
    assert (tmp_path / "state" / "cache" / "bootstrap" / "bk" / "stage.json").exists()
    assert cli.main(["build", "profiles/p.yaml", "--no-test"]) == 0
    assert in_container == ["bootstrap", "customize", "assemble"]  # everything cached
    assert cli.main(["build", "profiles/p.yaml", "--no-test", "--from", "assemble"]) == 0
    assert in_container[-1] == "assemble" and len(in_container) == 4


def test_stage_failure_prints_log_tail(repo, in_container, monkeypatch, capsys, tmp_path):
    log = tmp_path / "c.log"
    log.write_text("".join(f"line {i}\n" for i in range(50)))

    def failing(*a, **k):
        raise CommandError(["chroot", "x"], 100, log)

    monkeypatch.setattr(cli, "customize", failing)
    assert cli.main(["build", "profiles/p.yaml", "--no-test"]) == 1
    err = capsys.readouterr().err
    assert "stage 'customize' failed" in err and "last 40 lines" in err
    assert "line 49" in err and "line 9\n" not in err


def test_clean_in_container(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("OSP_IN_CONTAINER", "1")
    state = tmp_path / "state"
    monkeypatch.setenv("OSP_STATE_DIR", str(state))
    for d in ("cache/x", "work/y", "test/z"):
        (state / d).mkdir(parents=True)
    assert cli.main(["clean", "--work"]) == 0
    assert (state / "cache").exists() and not (state / "work").exists() and not (state / "test").exists()
    assert cli.main(["clean"]) == 0
    assert not (state / "cache").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -v`
Expected: `ImportError: cannot import name 'cli' from 'osp'`.

- [ ] **Step 3: Implement `src/osp/cli.py`**

```python
"""osp command line. build/test/clean run inside the builder container; profile commands run anywhere."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date
from pathlib import Path

import yaml

from osp.assemble import assemble
from osp.backends import get_backend
from osp.buildid import artifact_stem, compute_build_hash
from osp.customize import customize
from osp.launcher import LauncherError, container_command, find_engine, in_container, launch
from osp.mounts import safe_rmtree
from osp.profile import ProfileError
from osp.qemu import boot_test, install_test, kvm_available
from osp.run import CommandError, Runner
from osp.secrets import (SecretError, collect_refs, env_names, file_map_from_env, file_paths, resolve,
                         secret_strings)
from osp.stages import STAGES, StageError, run_stage, stages_to_force
from osp.validate import load_profile

FIRMWARES = ("bios", "uefi")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="osp", description="Build purpose-built OS images from profiles.")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build an image from a profile")
    b.add_argument("profile")
    b.add_argument("--from", dest="from_stage", choices=STAGES, help="rerun from this stage, ignoring its cache")
    b.add_argument("--no-test", action="store_true", help="skip the QEMU boot tests")
    b.add_argument("--keep-failed", action=argparse.BooleanOptionalAction, default=True,
                   help="keep a failed stage's work directory for inspection (default: on)")
    b.add_argument("--timeout", type=int, default=300, help="seconds per boot test")
    b.set_defaults(func=cmd_build)

    t = sub.add_parser("test", help="boot-test an ISO in QEMU")
    t.add_argument("iso")
    t.add_argument("--install", action="store_true", help="run the unattended install test")
    fw = t.add_mutually_exclusive_group()
    fw.add_argument("--bios", action="store_true")
    fw.add_argument("--uefi", action="store_true")
    t.add_argument("--timeout", type=int, default=300, help="seconds per boot")
    t.set_defaults(func=cmd_test)

    pr = sub.add_parser("profile", help="inspect profiles")
    psub = pr.add_subparsers(dest="profile_command", required=True)
    s = psub.add_parser("show", help="print the fully merged profile")
    s.add_argument("profile")
    s.set_defaults(func=cmd_profile_show)
    v = psub.add_parser("validate", help="validate a profile and its secrets")
    v.add_argument("profile")
    v.set_defaults(func=cmd_profile_validate)

    c = sub.add_parser("clean", help="remove cached and work state")
    c.add_argument("--cache", action="store_true")
    c.add_argument("--work", action="store_true")
    c.set_defaults(func=cmd_clean)
    return p


def _repo_root() -> Path:
    cwd = Path.cwd()
    if not (cwd / "profiles").is_dir():
        raise LauncherError("run osp from the repository root (no profiles/ directory here)")
    return cwd


def _state_dir(repo: Path) -> Path:
    return Path(os.environ.get("OSP_STATE_DIR") or repo / "output" / "state")


def _repo_relative(path: str, repo: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(repo.resolve()).as_posix()
    except ValueError:
        raise LauncherError(f"{path} must be inside the repository ({repo})") from None


def _launch(repo: Path, osp_args: list[str], envs: list[str], files: list[str]) -> int:
    return launch(container_command(find_engine(), repo, osp_args, envs, files, tty=sys.stdin.isatty()))


def publish(assemble_dir: Path, out_dir: Path, stem: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (".iso", ".iso.sha256", ".build.json"):
        shutil.copy2(assemble_dir / f"{stem}{suffix}", out_dir / f"{stem}{suffix}")
    return out_dir / f"{stem}.iso"


def _run_tests(iso: Path, install: bool, firmwares: list[str], workdir: Path, log_dir: Path,
               runner: Runner, timeout: int) -> int:
    if not kvm_available():
        print("warning: /dev/kvm is not available; QEMU falls back to software emulation "
              "and boot tests will be slow", file=sys.stderr)
    failed = False
    for fw in firmwares:
        if install:
            res = install_test(iso, fw, workdir / fw, log_dir, runner, timeout=timeout)
        else:
            res = boot_test(iso, fw, workdir / fw, log_dir / f"boot-{fw}.log", timeout=timeout)
        label = f"{fw} install" if install else fw
        print(f"[test {label}] {'PASS' if res.ok else 'FAIL'}: {res.reason} "
              f"(passed {len(res.passed)}, failed {res.failed}) log: {res.log_path}")
        failed = failed or not res.ok
    return 1 if failed else 0


def cmd_build(args) -> int:
    repo = _repo_root()
    if not in_container():
        profile = load_profile(args.profile, repo)
        refs = collect_refs(profile)
        resolve(refs, os.environ)  # fail fast on the host, before starting the container
        fwd = ["build", _repo_relative(args.profile, repo)]
        if args.from_stage:
            fwd += ["--from", args.from_stage]
        if args.no_test:
            fwd.append("--no-test")
        if not args.keep_failed:
            fwd.append("--no-keep-failed")
        fwd += ["--timeout", str(args.timeout)]
        return _launch(repo, fwd, env_names(refs), file_paths(refs))
    return _build_in_container(args, repo)


def _build_in_container(args, repo: Path) -> int:
    out, state = repo / "output", _state_dir(repo)
    profile = load_profile(args.profile, repo)
    secrets = resolve(collect_refs(profile), os.environ, file_map_from_env(os.environ))
    build_hash = compute_build_hash(profile, repo, secrets)
    stem = artifact_stem(profile, build_hash, date.today())
    logs = out / "logs" / stem
    base = Runner(secrets=secret_strings(secrets))
    force = stages_to_force(args.from_stage)
    backend = get_backend(profile.family)
    bkey = backend.bootstrap_key(profile)
    work = state / "work" / stem

    def stage(name, key, out_dir, fn):
        log = logs / f"{name}.log"
        return run_stage(name, key, out_dir, lambda d: fn(d, base.with_log(log)),
                         force=name in force, keep_failed=args.keep_failed, log_path=log)

    boot_dir = stage("bootstrap", bkey, state / "cache" / "bootstrap" / bkey,
                     lambda d, r: backend.bootstrap(profile, d / "rootfs", r))
    cust_dir = stage("customize", f"{build_hash}:{bkey}", work / "customize",
                     lambda d, r: customize(profile, secrets, boot_dir / "rootfs", d / "rootfs", repo, r))
    asm_dir = stage("assemble", build_hash, work / "assemble",
                    lambda d, r: assemble(profile, cust_dir / "rootfs", d, stem, build_hash, r))
    iso = publish(asm_dir, out, stem)
    print(f"built {iso}")
    if args.no_test:
        return 0
    return _run_tests(iso, False, list(FIRMWARES), state / "test" / stem, logs,
                      base.with_log(logs / "test.log"), args.timeout)


def cmd_test(args) -> int:
    repo = _repo_root()
    if not in_container():
        fwd = ["test", _repo_relative(args.iso, repo), "--timeout", str(args.timeout)]
        if args.install:
            fwd.append("--install")
        if args.bios:
            fwd.append("--bios")
        if args.uefi:
            fwd.append("--uefi")
        return _launch(repo, fwd, [], [])
    iso = Path(args.iso)
    firmwares = ["bios"] if args.bios else ["uefi"] if args.uefi else list(FIRMWARES)
    logs = repo / "output" / "logs" / iso.stem
    return _run_tests(iso, args.install, firmwares, _state_dir(repo) / "test" / iso.stem, logs,
                      Runner(log_path=logs / "test.log"), args.timeout)


def cmd_profile_show(args) -> int:
    profile = load_profile(args.profile, _repo_root())
    print(yaml.safe_dump(profile.data, sort_keys=False), end="")
    return 0


def cmd_profile_validate(args) -> int:
    profile = load_profile(args.profile, _repo_root())
    resolve(collect_refs(profile), os.environ, file_map_from_env(os.environ))
    print(f"ok: {profile.name}")
    return 0


def cmd_clean(args) -> int:
    repo = _repo_root()
    if not in_container():
        fwd = ["clean"] + (["--cache"] if args.cache else []) + (["--work"] if args.work else [])
        return _launch(repo, fwd, [], [])
    state = _state_dir(repo)
    both = not (args.cache or args.work)
    targets = []
    if args.cache or both:
        targets.append(state / "cache")
    if args.work or both:
        targets += [state / "work", state / "test"]
    for target in targets:
        if target.exists():
            safe_rmtree(target)
            print(f"removed {target}")
    return 0


def _print_log_tail(log_path) -> None:
    if log_path and Path(log_path).is_file():
        tail = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        print(f"--- last {len(tail)} lines of {log_path} ---", file=sys.stderr)
        print("\n".join(tail), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ProfileError, SecretError, LauncherError) as e:
        print(f"osp: {e}", file=sys.stderr)
        return 2
    except (StageError, CommandError) as e:
        print(f"osp: {e}", file=sys.stderr)
        _print_log_tail(e.log_path)
        return 1
    except RuntimeError as e:  # e.g. safe_rmtree refusing to delete a mounted tree
        print(f"osp: {e}", file=sys.stderr)
        return 1
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -v`
Expected: 11 passed.

- [ ] **Step 5: Run the whole unit suite**

Run: `python -m pytest -v`
Expected: all tests pass; `container` tests deselected.

- [ ] **Step 6: Commit**

```bash
git add src/osp/cli.py tests/test_cli.py
git commit -m "feat: add osp CLI and build orchestration"
```

---

### Task 16: Base layers, example profiles, and acceptance

**Files:**
- Create:
  - `profiles/base-common.yaml`, `profiles/base-debian.yaml`, `profiles/base-ubuntu.yaml`, `profiles/server-common.yaml`, `profiles/layer-desktop-xfce.yaml`
  - `profiles/rescue.yaml`, `profiles/homelab-node.yaml`, `profiles/desktop-xfce.yaml`, `profiles/kiosk.yaml`
  - `files/base/network/80-dhcp.network`, `files/homelab/sshd_config.d/10-osp.conf`, `files/kiosk/bash_profile`
  - `hooks/desktop/graphical-target.sh`, `hooks/rescue/sudoers.sh`
  - `installer/rescue.yaml`, `installer/homelab.yaml`
  - `docs/acceptance/2026-11-08-v1.md` (acceptance record)
- Test: `tests/test_example_profiles.py`

**Interfaces:**
- Consumes: the whole pipeline (Tasks 1–15).
- Produces: the four example profiles that make up the spec's acceptance matrix.

- [ ] **Step 1: Write the failing test**

`tests/test_example_profiles.py`:

```python
from pathlib import Path

import pytest

from osp.validate import load_profile

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = {
    "rescue": ("debian", "bookworm", "tui"),
    "homelab-node": ("debian", "bookworm", "unattended"),
    "desktop-xfce": ("ubuntu", "noble", "calamares"),
    "kiosk": ("debian", "bookworm", "none"),
}


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_example_profile_is_valid(name):
    p = load_profile(REPO / "profiles" / f"{name}.yaml", REPO)
    assert (p.flavor, p.release, p.installer_mode) == EXAMPLES[name]
    assert p.checks, "every example profile declares boot-test checks"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_example_profiles.py -v`
Expected: 4 FAIL with `ProfileError: profile not found`.

- [ ] **Step 3: Write the shared layers and their files**

`profiles/base-common.yaml`:

```yaml
schema_version: 1
name: base-common
packages: [systemd-resolved, iproute2, iputils-ping, less, nano, openssh-client]
files:
  - src: files/base/network/
    dest: /etc/systemd/network/
services:
  enable: [systemd-networkd, systemd-resolved]
test:
  checks: ["systemctl is-active systemd-networkd"]
```

`profiles/base-debian.yaml`:

```yaml
schema_version: 1
name: base-debian
extends: [base-common]
distro: {family: debian, flavor: debian, release: bookworm, arch: amd64}
```

`profiles/base-ubuntu.yaml`:

```yaml
schema_version: 1
name: base-ubuntu
extends: [base-common]
distro: {family: debian, flavor: ubuntu, release: noble, arch: amd64}
```

`profiles/server-common.yaml`:

```yaml
schema_version: 1
name: server-common
packages: [openssh-server]
services:
  enable: [ssh]
test:
  checks: ["systemctl is-active ssh"]
```

`profiles/layer-desktop-xfce.yaml`:

```yaml
schema_version: 1
name: layer-desktop-xfce
packages: [xserver-xorg, xfce4, xfce4-terminal, lightdm, lightdm-gtk-greeter, dbus-x11, polkitd, pkexec, mousepad]
desktop: {display_manager: lightdm}
services:
  enable: [lightdm]
hooks:
  customize: [hooks/desktop/graphical-target.sh]
test:
  checks: ["systemctl is-active lightdm"]
```

`files/base/network/80-dhcp.network`:

```ini
[Match]
Name=en* eth*

[Network]
DHCP=yes
```

`hooks/desktop/graphical-target.sh`:

```bash
systemctl set-default graphical.target
```

- [ ] **Step 4: Write the purpose profiles and their files**

`profiles/rescue.yaml`:

```yaml
schema_version: 1
name: rescue
extends: [base-debian]
packages: [testdisk, gddrescue, gdisk, parted, smartmontools, nvme-cli, lvm2, mdadm, cryptsetup,
           ntfs-3g, dosfstools, e2fsprogs, btrfs-progs, xfsprogs, rsync, tmux, htop, ethtool, tcpdump, mtr-tiny]
users:
  - name: rescue
    groups: [sudo]
hooks:
  customize: [hooks/rescue/sudoers.sh]
live:
  autologin: true
installer:
  mode: tui
  config: installer/rescue.yaml
test:
  checks: ["command -v testdisk", "sudo -n -u rescue sudo -n true"]
```

`hooks/rescue/sudoers.sh`:

```bash
echo 'rescue ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/rescue
chmod 0440 /etc/sudoers.d/rescue
visudo -cf /etc/sudoers.d/rescue
```

`installer/rescue.yaml`:

```yaml
hostname: rescue
```

`profiles/homelab-node.yaml`:

```yaml
schema_version: 1
name: homelab-node
extends: [base-debian, server-common]
packages: [qemu-guest-agent]
files:
  - src: files/homelab/sshd_config.d/
    dest: /etc/ssh/sshd_config.d/
users:
  - name: admin
    groups: [sudo]
    ssh_keys_from: env:OSP_ADMIN_SSH_KEY
installer:
  mode: unattended
  config: installer/homelab.yaml
test:
  checks: ["test -s /home/admin/.ssh/authorized_keys"]
```

`files/homelab/sshd_config.d/10-osp.conf`:

```
PasswordAuthentication no
PermitRootLogin no
```

`installer/homelab.yaml`:

```yaml
target_disk: /dev/sda
hostname: homelab-node
confirm: true
```

`profiles/desktop-xfce.yaml`:

```yaml
schema_version: 1
name: desktop-xfce
extends: [base-ubuntu, layer-desktop-xfce]
users:
  - name: live
    groups: [sudo]
live:
  autologin: true
installer:
  mode: calamares
test:
  checks: ["test -f /etc/calamares/settings.conf"]
```

`profiles/kiosk.yaml`:

```yaml
schema_version: 1
name: kiosk
extends: [base-debian]
packages: [cage, chromium, fonts-dejavu-core]
users:
  - name: kiosk
    groups: [video, render, input]
files:
  - src: files/kiosk/bash_profile
    dest: /home/kiosk/.bash_profile
live:
  autologin: true
test:
  checks: ["pgrep -x cage"]
```

`files/kiosk/bash_profile`:

```bash
# Start the kiosk app on the first console only; other ttys get a normal shell.
if [ "$(tty)" = /dev/tty1 ]; then
  export WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1
  exec cage -- chromium --kiosk --no-first-run --noerrdialogs https://example.org
fi
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_example_profiles.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit the profiles**

```bash
git add profiles files hooks installer tests/test_example_profiles.py
git commit -m "feat: add base layers and example profiles"
```

- [ ] **Step 7: Acceptance build — `homelab-node` (unattended)**

Generate a throwaway test key (PowerShell), then build. Any public key works for the boot test.

```powershell
ssh-keygen -t ed25519 -N '""' -f "$env:TEMP\osp-test-key" -q
$env:OSP_ADMIN_SSH_KEY = Get-Content "$env:TEMP\osp-test-key.pub"
osp build profiles/homelab-node.yaml
```

Expected: `built output/homelab-node-bookworm-amd64-<date>-<hash>.iso`, then `[test bios] PASS` and `[test uefi] PASS`. Then run the install test:

```powershell
osp test output/homelab-node-bookworm-amd64-<date>-<hash>.iso --install
```

Expected: `[test bios install] PASS` and `[test uefi install] PASS`. On failure, read `output/logs/<stem>/install-<fw>.log` and `installed-<fw>.log`, fix the cause in the relevant task's code, and rerun with `osp build ... --from <stage>`.

- [ ] **Step 8: Acceptance builds — `rescue`, `kiosk`, `desktop-xfce`**

```powershell
osp build profiles/rescue.yaml
```

```powershell
osp build profiles/kiosk.yaml
```

```powershell
osp build profiles/desktop-xfce.yaml
```

Expected: every build prints `PASS` for both `bios` and `uefi`. `desktop-xfce` is the Ubuntu noble build, so it's the real-pipeline confirmation of the Task 3 spike.

- [ ] **Step 9: Manual installer checks (not automatable in v1)**

1. `rescue`, TUI installer. Boot the ISO in QEMU with a blank disk, choose *Install*, pick the disk, enter a hostname, and confirm. Then reboot from the disk and log in on the console.

   ```powershell
   docker run --rm -it --privileged -v "${PWD}:/work" osp-builder:latest sh -c "qemu-img create -f qcow2 /tmp/d.qcow2 8G && qemu-system-x86_64 -m 2048 -cdrom output/<rescue-iso> -drive file=/tmp/d.qcow2,if=virtio,format=qcow2 -nographic"
   ```

2. `desktop-xfce`, Calamares. Boot it on a VM with a display: Proxmox or local QEMU with `-display gtk` (Linux host), or Hyper-V/VirtualBox on Windows. Choose *Install*, confirm that Calamares starts on its own, install, reboot, and log in as the user created in Calamares. Also check that `/usr/share/calamares/branding/default` existed on the live system (`branding: default` depends on it).

- [ ] **Step 10: Record acceptance**

`docs/acceptance/2026-11-08-v1.md`. Fill it in from the observed results:

```markdown
# v1 acceptance (spec §7)

| Profile | Flavor | Installer | BIOS boot | UEFI boot | Install test | Notes |
|---|---|---|---|---|---|---|
| rescue | debian/bookworm | tui | PASS/FAIL | PASS/FAIL | manual: PASS/FAIL | |
| homelab-node | debian/bookworm | unattended | PASS/FAIL | PASS/FAIL | BIOS PASS/FAIL, UEFI PASS/FAIL | |
| desktop-xfce | ubuntu/noble | calamares | PASS/FAIL | PASS/FAIL | manual: PASS/FAIL | |
| kiosk | debian/bookworm | none | PASS/FAIL | PASS/FAIL | n/a | |

KVM available during the run: yes/no. Builder image tool versions: see any `output/*.build.json`.
```

```bash
git add docs/acceptance/2026-11-08-v1.md
git commit -m "docs: record v1 acceptance results"
```

---

### Task 17: Documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`

**Interfaces:** none.

- [ ] **Step 1: Rewrite `README.md`**

```markdown
# os-pipeline

Build purpose-built OS images from declarative YAML profiles. v1 builds Debian and Ubuntu (amd64) hybrid live ISOs (BIOS + UEFI, USB-bootable) with an optional install-to-disk path (unattended, text-mode, or Calamares), and boot-tests every image in QEMU.

## Requirements

- Docker or Podman, with privileged containers allowed (Docker Desktop works on Windows).
- Python 3.11+ on the host for the `osp` launcher.

## Setup

    python -m pip install -e ".[dev]"
    docker build -t osp-builder:latest -f builder/Containerfile builder

## Usage (from the repository root)

    osp profile show profiles/rescue.yaml       # print the merged profile
    osp profile validate profiles/rescue.yaml   # schema, rules, secrets
    osp build profiles/rescue.yaml              # build + BIOS/UEFI boot tests
    osp build profiles/rescue.yaml --from customize   # rerun from a stage
    osp test output/<image>.iso --install       # unattended install test
    osp clean [--cache] [--work]

Artifacts land in `output/` (`.iso`, `.iso.sha256`, `.build.json`), logs in `output/logs/<image>/`. Work and cache state live in the `osp-state` Docker volume.

## Profiles

Profiles live in `profiles/` and combine layers with `extends:`. Secrets (SSH keys, password hashes) are only referenced, as `env:VAR` or `file:path`. Examples: `rescue`, `homelab-node` (needs `OSP_ADMIN_SSH_KEY`), `desktop-xfce`, `kiosk`.

Design: `docs/superpowers/specs/2026-11-08-os-pipeline-core-debian-design.md`.
```

- [ ] **Step 2: Replace `CLAUDE.md`**

```markdown
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Run everything from the repository root. Host shell is PowerShell on Windows.

- Install for development: `python -m pip install -e ".[dev]"`
- Build the builder image: `docker build -t osp-builder:latest -f builder/Containerfile builder`
- Unit tests (host, fast): `python -m pytest`. A single test: `python -m pytest tests/test_profile.py::test_merge_lists_concatenate_and_dedupe -v`
- Container tests (need root, loop devices and network; run in the builder): `docker run --rm --privileged -v "${PWD}:/work" -v osp-state:/var/lib/osp osp-builder:latest python3 -m pytest -m container -v`
- Bash payload tests are skipped on Windows; run them in the container: `docker run --rm -v "${PWD}:/work" osp-builder:latest python3 -m pytest tests/test_payload.py -v`
- Build an image: `osp build profiles/<name>.yaml` (add `--no-test` to skip QEMU; `--from <stage>` to bypass a stage's cache)

## Architecture

- `osp` on the host only validates profiles and re-launches itself in the privileged `osp-builder` container (`src/osp/launcher.py`). Inside the container, `OSP_IN_CONTAINER=1` and `OSP_STATE_DIR=/var/lib/osp` (a named volume, because a Windows bind mount can't hold a rootfs). Only `output/` (artifacts + logs) is written to the repo.
- A build is four stages run by `run_stage` (`src/osp/stages.py`). Each writes a `stage.json` manifest that doubles as the cache record: bootstrap (cached by `bootstrap_key`) → customize → assemble (keyed by the build hash) → test (never cached).
- Only bootstrap is family-specific. A backend module in `src/osp/backends/` exposes `SUPPORTED`, `bootstrap_key(profile)` and `bootstrap(profile, rootfs, runner)`, and is registered in `backends/__init__.py`. New Linux families add a backend; Windows will get its own stage set (separate spec).
- Every external command goes through `osp.run.Runner`, which logs per stage and redacts secret values. Unit tests swap in `RecordingRunner` and assert on recorded commands instead of executing them.
- `src/osp/payload/` is shipped *into* images: `install-to-disk` (bash), systemd units, `osp-ready` and the Calamares config. Payload content is part of the build hash.
- QEMU tests talk to the image only through serial-console markers (`OSP-READY`, `OSP-CHECK-PASS/FAIL`, `OSP-CHECKS-DONE`, `OSP-INSTALL-DONE`), printed by `osp-ready` and `install-to-disk`. Change both sides together.
- Profiles: lists concatenate (de-duplicated), dicts merge, scalars override; validation is JSON Schema (`src/osp/schema/`) plus semantic rules in `src/osp/validate.py`. Secrets are `env:`/`file:` references only, and resolved values must never reach manifests or logs.

## Conventions

- Line endings: LF everywhere except `*.ps1`/`*.bat`/`*.cmd` (CRLF), enforced by `.gitattributes`. Bash payload scripts must stay LF.
- Design spec and plan: `docs/superpowers/specs/` and `docs/superpowers/plans/`.
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document usage and architecture"
```

