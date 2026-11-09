# os-pipeline: Core Pipeline + Debian/Ubuntu Backend — Design

Date: 2026-11-08
Status: Approved design, pending implementation plan

## 1. Goal and scope

`os-pipeline` builds purpose-built OS images from declarative profiles. The long-term goal covers every OS family (Windows and multiple Linux families). That is decomposed into independent sub-projects:

1. **Core pipeline + Debian/Ubuntu backend** — *this spec.*
2. Further Linux backends (Arch, Fedora/RHEL, openSUSE, …) — each its own spec; implements only the bootstrap stage.
3. Windows backend — its own spec; shares the profile/CLI layer but defines its own stage set (DISM/WinPE/unattend.xml), since it does not fit the rootfs model.

### In scope (v1)

- Python CLI `osp`, executed inside a privileged builder container (Docker or Podman; Docker Desktop on Windows is sufficient on the host).
- Declarative YAML profiles with inheritance and schema validation.
- Debian (e.g. `bookworm`) and Ubuntu (e.g. `noble`), `amd64` only.
- Output: hybrid live ISO (BIOS + UEFI, bootable from optical media or USB), optionally carrying an install-to-disk option.
- Installer modes: `unattended`, `tui`, `calamares`, `none`.
- Automated QEMU boot and install testing.
- Four example profiles: `rescue`, `homelab-node`, `desktop-xfce`, `kiosk`.

### Out of scope (v1)

Secure Boot, non-amd64 architectures, encrypted root, dual-boot, VM/disk-image outputs (qcow2/raw/vmdk), standalone installer-only ISOs, CI integration.

## 2. Architecture

A host-side launcher (`osp ...`) starts the builder container with the repo and `output/` mounted, then runs the same `osp` package inside it. All image work happens in the container.

```
osp build profiles/rescue.yaml
  └─ builder container: resolve → bootstrap → customize → assemble → test
```

### Units

| Unit | Responsibility | Input → Output |
|---|---|---|
| `osp.profile` | Load YAML, resolve `extends:`, validate against JSON Schema, resolve secret references | profile path → `Profile` |
| `osp.backends.debian` | Bootstrap stage for Debian and Ubuntu via `mmdebstrap`. The **only** family-specific unit | `Profile` → rootfs dir |
| `osp.customize` | Apply packages, files, users, services, installer payload, hooks inside a chroot | rootfs + `Profile` → rootfs |
| `osp.assemble.live_iso` | squashfs, kernel/initrd extraction, GRUB config, hybrid ISO | rootfs → `.iso` + metadata |
| `osp.installer` | Source of `install-to-disk` (shipped into images) and Calamares config | files copied in by customize |
| `osp.test.qemu` | Headless boot/install tests | `.iso` → pass/fail report |
| `osp.cli` | Stage orchestration, caching, artifact naming, logging | — |
| `osp.run` | Single wrapper for all external commands (logging, error capture, test recording) | — |

### Stage contract

- Each stage reads its predecessor's work directory and writes a new one under `output/work/<build>/<stage>/`.
- On success it writes `stage.json` (stage name, input hash, tool versions, timestamp). No `stage.json` means the stage did not complete.
- `osp build --from <stage>` resumes from the last completed cached stage before `<stage>`.
- A new Linux backend implements one function: `bootstrap(profile, workdir) -> rootfs_path`. Everything downstream is shared.

### Repository layout

```
src/osp/             Python package (units above)
profiles/            base layers (base-debian.yaml, base-ubuntu.yaml, server-common.yaml,
                     desktop-xfce-layer.yaml, ...) and purpose profiles
files/               static files referenced by profiles
hooks/               customize hook scripts referenced by profiles
installer/           installer configs referenced by profiles
builder/Containerfile builder image (mmdebstrap, squashfs-tools, grub-*, xorriso, qemu, ovmf, python)
tests/               unit, container and boot tests
output/              artifacts, work dirs, logs (git-ignored)
```

## 3. Profile format

YAML, `schema_version: 1`, validated by a versioned JSON Schema.

