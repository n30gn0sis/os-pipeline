import json
import os
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
    stdin_is_terminal,
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


class _FakeProc:
    def __init__(self, rcs):
        self.rcs = list(rcs)

    def wait(self):
        rc = self.rcs.pop(0)
        if isinstance(rc, BaseException):
            raise rc
        return rc


def _patch_popen(monkeypatch, proc):
    from osp import launcher
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd: proc)
    return launcher


@pytest.mark.parametrize("rc,expected", [(0, 0), (1, 1), (2, 2), (137, 1), (130, 1)])
def test_launch_maps_exit_codes(monkeypatch, capsys, rc, expected):
    launcher = _patch_popen(monkeypatch, _FakeProc([rc]))
    assert launcher.launch(["docker"]) == expected
    assert ("container exited with code" in capsys.readouterr().err) == (rc not in (0, 1, 2))


def test_launch_125_raises(monkeypatch):
    launcher = _patch_popen(monkeypatch, _FakeProc([125]))
    with pytest.raises(LauncherError, match="could not start"):
        launcher.launch(["docker"])


def test_launch_keeps_waiting_on_interrupt_then_reraises(monkeypatch):
    proc = _FakeProc([KeyboardInterrupt(), 130])
    launcher = _patch_popen(monkeypatch, proc)
    with pytest.raises(KeyboardInterrupt):
        launcher.launch(["docker"])
    assert proc.rcs == []


class _Stream:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self):
        return self._tty

    def fileno(self):
        return 0


def test_stdin_is_terminal_false_for_non_tty():
    assert stdin_is_terminal(_Stream(False), is_console=lambda fd: True) is False


def test_stdin_is_terminal_requires_a_console_when_checked():
    # On Windows, NUL is a character device: isatty() is True but it is not a console.
    assert stdin_is_terminal(_Stream(True), is_console=lambda fd: False) is False
    assert stdin_is_terminal(_Stream(True), is_console=lambda fd: True) is True


def test_stdin_is_terminal_false_for_devnull():
    with open(os.devnull) as null:
        assert stdin_is_terminal(null) is False
