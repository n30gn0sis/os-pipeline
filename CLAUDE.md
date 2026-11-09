# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Run everything from the repository root. The commands below work in PowerShell (Windows) and in a shell on a Linux Docker host.

- Install for development: `python -m pip install -e ".[dev]"`
- Build the builder image: `docker build -t osp-builder:latest -f builder/Containerfile builder`
- Unit tests (host, fast): `python -m pytest`. A single test: `python -m pytest tests/test_profile.py::test_merge_lists_concatenate_and_dedupe -v`
- Container tests (need root and network; run in the builder): `docker run --rm --privileged -v "${PWD}:/work" -v osp-state:/var/lib/osp osp-builder:latest python3 -m pytest -m container -v`
- Bash payload tests are skipped on Windows; run them in the container: `docker run --rm -v "${PWD}:/work" osp-builder:latest python3 -m pytest tests/test_payload.py -v`
- Build an image: `osp build profiles/<name>.yaml` (add `--no-test` to skip QEMU; `--from <stage>` to bypass a stage's cache)

## Architecture

- `osp` on the host only validates profiles and re-launches itself in the privileged `osp-builder` container (`src/osp/launcher.py`). Inside the container, `OSP_IN_CONTAINER=1` and `OSP_STATE_DIR=/var/lib/osp` (a named volume, because a Windows bind mount can't hold a rootfs). Only `output/` (artifacts + logs) is written to the repo.
- A build is four stages run by `run_stage` (`src/osp/stages.py`). Each writes a `stage.json` manifest that doubles as the cache record: bootstrap (cached by `bootstrap_key`) → customize → assemble (keyed by the build hash, which also covers the osp code, so a code change reruns customize/assemble once) → test (never cached).
- Only bootstrap is family-specific. A backend module in `src/osp/backends/` exposes `SUPPORTED`, `bootstrap_key(profile)` and `bootstrap(profile, rootfs, runner)`, and is registered in `backends/__init__.py`. New Linux families add a backend; Windows will get its own stage set (separate spec).
- Every external command goes through `osp.run.Runner`, which logs per stage and redacts secret values. Unit tests swap in `RecordingRunner` and assert on recorded commands instead of executing them.
- `src/osp/payload/` is shipped *into* images: `install-to-disk` (bash), systemd units, `osp-ready` and the Calamares config. Payload content is part of the build hash.
- QEMU tests talk to the image only through serial-console markers (`OSP-READY`, `OSP-CHECK-PASS/FAIL`, `OSP-CHECKS-DONE`, `OSP-INSTALL-DONE`), printed by `osp-ready` and `install-to-disk`. Change both sides together.
- The QEMU test VMs use virtio-gpu and an AHCI CD-ROM (`src/osp/qemu.py`). Serial markers may appear anywhere in a console line (e.g. after a `login:` prompt); the earliest match wins.
- Profiles: lists concatenate (de-duplicated), dicts merge, scalars override; validation is JSON Schema (`src/osp/schema/`) plus semantic rules in `src/osp/validate.py`. Secrets are `env:`/`file:` references only, and resolved values must never reach manifests or logs.

## Conventions

- Line endings: LF everywhere except `*.ps1`/`*.bat`/`*.cmd` (CRLF), enforced by `.gitattributes`. Bash payload scripts must stay LF.
- Design spec and plan: `docs/superpowers/specs/` and `docs/superpowers/plans/`.
- The four example profiles and their expected QEMU results are recorded in `docs/acceptance/2026-11-08-v1.md`. Profile-related changes should keep `tests/test_example_profiles.py` passing.