```yaml
schema_version: 1
name: homelab-node
extends: [base-debian, server-common]   # merged left→right, then this file
distro:
  family: debian            # selects osp.backends.debian
  release: bookworm         # Ubuntu profiles use release: noble with family: debian, flavor: ubuntu
  flavor: debian            # debian | ubuntu
  arch: amd64
packages:
  - openssh-server
  - qemu-guest-agent
files:
  - src: files/homelab/sshd_config.d/
    dest: /etc/ssh/sshd_config.d/
users:
  - name: admin
    groups: [sudo]
    ssh_keys_from: env:OSP_ADMIN_SSH_KEY
services:
  enable: [ssh, qemu-guest-agent]
hooks:
  customize: [hooks/homelab/post.sh]
live:
  persistence: false
  autologin: false
installer:
  mode: unattended          # unattended | tui | calamares | none
  config: installer/homelab.yaml
test:
  checks: ["systemctl is-active ssh"]
```

### Merge rules

- `extends` names resolve to `profiles/<name>.yaml`; parents are merged left to right, then the child.
- Lists concatenate with order-preserving de-duplication; scalars and mappings-of-scalars are overridden by the later source; nested mappings merge recursively.
- Cycles in `extends` are a validation error.
- `osp profile show <file>` prints the fully merged, validated profile (secrets shown as their reference, never their value).

### Secrets

- Fields that carry secrets accept only references: `env:VAR` or `file:path` (path outside the repo is allowed).
- References are resolved at build time inside the container. Resolved values are never written to `stage.json`, `build.json`, or logs; `osp.run` redacts known secret values from logged command lines and output.
- A missing env var or file is a validation error before any stage runs.

### Validation rules (beyond schema types)

- `installer.mode: calamares` requires a desktop layer (a profile in `extends` providing a graphical session).
- `installer.mode: unattended` requires `installer.config` with an explicit target disk; otherwise validation fails.
- The unattended installer config also has `confirm: true|false` (default `true`): when `true`, the installer requires a typed confirmation before wiping the target.
- `distro.family` must name an existing backend; `distro.flavor`/`release` must be a combination that backend declares as supported.

## 4. Build flow

1. **Resolve** — merge + validate the profile, resolve secrets, compute the build hash over the merged profile plus the contents of every referenced file, hook, and installer config. Artifact name: `<name>-<release>-<arch>-<YYYYMMDD>-<hash6>.iso`.
2. **Bootstrap** — `mmdebstrap` with `--variant=minbase` for the configured flavor/release/mirror, plus kernel, `live-boot`, `systemd-sysv`, and GRUB packages. Cache key: flavor + release + arch + sorted bootstrap package list.
3. **Customize** — in a chroot with `/proc`, `/sys`, `/dev`, `/dev/pts` bind-mounted (always unmounted in a `finally`):
   1. install `packages` (plus packages implied by installer mode: `whiptail`/`parted`/`grub-*` for `tui`/`unattended`, `calamares` + settings for `calamares`);
   2. copy `files`;
   3. create `users` and install SSH keys;
   4. enable/disable `services`;
   5. install the installer payload and `osp-ready.service`;
   6. configure live options (autologin, persistence support);
   7. run `hooks.customize` in order, each with `set -euo pipefail`;
   8. clean apt caches and machine-id.
4. **Assemble** — `mksquashfs` the rootfs to `live/filesystem.squashfs`; copy kernel and initrd to `live/`; render `grub.cfg` with entries *Live*, *Live (safe graphics)*, and *Install* (only if installer mode ≠ `none`); build a hybrid BIOS+UEFI ISO with `grub-mkrescue` (xorriso backend). Write alongside it `<artifact>.iso.sha256` and `<artifact>.build.json` (merged profile with secrets redacted, installed package versions, tool versions, build hash).
5. **Test** — see §7. Runs by default; skipped with `--no-test`.

### Ubuntu

