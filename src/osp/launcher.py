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
    raise LauncherError("docker or podman is required but neither was found on PATH")


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
