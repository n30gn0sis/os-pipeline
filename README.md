# os-pipeline

Build purpose-built OS images from declarative YAML profiles. v1 builds Debian and Ubuntu (amd64) hybrid live ISOs (BIOS + UEFI, USB-bootable) with an optional install-to-disk path (unattended, text-mode, or Calamares), and boot-tests every image in QEMU.

## Requirements

- Docker on Linux, or Docker Desktop on Windows, with privileged containers allowed. The launcher also detects Podman, but v1 was tested with Docker only.
- Python 3.11+ on the host for the `osp` launcher.
- Boot tests use KVM when `/dev/kvm` is available and fall back to slow TCG emulation otherwise.

## Setup

    python -m pip install -e ".[dev]"
    docker build -t osp-builder:latest -f builder/Containerfile builder

## Usage (from the repository root)

    osp profile show profiles/rescue.yaml       # print the merged profile
    osp profile validate profiles/rescue.yaml   # schema, rules, secrets
    osp build profiles/rescue.yaml              # build + BIOS/UEFI boot tests
    osp build profiles/rescue.yaml --from customize   # rerun from a stage
    osp test output/<image>.iso --bios|--uefi   # boot test (default: both)
    osp test output/<image>.iso --install       # unattended install test
    osp clean [--cache] [--work]

Artifacts land in `output/` (`.iso`, `.iso.sha256`, `.build.json`), logs in `output/logs/<image>/`. Work and cache state live in the `osp-state` Docker volume. The hash suffix in the image name covers the merged profile, its referenced files and secrets, and the osp code itself, so it changes when the osp code changes, and the next build reruns customize and assemble once for that code change.

## Profiles

Profiles live in `profiles/` and combine layers with `extends:`. Secrets (SSH keys, password hashes) are only referenced, as `env:VAR` or `file:path`. Examples: `rescue`, `homelab-node` (needs `OSP_ADMIN_SSH_KEY`), `desktop-xfce`, `kiosk`.

## Behaviour worth knowing

- Calamares installs remove both the display-manager (lightdm) and the tty1 autologin. TUI and unattended installs keep whatever `live.autologin` the profile set; there the profile decides.
- Calamares installs carry the live/profile users onto the installed system. They have no password unless the profile sets `password_hash`.
- Run one build per profile at a time. The work directory is one slot per profile name (`state/work/<profile.name>` in the state volume) and there is no lock.
- `files[]` entries are installed with mode 0644 (directories 0755). Use a `hooks.customize` script to set executable bits or other modes.
- `installer/homelab.yaml` is an example. Its `target_disk: /dev/sda` must match the real disk (`/dev/vda` on virtio VMs), and `confirm: true` asks for a typed YES on tty1 (the local monitor and keyboard; the prompt is also echoed to the serial console, but the answer is read only from tty1). A serial-only install (no tty1) must use `confirm: false`. `osp test --install` supplies its own config (target = the test disk, `confirm: false`) via the kernel command line and does not use the profile's.
- Bootstrap caches are keyed by flavor, release, arch, apt sources, the bootstrap package list and the backend's own source (`src/osp/backends/debian.py`). After a change to any of those the old cache is orphaned until `osp clean --cache`.

## Verification

The acceptance record for `main` is `docs/acceptance/2026-11-08-v1.md`. The two interactive installs (`rescue` text-mode, `desktop-xfce` Calamares) are verified manually, not by `osp test`.

Design: `docs/superpowers/specs/2026-11-08-os-pipeline-core-debian-design.md`.