Ubuntu images use the same `live-boot` stack (from Ubuntu universe) rather than casper, so there is a single assemble path. This is the main technical risk; the implementation plan must start with a spike proving an Ubuntu `noble` rootfs boots via `live-boot` from the shared assemble stage. If the spike fails, the fallback is a flavor-specific initramfs/boot-parameter branch inside `osp.assemble.live_iso` — not a separate backend.

## 5. Installer

`install-to-disk` ships in every image whose `installer.mode` is not `none`. Core steps (shared by all front ends):

1. Partition target disk: GPT with EFI system partition, root partition, and a BIOS boot partition for legacy boot.
2. Copy the running live rootfs (`/run/live/rootfs/filesystem.squashfs` contents) to the root partition.
3. Remove live-only packages (`live-boot`, installer packages, `osp-ready.service`).
4. Write `/etc/fstab` (by UUID) and hostname; regenerate initramfs; install GRUB for both EFI and BIOS.
5. Apply post-install settings from the installer config (hostname, optional user overrides).

Front ends:

- **`tui`** — whiptail dialogs: choose disk, hostname, confirm wipe.
- **`unattended`** — reads `install.yaml` (from `installer.config`, copied onto the ISO) and runs without interaction, honoring `confirm`.
- **`calamares`** — Calamares configured with `partition`, `unpackfs` (from the live squashfs), `fstab`, `bootloader`, and `users` modules; live-only package removal via its `packages` module.

## 6. Error handling and logging

- Each stage runs inside a context manager that always unmounts bind mounts and detaches loop devices, including on `KeyboardInterrupt`.
- A failed stage leaves its work directory for inspection (`--keep-failed`, default on) and writes no `stage.json`, so reruns retry it.
- Each stage logs to `output/logs/<build>/<stage>.log`; on failure the CLI prints the stage name, the failing command, and the last 40 log lines.
- A failing hook fails the customize stage and is named in the error.
- The host launcher fails fast with a clear message if neither Docker nor Podman is available, or the container cannot run privileged.

## 7. Testing

1. **Unit tests** (pytest on the host, no root): profile merge rules, validation rules from §3, secret resolution and redaction, cache-key hashing, artifact naming, `grub.cfg` and manifest rendering. Stage code uses `osp.run`, which tests swap for a recorder that captures intended commands.
2. **Container stage tests** (`@pytest.mark.container`, inside the builder): bootstrap a minimal `minbase` rootfs once (cached), run customize against fixture profiles, assert files, users, and enabled service units exist in the rootfs.
3. **Boot tests** (`osp test <iso>`; also the final stage of `osp build`):
   - QEMU, headless, serial console, in BIOS mode and UEFI (OVMF) mode.
   - Success = `osp-ready.service` prints a ready marker, then each `test.checks` command passes. Timeout 5 minutes per boot.
   - `--install`: boot with the unattended installer against a blank qcow2 disk, reboot from that disk, and re-check the marker. The test supplies its own unattended config with `confirm: false`, the qcow2 as target, and `keep_test_marker: true`, which makes `install-to-disk` skip removing `osp-ready.service` (step 3 of §5). `keep_test_marker` is rejected by validation in profile-supplied installer configs.
   - Without KVM (e.g. Docker Desktop), QEMU uses TCG; the CLI warns that boot tests will be slow.

### Acceptance

v1 is done when each example profile builds and passes its boot test in BIOS and UEFI mode, and the installer-bearing profiles pass `--install`:

| Profile | Flavor | Installer mode |
|---|---|---|
| `rescue` | Debian bookworm | `tui` |
| `homelab-node` | Debian bookworm | `unattended` |
| `desktop-xfce` | Ubuntu noble | `calamares` |
| `kiosk` | Debian bookworm | `none` (autologin into a single app) |

`--install` boot testing applies to `homelab-node` (unattended). `tui` and `calamares` installs are interactive and are verified manually for v1.

## 8. CLI surface (v1)

```
osp build <profile> [--from <stage>] [--no-test] [--keep-failed/--no-keep-failed]
osp test <iso> [--install] [--bios|--uefi]   # default: both firmware modes
osp profile show <profile>
osp profile validate <profile>
osp clean [--cache] [--work]
```
