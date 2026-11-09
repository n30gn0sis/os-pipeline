"""Host side: run osp commands inside the privileged builder container."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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


def _windows_console(fd: int) -> bool:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    try:
        handle = msvcrt.get_osfhandle(fd)
        # A private kernel32 instance, so declaring prototypes cannot affect other ctypes users.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (OSError, ctypes.ArgumentError):
        return False


def stdin_is_terminal(stdin=None, is_console: Callable[[int], bool] | None = None) -> bool:
    """True only for an interactive terminal. On Windows, isatty() is also True for NUL,
    which makes `docker run -it` fail, so the handle must also be a real console."""
    stdin = sys.stdin if stdin is None else stdin
    try:
        if stdin is None or not stdin.isatty():
            return False
        if is_console is None:
            if os.name != "nt":
                return True
            is_console = _windows_console
        return is_console(stdin.fileno())
    except (AttributeError, OSError, ValueError):
        return False


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
    proc = subprocess.Popen(cmd)
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.wait()  # let the engine forward the signal and remove the container
        raise
    if rc == 125:  # docker/podman: the container itself failed to start
        raise LauncherError(
            "could not start the builder container. Build the image with:\n"
            "  docker build -t osp-builder:latest -f builder/Containerfile builder\n"
            "and make sure privileged containers are allowed."
        )
    if rc not in (0, 1, 2):
        print(f"osp: container exited with code {rc}", file=sys.stderr)
        return 1
    return rc
