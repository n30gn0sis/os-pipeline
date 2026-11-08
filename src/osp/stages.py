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
