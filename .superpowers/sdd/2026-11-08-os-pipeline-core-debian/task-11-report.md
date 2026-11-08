# Task 11 report: Calamares installer payload

## Status
DONE

## Commit
- `85623f6` feat: add Calamares installer configuration

## What was implemented
Added the Calamares configuration payload exactly as specified in `task-11-brief.md`:

- `src/osp/payload/calamares/settings.conf`
- `src/osp/payload/calamares/modules/unpackfs.conf`
- `src/osp/payload/calamares/modules/packages.conf`
- `src/osp/payload/calamares/modules/shellprocess_removelive.conf`
- `src/osp/payload/calamares/modules/bootloader.conf`
- `src/osp/payload/calamares/modules/displaymanager.conf`
- `src/osp/payload/calamares/modules/users.conf`
- `src/osp/payload/osp-calamares.desktop`
- `src/osp/payload/49-osp-live.rules`
- `tests/test_payload_calamares.py` (5 tests)

Existing Task 10 payload files were not modified.

## TDD evidence

RED: `python -m pytest tests/test_payload_calamares.py -v`
```
FAILED tests/test_payload_calamares.py::test_settings_sequence_and_instances
FAILED tests/test_payload_calamares.py::test_unpackfs_reads_live_squashfs - F...
FAILED tests/test_payload_calamares.py::test_packages_remove_live_only_packages
FAILED tests/test_payload_calamares.py::test_removelive_drops_osp_live_files
FAILED tests/test_payload_calamares.py::test_autostart_is_gated_on_cmdline - ...
============================== 5 failed in 0.41s ==============================
```
Failures were `FileNotFoundError` for the missing payload files.

GREEN: `python -m pytest tests/test_payload_calamares.py -v`
```
test_settings_sequence_and_instances PASSED
test_unpackfs_reads_live_squashfs PASSED
test_packages_remove_live_only_packages PASSED
test_removelive_drops_osp_live_files PASSED
test_autostart_is_gated_on_cmdline PASSED
============================== 5 passed in 0.11s ==============================
```

Full unit suite: `python -m pytest`
```
================ 94 passed, 10 skipped, 1 deselected in 1.33s =================
```
The skips and the deselected test come from the existing pytest configuration (container-marked tests), not from this change.

## Verification
- LF-only byte check (`b"\r" not in Path(p).read_bytes()`) passed for all 10 new files.
- `pyproject.toml` package-data includes `payload/**/*`, so the `calamares/` subtree ships in built wheels.

## Files changed
- Added the 10 files listed above. Nothing else was staged.
- Untracked `CLAUDE.md` and `src/os_pipeline.egg-info/` were not staged.

## Self-review
- Completeness: all 9 payload files and the test file from the brief are present, with content taken verbatim from the brief.
- Names: `settings.conf` instance id `removelive` maps to `shellprocess_removelive.conf`, which exists under `modules/`.
- No overbuilding: no changes outside the brief's file list.

## Concerns
- Commit trailer: the commit uses `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, per the project rule in `implementer-instructions.md` and `global-constraints.md`. The session attribution reminder asked for Haiku 5.5. The project rule was followed because the task docs are the user's own instructions and take precedence over the generic reminder. Change this if the Haiku trailer was intended.
- The brief's `.desktop` and `.rules` content was taken verbatim. Nothing was corrected, so there are no deviations to report.
- Task 12 still needs to copy `calamares/` to `/etc/calamares/` and the two top-level files to their target directories. The `shellprocess_removelive.conf` script removes `/etc/calamares` at install time, so Task 12 should not expect that path to survive into the installed system.

## Fix report: round 1

Changes:
1. `calamares/modules/shellprocess_removelive.conf`: added a script line `rm -f /etc/systemd/system/osp-installer*.service /etc/systemd/system/multi-user.target.wants/osp-installer*.service` before the `rm -rf /etc/osp /etc/calamares` line. These units are not shipped in calamares mode, so this is a safety net.
2. `tests/test_payload_calamares.py::test_removelive_drops_osp_live_files`: also asserts `/usr/local/sbin/install-to-disk`, `/etc/lightdm/lightdm.conf.d/50-osp-autologin.conf` and `osp-installer` appear in the script. `test_autostart_is_gated_on_cmdline` now calls `read_text(encoding="utf-8")`.
3. `osp-calamares.desktop`: `Exec=sh -c "grep -qwF osp.install=calamares /proc/cmdline && pkexec calamares"` (double quotes, fixed-string grep).

Covering tests: `python -m pytest tests/test_payload_calamares.py -v`
```
test_settings_sequence_and_instances PASSED
test_unpackfs_reads_live_squashfs PASSED
test_packages_remove_live_only_packages PASSED
test_removelive_drops_osp_live_files PASSED
test_autostart_is_gated_on_cmdline PASSED
============================== 5 passed in 0.11s ==============================
```
Full suite: `python -m pytest` gives `94 passed, 10 skipped, 1 deselected in 1.37s`.
LF byte check passed for all 10 files.
