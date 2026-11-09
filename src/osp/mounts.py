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
            # Track before mounting so an interrupt between mount and append cannot leak it.
            # A failed mount leaves a harmless check=False umount of an unmounted path.
            mounted.append(target)
            runner.run(["mount", *args, str(target)])
        yield
    finally:
        for target in reversed(mounted):
            # One failing unmount must not skip the remaining targets.
            try:
                if runner.run(["umount", str(target)], check=False).returncode != 0:
                    runner.run(["umount", "-l", str(target)], check=False)
            except Exception:
                pass
